"""Health, readiness, node info and Prometheus metrics."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .. import __version__
from ..node import Node
from .common import api_key, client_ip, get_node, need

router = APIRouter(tags=["system"])


@router.get("/healthz")
async def healthz(node: Node = Depends(get_node)):
    return {"status": "ok", "version": __version__, "node_id": node.node_id,
            "worker": node.worker.health() if node.worker else {"enabled": False}}


@router.get("/readyz")
async def readyz(node: Node = Depends(get_node)):
    sup = node.supervisor
    ready = sup.ready.is_set() and sup.warm and not sup.missing_nodes
    body = {"ready": ready, "comfy": sup.state, "warm": sup.warm, "missing_nodes": sup.missing_nodes}
    return JSONResponse(body, status_code=200 if ready else 503)


@router.get("/v1/node/info")
async def node_info(key: dict = Depends(need("jobs:read")), node: Node = Depends(get_node)):
    return node.info()


@router.get("/metrics")
async def metrics(request: Request, node: Node = Depends(get_node)):
    if client_ip(request) not in ("127.0.0.1", "::1") or request.headers.get("cf-connecting-ip"):
        key = api_key(request, node)
        if "admin:*" not in (key.get("scopes") or []):
            return PlainTextResponse("forbidden\n", status_code=403)
    q = node.jobs.queue_stats()
    lines = [
        "# TYPE qis_queue_depth gauge", f"qis_queue_depth {q['queued']}",
        "# TYPE qis_queue_depth_cpu gauge", f"qis_queue_depth_cpu {q['queued_cpu']}",
        "# TYPE qis_running gauge", f"qis_running {q['running']}",
        "# TYPE qis_queue_eta_seconds gauge", f"qis_queue_eta_seconds {q['eta_s']}",
        "# TYPE qis_comfy_ready gauge", f"qis_comfy_ready {int(node.supervisor.ready.is_set())}",
        "# TYPE qis_comfy_restarts_total counter", f"qis_comfy_restarts_total {node.supervisor.restarts}",
    ]
    lines.append("# TYPE qis_jobs_total counter")
    for row in node.db.all("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"):
        lines.append(f'qis_jobs_total{{status="{row["status"]}"}} {row["n"]}')
    gpu = node.gpu_info
    if gpu.get("memory_used") is not None:
        lines += ["# TYPE qis_gpu_vram_used_bytes gauge", f"qis_gpu_vram_used_bytes {gpu['memory_used']}",
                  "# TYPE qis_gpu_util gauge", f"qis_gpu_util {gpu.get('util_gpu', 0)}"]
    lines += ["# TYPE qis_storage_bytes gauge", f"qis_storage_bytes {node.storage.usage_bytes()}"]
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
