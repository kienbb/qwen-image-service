"""MCP (Model Context Protocol) endpoint, Streamable HTTP transport, authenticated with an API key.

POST /mcp takes JSON-RPC. Tool calls wait for their job; when the client accepts text/event-stream the answer is
an SSE stream with progress notifications and keep-alive pings, so long jobs survive proxies (Cloudflare 100 s).
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .. import __version__
from ..auth import AuthError
from ..engine.ops import OpError
from ..node import Node
from ..pipeline import JobSpec
from ..scheduler import TERMINAL, AdmissionError
from .common import api_key, get_node, resolve_image_value
from .jobs import check_scopes
from .present import job_public

router = APIRouter(tags=["mcp"])
PROTOCOL = "2025-06-18"
MAX_WAIT_S = 900
INLINE_MAX = 700_000  # bytes: inline small images (sprites) as MCP image content

IMG = {"type": "string", "description": "image: file id (file_...), https URL, data URL or base64"}
TOOLS = [
    {"name": "generate_image", "description": "Generate images from a text prompt on the Qwen-Image 2.1 node. "
     "Use list_models to see presets (e.g. pixelart-16bit-64 for game sprites).",
     "inputSchema": {"type": "object", "required": ["prompt"], "properties": {
         "prompt": {"type": "string"}, "model": {"type": "string", "description": "preset name"},
         "n": {"type": "integer", "minimum": 1, "maximum": 8}, "size": {"type": "string", "description": "WxH or auto"},
         "seed": {"type": "integer"}, "transparent": {"type": "boolean"},
         "quality": {"type": "string", "enum": ["low", "medium", "high", "hd"]},
         "enhance": {"type": "boolean", "description": "rewrite the prompt with the official Qwen rewriter (slow)"}}}},
    {"name": "edit_image", "description": "Edit 1-3 reference images following an instruction (image 1 sets the frame).",
     "inputSchema": {"type": "object", "required": ["prompt", "images"], "properties": {
         "prompt": {"type": "string"}, "images": {"type": "array", "items": IMG, "minItems": 1, "maxItems": 3},
         "model": {"type": "string"}, "seed": {"type": "integer"}}}},
    {"name": "pixelize", "description": "Turn an image into a pixel-art sprite (crop, grid downscale, palette, transparency).",
     "inputSchema": {"type": "object", "required": ["image"], "properties": {
         "image": IMG, "grid": {"type": "integer"}, "colors": {"type": "integer"},
         "palette": {"type": "string", "enum": ["adaptive", "pico8", "sweetie16", "db32"]}}}},
    {"name": "upscale", "description": "Upscale an image (Real-ESRGAN, or nearest for pixel art).",
     "inputSchema": {"type": "object", "required": ["image"], "properties": {
         "image": IMG, "scale": {"type": "number"}, "width": {"type": "integer"}, "height": {"type": "integer"},
         "method": {"type": "string", "enum": ["esrgan", "nearest", "lanczos"]}}}},
    {"name": "refine", "description": "Hires-fix: resize and lightly re-denoise to recreate fine texture.",
     "inputSchema": {"type": "object", "required": ["image", "prompt"], "properties": {
         "image": IMG, "prompt": {"type": "string"}, "scale": {"type": "number"}, "nodes": {"type": "string"}}}},
    {"name": "outpaint", "description": "Extend an image to a new size/aspect without redrawing the original pixels.",
     "inputSchema": {"type": "object", "required": ["image", "width", "height", "prompt"], "properties": {
         "image": IMG, "width": {"type": "integer"}, "height": {"type": "integer"}, "prompt": {"type": "string"},
         "anchor_y": {"type": "number"}}}},
    {"name": "remove_background", "description": "Make a flat background transparent.",
     "inputSchema": {"type": "object", "required": ["image"], "properties": {"image": IMG}}},
    {"name": "get_job", "description": "Status and outputs of a job (for runs that outlived the call).",
     "inputSchema": {"type": "object", "required": ["job_id"], "properties": {"job_id": {"type": "string"}}}},
    {"name": "list_models", "description": "Presets available on this node.", "inputSchema": {"type": "object", "properties": {}}},
]


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


async def _spec_for(node: Node, key: dict, name: str, args: dict) -> JobSpec:
    async def img(v):
        return await resolve_image_value(node, key, v)

    models = node.cfg.models.model_set()
    if name in ("generate_image", "edit_image"):
        model = args.get("model") or node.settings.current.api.default_preset
        if model not in node.presets.available(models):
            raise OpError(f"model {model!r} is not available; call list_models")
        extra = {k: args[k] for k in ("seed", "enhance") if k in args}
        refs = [await img(v) for v in args.get("images", [])] if name == "edit_image" else None
        return node.presets.build(model, prompt=args["prompt"], n=int(args.get("n", 1)), size=args.get("size"),
                                  quality=args.get("quality"), transparent=args.get("transparent"), refs=refs,
                                  extra=extra, models=models)
    op = {"pixelize": "pixelize", "upscale": "upscale", "refine": "refine", "outpaint": "outpaint",
          "remove_background": "remove_bg"}[name]
    params = {k: v for k, v in args.items() if k != "image"}
    return JobSpec.model_validate({"ops": [{"op": op, "image": await img(args["image"]), **params}]})


def _content(node: Node, request: Request, job: dict) -> dict:
    if job["status"] != "succeeded":
        e = job.get("error") or {}
        return {"isError": True, "content": [{"type": "text", "text": f"job {job['id']} {job['status']}: {e.get('message', '')}"}]}
    pub = job_public(node, request, job)
    out = pub["result"]["outputs"][-1]
    content: list[dict[str, Any]] = []
    for img in out["images"]:
        if (img.get("bytes") or 0) <= INLINE_MAX:
            content.append({"type": "image", "mimeType": "image/png",
                            "data": base64.b64encode(node.storage.read(img["file_id"])).decode()})
    summary = {"job_id": job["id"], "images": [{"file_id": i["file_id"], "url": i["url"], "width": i["width"],
                                                "height": i["height"]} for i in out["images"]],
               "extra": {n: [i["url"] for i in v] for n, v in out["extra"].items()}, "text": out.get("text"),
               "data": out.get("data")}
    content.insert(0, {"type": "text", "text": json.dumps(summary, ensure_ascii=False, default=str)})
    return {"content": content, "structuredContent": summary}


async def _call(node: Node, key: dict, request: Request, name: str, args: dict, progress=None) -> dict:
    if name == "list_models":
        avail = node.presets.available(node.cfg.models.model_set())
        text = json.dumps({n: p.get("description", "") for n, p in avail.items()}, ensure_ascii=False)
        return {"content": [{"type": "text", "text": text}]}
    if name == "get_job":
        job = node.jobs.get(args.get("job_id", ""))
        if not job or job["api_key_id"] != key["id"]:
            return {"isError": True, "content": [{"type": "text", "text": "job not found"}]}
        if job["status"] not in TERMINAL:
            return {"content": [{"type": "text", "text": json.dumps({"job_id": job["id"], "status": job["status"],
                                                                      "position": job.get("position"), "eta_s": job.get("eta_s")})}]}
        return _content(node, request, job)
    if name not in {t["name"] for t in TOOLS}:
        raise RpcError(-32602, f"unknown tool {name}")
    try:
        spec = await _spec_for(node, key, name, args)
        check_scopes(key, spec)
        job = node.jobs.submit(spec, key=key, kind=f"mcp.{name}")
    except (OpError, AdmissionError, AuthError, ValueError, KeyError) as e:
        msg = getattr(e, "message", None) or str(e)
        return {"isError": True, "content": [{"type": "text", "text": msg}]}
    q = node.events.subscribe(job["id"]) if progress else None
    try:
        waiter = asyncio.ensure_future(node.jobs.wait(job["id"], MAX_WAIT_S))
        while not waiter.done():
            if q is None:
                await asyncio.wait({waiter})
                break
            getter = asyncio.ensure_future(q.get())
            done, _ = await asyncio.wait({waiter, getter}, timeout=15, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                ev = getter.result()
                if ev["type"] == "progress":
                    await progress(ev["data"].get("pct", 0), f"{ev['data'].get('op')} {ev['data'].get('step')}/{ev['data'].get('steps')}")
            else:
                getter.cancel()
                if not done:
                    await progress(None, None)  # keep-alive
        final = waiter.result()
    finally:
        if q is not None:
            node.events.unsubscribe(job["id"], q)
    if final["status"] not in TERMINAL:
        return {"content": [{"type": "text", "text": json.dumps({"job_id": job["id"], "status": final["status"],
                                                                  "note": "still running; call get_job later"})}]}
    return _content(node, request, final)


async def _handle(node: Node, key: dict, request: Request, msg: dict, progress=None) -> dict | None:
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:  # notification
        return None
    try:
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "qwen-image-service", "version": __version__},
                      "instructions": "Image generation tools on a Qwen-Image 2.1 node. Images are returned inline "
                                      "(small ones) and as signed URLs. Long jobs may return a job_id: poll get_job."}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = await _call(node, key, request, params.get("name", ""), params.get("arguments") or {}, progress)
        else:
            raise RpcError(-32601, f"method not found: {method}")
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    except RpcError as e:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": e.code, "message": e.message}}


@router.post("/mcp")
async def mcp_post(request: Request, key: dict = Depends(api_key), node: Node = Depends(get_node)):
    try:
        msg = await request.json()
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}, status_code=400)
    if isinstance(msg, list):
        replies = [r for m in msg if (r := await _handle(node, key, request, m))]
        return JSONResponse(replies) if replies else Response(status_code=202)
    if msg.get("id") is None:
        return Response(status_code=202)
    wants_sse = "text/event-stream" in request.headers.get("accept", "") and msg.get("method") == "tools/call"
    if not wants_sse:
        return JSONResponse(await _handle(node, key, request, msg))

    token = (msg.get("params") or {}).get("_meta", {}).get("progressToken")
    queue: asyncio.Queue = asyncio.Queue()

    async def progress(pct, message):
        if pct is None:
            await queue.put(b": ping\n\n")
        elif token is not None:
            note = {"jsonrpc": "2.0", "method": "notifications/progress",
                    "params": {"progressToken": token, "progress": pct, "total": 1, "message": message}}
            await queue.put(f"event: message\ndata: {json.dumps(note)}\n\n".encode())

    async def run():
        reply = await _handle(node, key, request, msg, progress)
        await queue.put(f"event: message\ndata: {json.dumps(reply, default=str)}\n\n".encode())
        await queue.put(None)

    async def stream():
        task = asyncio.create_task(run())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.get("/mcp")
async def mcp_get():
    return Response(status_code=405, headers={"Allow": "POST"})
