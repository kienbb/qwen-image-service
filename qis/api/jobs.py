"""Native job API (/v1/jobs), files (/v1/files) and single-op tools (/v1/tools/{op})."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..auth import AuthError, has_scope
from ..engine.ops import OP_TYPES, OpError
from ..node import Node
from ..pipeline import JobSpec
from .common import api_key, get_node, need, resolve_image_value, store_input
from .present import convert, job_public, job_sse, sync_response

router = APIRouter(tags=["jobs"])


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ops: list[dict[str, Any]] | None = None
    preset: str | None = None
    prompt: str | None = None
    params: dict[str, Any] = Field(default_factory=dict, description="preset shortcut: parameters for the preset's ops")
    inputs: dict[str, str | list[str]] = Field(default_factory=dict)
    outputs: list[str] | None = None
    priority: str | None = None
    webhook_url: str | None = None
    retention_hours: float | None = None


def required_scopes(spec: JobSpec) -> set[str]:
    need_ = set()
    for op in spec.ops:
        if op.op == "generate":
            need_.add("images:edit" if op.refs else "images:generate")
        elif op.op == "enhance_prompt":
            need_.add("images:generate")
        else:
            need_.add("tools:*")
    return need_


def check_scopes(key: dict, spec: JobSpec) -> None:
    for s in required_scopes(spec):
        if not has_scope(key.get("scopes") or [], s):
            raise AuthError(403, "insufficient_scope", f"this API key lacks the '{s}' scope")


async def _resolve_inputs(node: Node, key: dict, inputs: dict, ops: list[dict]) -> tuple[dict, list[dict]]:
    res = {}
    for name, v in inputs.items():
        vals = v if isinstance(v, list) else [v]
        out = [await resolve_image_value(node, key, x) for x in vals]
        res[name] = out if isinstance(v, list) else out[0]
    new_ops = []
    for op in ops:
        op = dict(op)
        cls = OP_TYPES.get(op.get("op"))
        if cls is None:
            raise OpError(f"unknown op {op.get('op')!r}; valid: {sorted(OP_TYPES)}")
        for f in cls.image_fields:
            if f in op:
                v = op[f]
                vals = v if isinstance(v, list) else [v]
                out = [await resolve_image_value(node, key, x) for x in vals]
                op[f] = out if isinstance(v, list) else out[0]
        new_ops.append(op)
    return res, new_ops


async def build_spec(node: Node, key: dict, body: JobCreate) -> JobSpec:
    if body.ops is None:
        if not body.preset:
            raise OpError("give either ops[] or a preset")
        if body.preset not in node.presets.available(node.cfg.models.model_set()):
            raise OpError(f"preset {body.preset!r} is not available on this node")
        params = dict(body.params)
        spec = node.presets.build(body.preset, prompt=body.prompt or params.pop("prompt", ""), n=params.pop("n", 1),
                                  size=params.pop("size", None), quality=params.pop("quality", None),
                                  transparent=params.pop("transparent", None),
                                  refs=[await resolve_image_value(node, key, r) for r in params.pop("refs", [])] or None,
                                  extra=params, models=node.cfg.models.model_set())
        ops = spec.model_dump(mode="json", exclude_none=True)["ops"]
    else:
        ops = body.ops
    inputs, ops = await _resolve_inputs(node, key, body.inputs, ops)
    data = {"ops": ops, "inputs": inputs, "outputs": body.outputs, "priority": body.priority,
            "webhook_url": body.webhook_url, "retention_hours": body.retention_hours, "preset": body.preset}
    return JobSpec.model_validate({k: v for k, v in data.items() if v is not None})


def _own(node: Node, key: dict, job_id: str) -> dict:
    job = node.jobs.get(job_id)
    if not job or (job["api_key_id"] != key["id"] and "admin:*" not in (key.get("scopes") or [])):
        raise HTTPException(404, f"job {job_id} not found")
    return job


@router.post("/v1/jobs", status_code=202)
async def create_job(request: Request, body: JobCreate, key: dict = Depends(api_key), node: Node = Depends(get_node)):
    spec = await build_spec(node, key, body)
    check_scopes(key, spec)
    if spec.preset:
        from .common import check_model_allowed
        check_model_allowed(key, spec.preset)
    job = node.jobs.submit(spec, key=key, kind="pipeline", idempotency_key=request.headers.get("idempotency-key"))
    return JSONResponse(job_public(node, request, job), status_code=202,
                        headers={"Location": f"/v1/jobs/{job['id']}", "X-Qis-Job-Id": job["id"]})


@router.get("/v1/jobs")
async def list_jobs(request: Request, status: str | None = None, limit: int = Query(20, ge=1, le=200),
                    after: str | None = None, key: dict = Depends(need("jobs:read")), node: Node = Depends(get_node)):
    jobs = node.jobs.list(key_id=key["id"], status=status, limit=limit, before=after)
    return {"object": "list", "data": [job_public(node, request, j) for j in jobs],
            "has_more": len(jobs) == limit, "last_id": jobs[-1]["id"] if jobs else None}


@router.get("/v1/jobs/{job_id}")
async def get_job(job_id: str, request: Request, key: dict = Depends(need("jobs:read")), node: Node = Depends(get_node)):
    return job_public(node, request, _own(node, key, job_id))


@router.get("/v1/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request, key: dict = Depends(need("jobs:read")), node: Node = Depends(get_node)):
    _own(node, key, job_id)
    last = request.headers.get("last-event-id") or request.query_params.get("last_event_id") or "0"
    return StreamingResponse(job_sse(node, job_id, request, int(last) if last.isdigit() else 0),
                             media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.delete("/v1/jobs/{job_id}")
async def cancel_job(job_id: str, request: Request, key: dict = Depends(api_key), node: Node = Depends(get_node)):
    _own(node, key, job_id)
    return job_public(node, request, await node.jobs.cancel(job_id, actor=key["name"]))


@router.post("/v1/jobs/{job_id}/retry", status_code=202)
async def retry_job(job_id: str, request: Request, key: dict = Depends(api_key), node: Node = Depends(get_node)):
    old = _own(node, key, job_id)
    spec = JobSpec.model_validate(old["spec"])
    check_scopes(key, spec)
    job = node.jobs.submit(spec, key=key, kind="pipeline")
    return JSONResponse(job_public(node, request, job), status_code=202)


# ---------------------------------------------------------------- files
@router.post("/v1/files")
async def upload_file(file: UploadFile = File(...), purpose: str = Form("input"), key: dict = Depends(need("files:*")),
                      node: Node = Depends(get_node)):
    if purpose not in ("input", "vision", "user_data"):
        raise OpError("purpose must be input")
    data = await file.read()
    fid = store_input(node, key, data, file.filename or "upload.png")
    return _file_obj(node.storage.get(fid))


def _file_obj(row: dict) -> dict:
    return {"id": row["id"], "object": "file", "bytes": row["bytes"], "created_at": int(row["created_at"]),
            "expires_at": int(row["expires_at"]), "filename": row.get("name") or row["id"] + ".png", "purpose": row["purpose"],
            "mime_type": row["mime"], "width": row["width"], "height": row["height"], "has_alpha": bool(row["has_alpha"]),
            "sha256": row["sha256"]}


def _own_file(node: Node, key: dict, file_id: str) -> dict:
    row = node.storage.get(file_id)
    if not row or (row["owner_key_id"] not in (None, key["id"]) and "admin:*" not in (key.get("scopes") or [])):
        raise HTTPException(404, f"file {file_id} not found")
    return row


@router.get("/v1/files/{file_id}")
async def file_meta(file_id: str, key: dict = Depends(need("files:*")), node: Node = Depends(get_node)):
    return _file_obj(_own_file(node, key, file_id))


@router.get("/v1/files/{file_id}/content")
async def file_content(file_id: str, request: Request, exp: int | None = None, sig: str | None = None,
                       format: str | None = None, node: Node = Depends(get_node)):
    if exp is not None and sig:
        if not node.storage.verify(file_id, exp, sig):
            raise HTTPException(403, "link expired or invalid signature")
        row = node.storage.get(file_id)
        if not row:
            raise HTTPException(404, "file not found")
    else:
        key = api_key(request, node)
        if not has_scope(key.get("scopes") or [], "files:*"):
            raise AuthError(403, "insufficient_scope", "this API key lacks the 'files:*' scope")
        row = _own_file(node, key, file_id)
    data = node.storage.read(file_id)
    mime = row["mime"]
    if format in ("webp", "jpeg") and mime == "image/png":
        data, mime = convert(data, format), f"image/{format}"
    return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=3600",
                                                    "Content-Disposition": f'inline; filename="{file_id}.{mime.split("/")[-1]}"'})


@router.delete("/v1/files/{file_id}")
async def delete_file(file_id: str, key: dict = Depends(need("files:*")), node: Node = Depends(get_node)):
    _own_file(node, key, file_id)
    node.storage.delete(file_id)
    return {"id": file_id, "object": "file", "deleted": True}


# ---------------------------------------------------------------- tools
TOOL_OPS = {"enhance-prompt": "enhance_prompt", "refine": "refine", "upscale": "upscale", "outpaint": "outpaint",
            "pixelize": "pixelize", "remove-bg": "remove_bg", "compose": "compose", "fidelity": "fidelity",
            "generate": "generate"}


def _render_tool(node: Node, request: Request, response_format: str):
    def render(job: dict) -> dict:
        pub = job_public(node, request, job)
        out = pub["result"]["outputs"][-1]
        if response_format == "b64_json":
            import base64
            for img in out["images"] + [i for v in out["extra"].values() for i in v]:
                img["b64_json"] = base64.b64encode(node.storage.read(img["file_id"])).decode()
        return {"object": "tool.result", "job_id": job["id"], "op": out["op"], "images": out["images"],
                "extra": out["extra"], "text": out.get("text"), "data": out.get("data"),
                "timings": pub["result"].get("timings")}
    return render


@router.post("/v1/tools/{tool}")
async def run_tool(tool: str, request: Request, key: dict = Depends(api_key), node: Node = Depends(get_node)):
    op_name = TOOL_OPS.get(tool) or (tool if tool in OP_TYPES else None)
    if not op_name:
        raise HTTPException(404, f"unknown tool {tool}; available: {sorted(TOOL_OPS)}")
    ctype = request.headers.get("content-type", "")
    params: dict[str, Any] = {}
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        for k, v in form.multi_items():
            if hasattr(v, "read"):
                fid = store_input(node, key, await v.read(), v.filename or "image.png")
                if k in params:
                    params[k] = (params[k] if isinstance(params[k], list) else [params[k]]) + [fid]
                else:
                    params[k] = fid
            else:
                try:
                    params[k] = json.loads(v) if v not in ("", None) and k not in ("prompt", "scene", "nodes", "refine_nodes") else v
                except ValueError:
                    params[k] = v
    else:
        params = await request.json()
    response_format = params.pop("response_format", "url")
    is_async = request.query_params.get("async") in ("1", "true") or params.pop("async", False)
    webhook = params.pop("webhook_url", None)
    cls = OP_TYPES[op_name]
    for f in cls.image_fields:
        if f not in params and f in ("image", "model", "images") and cls.model_fields[f].default == "$prev":
            raise OpError(f"{f} is required")
    body = JobCreate(ops=[{"op": op_name, **params}], webhook_url=webhook)
    spec = await build_spec(node, key, body)
    check_scopes(key, spec)
    job = node.jobs.submit(spec, key=key, kind=f"tool.{op_name}", idempotency_key=request.headers.get("idempotency-key"))
    if is_async:
        return JSONResponse(job_public(node, request, job), status_code=202)
    return await sync_response(node, job, _render_tool(node, request, response_format))
