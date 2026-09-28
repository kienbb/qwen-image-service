"""Turning jobs into responses: file URLs / base64, output format conversion, and the long-request strategies
(keep-alive whitespace, SSE) that keep Cloudflare from cutting requests after ~100 s (DESIGN §6.2)."""
from __future__ import annotations

import asyncio
import base64
import io
import json
from collections.abc import AsyncIterator, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from PIL import Image

from ..node import Node
from ..scheduler import TERMINAL
from .common import error_body

FIRST_WAIT_S = 60  # answer normally (real status code) if the job is done within this; else start keep-alive


def base_url(node: Node, request: Request) -> str:
    return node.public_url or str(request.base_url).rstrip("/")


def file_url(node: Node, request: Request, file_id: str) -> str:
    return base_url(node, request) + node.storage.signed_path(file_id, node.settings.current.storage.url_ttl_s)


def convert(data: bytes, fmt: str, compression: int | None = None) -> bytes:
    if fmt in (None, "", "png"):
        return data
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        buf = io.BytesIO()
        q = 100 - (compression if compression is not None else 10)
        if fmt == "jpeg":
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.getchannel("A"))
                im = bg
            im.convert("RGB").save(buf, "JPEG", quality=max(1, min(q, 100)))
        elif fmt == "webp":
            im.save(buf, "WEBP", quality=max(1, min(q, 100)))
        else:
            raise ValueError(f"unknown output_format {fmt}")
        return buf.getvalue()


def image_entry(node: Node, request: Request, ref: dict, response_format: str, output_format: str = "png",
                compression: int | None = None) -> dict:
    fid = ref["file_id"]
    if response_format == "b64_json":
        return {"b64_json": base64.b64encode(convert(node.storage.read(fid), output_format, compression)).decode()}
    url = file_url(node, request, fid)
    if output_format != "png":
        url += f"&format={output_format}"
    return {"url": url}


def job_public(node: Node, request: Request, job: dict) -> dict:
    """Job view for API clients: output files get signed URLs, internal fields are dropped."""
    j = {k: v for k, v in job.items() if k not in ("api_key_id",)}
    res = j.get("result")
    if res:
        res = dict(res)
        outs = []
        for o in res.get("outputs", []):
            o = dict(o)
            o["images"] = [{**i, "url": file_url(node, request, i["file_id"])} for i in o.get("images", [])]
            o["extra"] = {n: [{**i, "url": file_url(node, request, i["file_id"])} for i in v] for n, v in (o.get("extra") or {}).items()}
            outs.append(o)
        res["outputs"] = outs
        j["result"] = res
    j["object"] = "job"
    return j


def job_error_status(job: dict) -> int:
    code = (job.get("error") or {}).get("code")
    return {"invalid_request": 400, "workflow_rejected": 400, "canceled": 409, "expired": 504, "timeout": 504,
            "engine_unavailable": 503}.get(code, 500)


def job_error_body(job: dict) -> dict:
    e = job.get("error") or {}
    status = job_error_status(job)
    body = error_body(status, e.get("message") or f"job {job['status']}", e.get("code") or job["status"])
    body["error"]["job_id"] = job["id"]
    return body


async def sync_response(node: Node, job: dict, render: Callable[[dict], dict], headers: dict | None = None):
    """Wait for a job and answer with render(job). Long jobs stream whitespace keep-alives before the JSON."""
    headers = {"X-Qis-Job-Id": job["id"], "X-Qis-Node": node.node_id, **(headers or {})}
    api = node.settings.current.api
    first = FIRST_WAIT_S if api.sync_keepalive else api.sync_max_s
    done = await node.jobs.wait(job["id"], first)
    if done and done["status"] in TERMINAL:
        if done["status"] == "succeeded":
            return JSONResponse(render(done), headers=headers)
        return JSONResponse(job_error_body(done), status_code=job_error_status(done), headers=headers)
    if not api.sync_keepalive:
        body = error_body(504, f"job {job['id']} is still running; poll GET /v1/jobs/{job['id']}", "still_running")
        body["error"]["job_id"] = job["id"]
        return JSONResponse(body, status_code=504, headers=headers)

    async def stream() -> AsyncIterator[bytes]:
        while True:
            j = await node.jobs.wait(job["id"], api.keepalive_interval_s)
            if j is None or j["status"] in TERMINAL:
                break
            yield b" "
        # headers are gone: a failure can only be reported in the body (status stays 200)
        payload = render(j) if j and j["status"] == "succeeded" else job_error_body(j or {"id": job["id"], "status": "failed"})
        yield json.dumps(payload).encode()

    return StreamingResponse(stream(), media_type="application/json", headers=headers)


def sse(event: str | None, data: dict, id_: int | None = None) -> bytes:
    out = ""
    if id_ is not None:
        out += f"id: {id_}\n"
    if event:
        out += f"event: {event}\n"
    return (out + f"data: {json.dumps(data, default=str)}\n\n").encode()


async def job_sse(node: Node, job_id: str, request: Request, last_id: int = 0) -> AsyncIterator[bytes]:
    """Native job events: replay what the client missed, then follow live until a terminal event."""
    q = node.events.subscribe(job_id)
    try:
        seen = last_id
        for ev in node.events.history(job_id, last_id):
            seen = ev["seq"]
            yield sse(ev["type"], {"job_id": job_id, **(ev["data"] or {})}, ev["seq"])
        job = node.jobs.get(job_id)
        if job and job["status"] in TERMINAL:
            return
        interval = node.settings.current.api.keepalive_interval_s
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), interval)
            except TimeoutError:
                if await request.is_disconnected():
                    return
                yield b": ping\n\n"
                continue
            if ev["seq"] <= seen:
                continue
            seen = ev["seq"]
            yield sse(ev["type"], {"job_id": job_id, **(ev["data"] or {})}, ev["seq"])
            if ev["type"] in TERMINAL:
                return
    finally:
        node.events.unsubscribe(job_id, q)
