"""OpenAI Images API compatibility: /v1/images/generations, /v1/images/edits, /v1/models."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..engine.ops import OpError
from ..node import Node
from ..pipeline import JobSpec
from ..scheduler import TERMINAL
from .common import check_model_allowed, get_node, need, store_input
from .present import convert, image_entry, job_error_body, sse, sync_response

router = APIRouter(tags=["openai"])

KNOWN = {"prompt", "model", "n", "size", "quality", "background", "output_format", "output_compression",
         "response_format", "stream", "partial_images", "user", "moderation", "style"}
SPECIAL = {"priority", "webhook_url", "retention_hours"}


class ImageGenRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    prompt: str = Field(min_length=1, max_length=8000)
    model: str | None = None
    n: int = Field(1, ge=1, le=16)
    size: str | None = None
    quality: str | None = None
    background: Literal["transparent", "opaque", "auto"] | None = None
    output_format: Literal["png", "jpeg", "webp"] = "png"
    output_compression: int | None = Field(None, ge=0, le=100)
    response_format: Literal["b64_json", "url"] = "b64_json"
    stream: bool = False
    partial_images: int = Field(0, ge=0, le=3)
    user: str | None = None
    moderation: str | None = None
    style: str | None = None


def _split_extra(values: dict) -> tuple[dict, dict]:
    extra, special = {}, {}
    for k, v in values.items():
        if k in KNOWN:
            continue
        (special if k in SPECIAL else extra)[k] = v
    return extra, special


def _build(node: Node, key: dict, req: dict, refs: list[str] | None) -> tuple[str, JobSpec]:
    model = req.get("model") or node.settings.current.api.default_preset
    check_model_allowed(key, model)
    available = node.presets.available(node.cfg.models.model_set())
    if model not in available:
        raise OpError(f"model {model!r} is not available on this node; see GET /v1/models")
    if req.get("n", 1) > node.settings.current.api.max_n:
        raise OpError(f"n must be <= {node.settings.current.api.max_n}")
    extra, special = _split_extra(req)
    style = req.get("style")
    if style and style not in ("vivid", "natural"):  # OpenAI's dall-e-3 style is ignored; ours is the pixel style
        extra["style"] = style
    bg = req.get("background")
    spec = node.presets.build(model, prompt=req["prompt"], n=req.get("n", 1), size=req.get("size"),
                              quality=req.get("quality"), transparent=None if bg in (None, "auto") else bg == "transparent",
                              refs=refs, extra=extra, models=node.cfg.models.model_set())
    upd = {k: special[k] for k in SPECIAL if k in special}
    if upd:
        spec = JobSpec.model_validate({**spec.model_dump(mode="json", exclude_none=True), **upd})
    return model, spec


def _render(node: Node, request: Request, req: dict):
    def render(job: dict) -> dict:
        res = job["result"]
        out = res["outputs"][-1]
        steps = res.get("steps", [])
        gen = next((s for s in steps if s["op"] == "generate"), {})
        gdata = gen.get("data") or {}
        revised = gdata.get("revised_prompt") or gdata.get("prompt")
        data = []
        for img in out["images"]:
            entry = image_entry(node, request, img, req["response_format"], req["output_format"], req.get("output_compression"))
            if revised:
                entry["revised_prompt"] = revised
            data.append(entry)
        body = {"created": int(job.get("finished_at") or time.time()), "data": data,
                "background": req.get("background") or "auto", "output_format": req["output_format"],
                "size": req.get("size") or "auto", "quality": req.get("quality") or "auto",
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                "qis": {"job_id": job["id"], "model": job.get("preset"), "seeds": gdata.get("seeds"),
                        "timings": res.get("timings"), "degraded": res.get("degraded", False)}}
        extra = {n: [image_entry(node, request, r, req["response_format"]) for r in refs]
                 for n, refs in (out.get("extra") or {}).items()}
        if extra:
            body["qis"]["extra"] = extra
        if len(steps) > 1 and gen.get("images"):
            body["qis"]["raw"] = [image_entry(node, request, {"file_id": f}, req["response_format"]) for f in gen["images"]]
        return body
    return render


async def _stream(node: Node, request: Request, job: dict, req: dict, render):
    """OpenAI image streaming: image_generation.partial_image events, then image_generation.completed per image."""
    q = node.events.subscribe(job["id"])
    base = {"size": req.get("size") or "auto", "quality": req.get("quality") or "auto",
            "background": req.get("background") or "auto", "output_format": req["output_format"]}

    async def gen():
        partial = 0
        interval = node.settings.current.api.keepalive_interval_s
        try:
            while True:
                cur = node.jobs.get(job["id"])
                if cur and cur["status"] in TERMINAL:
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), interval)
                except TimeoutError:
                    yield b": ping\n\n"
                    continue
                if ev["type"] == "preview" and partial < req["partial_images"]:
                    import base64 as b64
                    raw = b64.b64decode(ev["data"]["b64"])
                    data = b64.b64encode(convert(raw, "png") if ev["data"].get("mime") == "image/png" else raw).decode()
                    yield sse("image_generation.partial_image", {"type": "image_generation.partial_image", "b64_json": data,
                                                                 "partial_image_index": partial, "created_at": int(time.time()), **base})
                    partial += 1
                if ev["type"] in TERMINAL:
                    break
        finally:
            node.events.unsubscribe(job["id"], q)
        final = node.jobs.get(job["id"])
        if final["status"] != "succeeded":
            yield sse("error", {"type": "error", **job_error_body(final)})
            return
        body = render(final)
        for item in body["data"]:
            yield sse("image_generation.completed", {"type": "image_generation.completed", "created_at": body["created"],
                                                     **item, **base, "usage": body["usage"], "qis": body["qis"]})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Qis-Job-Id": job["id"], "X-Accel-Buffering": "no"})


@router.post("/v1/images/generations")
async def generations(request: Request, body: ImageGenRequest, key: dict = Depends(need("images:generate")),
                      node: Node = Depends(get_node)):
    req = body.model_dump()
    req.update(body.model_extra or {})
    model, spec = _build(node, key, req, None)
    job = node.jobs.submit(spec, key=key, kind="images.generations", idempotency_key=request.headers.get("idempotency-key"))
    render = _render(node, request, req)
    if req["stream"]:
        return await _stream(node, request, job, req, render)
    return await sync_response(node, job, render)


def _coerce(v: str):
    """Multipart form values are strings; accept JSON numbers / booleans / lists for extra parameters."""
    if v in ("true", "false"):
        return v == "true"
    try:
        return json.loads(v)
    except ValueError:
        return v


@router.post("/v1/images/edits")
async def edits(request: Request, key: dict = Depends(need("images:edit")), node: Node = Depends(get_node)):
    form = await request.form()
    files = [f for k in ("image", "image[]") for f in form.getlist(k) if hasattr(f, "read")]
    if not files:
        raise OpError("image is required (multipart field 'image' or 'image[]')")
    max_refs = node.settings.current.inputs.max_refs
    if len(files) > max_refs:
        raise OpError(f"at most {max_refs} images")
    if form.get("mask"):
        raise OpError("mask (inpainting) is not supported yet; describe the change in the prompt instead")
    refs = [store_input(node, key, await f.read(), f.filename or "image.png") for f in files]
    req: dict = {"response_format": "b64_json", "output_format": "png", "stream": False, "partial_images": 0}
    for k, v in form.multi_items():
        if k in ("image", "image[]", "mask") or hasattr(v, "read"):
            continue
        req[k] = _coerce(v) if k not in ("prompt", "model", "size", "quality", "background", "user") else v
    body = ImageGenRequest.model_validate(req)
    req = body.model_dump()
    req.update(body.model_extra or {})
    model, spec = _build(node, key, req, refs)
    job = node.jobs.submit(spec, key=key, kind="images.edits", idempotency_key=request.headers.get("idempotency-key"))
    render = _render(node, request, req)
    if req["stream"]:
        return await _stream(node, request, job, req, render)
    return await sync_response(node, job, render)


@router.get("/v1/models")
async def models(key: dict = Depends(need("jobs:read")), node: Node = Depends(get_node)):
    avail = node.presets.available(node.cfg.models.model_set())
    allowed = key.get("allowed_models")
    data = []
    for name, p in avail.items():
        if allowed and name not in allowed:
            continue
        data.append(_model_obj(node, name, p))
    return {"object": "list", "data": data}


def _model_obj(node: Node, name: str, p: dict) -> dict:
    spec = node.presets.build(name, prompt="x", models=node.cfg.models.model_set())
    return {"id": name, "object": "model", "created": int(node.started_at), "owned_by": "qis",
            "qis": {"description": p.get("description", ""), "ops": [o["op"] for o in p["ops"]],
                    "est_s": round(node.jobs.estimate(spec)), "commercial_ok": False,
                    "license": "Qwen Research License (non-commercial)"}}


@router.get("/v1/models/{model_id}")
async def model(model_id: str, key: dict = Depends(need("jobs:read")), node: Node = Depends(get_node)):
    avail = node.presets.available(node.cfg.models.model_set())
    if model_id not in avail:
        from fastapi import HTTPException
        raise HTTPException(404, f"model {model_id} not found")
    return _model_obj(node, model_id, avail[model_id])
