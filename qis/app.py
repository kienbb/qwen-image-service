"""FastAPI application factory."""
from __future__ import annotations

import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from . import __version__
from .api import admin, jobs, mcp, openai_images, system
from .api.common import install_error_handlers
from .config import Config
from .node import Node

log = logging.getLogger("qis")

# POST routes that create jobs; blocked while the node is a worker (the hub is the only front door)
JOB_ROUTES = re.compile(r"^/(v1/images/[^/]+|v1/jobs|v1/jobs/[^/]+/retry|v1/tools/[^/]+|mcp)/?$")


def web_dist() -> Path | None:
    p = Path(str(resources.files("qis"))) / "web" / "dist"
    return p if (p / "index.html").exists() else None


def create_app(cfg: Config, node: Node | None = None) -> FastAPI:
    node = node or Node(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await node.start()
        log.info("node %s ready on %s:%s", node.node_id, cfg.server.host, cfg.server.port)
        try:
            yield
        finally:
            await node.stop()

    app = FastAPI(title="Qwen Image Service", version=__version__, lifespan=lifespan,
                  description="OpenAI-compatible image API on a local Qwen-Image 2.1 node. Auth: `Authorization: Bearer sk-qis-...`")
    app.state.node = node
    install_error_handlers(app)
    if cfg.security.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=cfg.security.cors_origins, allow_credentials=True,
                           allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        length = request.headers.get("content-length")
        limit = node.settings.current.api.max_body_mb * 1024 * 1024
        if length and length.isdigit() and int(length) > limit:
            from .api.common import err
            return err(413, f"request body is larger than {limit // 1024 // 1024} MB", "body_too_large")
        if request.method == "POST" and node.worker_blocks_local_jobs and JOB_ROUTES.match(request.url.path):
            from .api.common import err
            return err(409, "this node is a worker: submit jobs to the hub (set worker.accept_local_jobs to allow local jobs)",
                       "worker_mode")
        t = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        response.headers["X-Qis-Node"] = node.node_id
        if request.url.path.startswith("/v1/") and response.status_code >= 400:
            log.info("%s %s -> %s (%.0f ms) rid=%s", request.method, request.url.path, response.status_code,
                     (time.monotonic() - t) * 1000, rid)
        return response

    app.include_router(system.router)
    app.include_router(openai_images.router)
    app.include_router(jobs.router)
    app.include_router(admin.router)
    app.include_router(mcp.router)

    dist = web_dist()

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/admin/")

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/{path:path}", include_in_schema=False)
    async def spa(path: str = ""):
        if dist is None:
            return HTMLResponse("<h1>Qwen Image Service</h1><p>The dashboard is not built. Run <code>npm run build</code> "
                                "in <code>web/</code> or install a release package. API docs: <a href='/docs'>/docs</a></p>")
        f = (dist / path).resolve()
        if path and f.is_file() and dist.resolve() in f.parents:
            return FileResponse(f)
        return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})

    return app
