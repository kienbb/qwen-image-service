"""Executors run a job's ops. LocalComfyExecutor drives the ComfyUI of this node; a RemoteNodeExecutor for the
mesh (P5) will implement the same interface by calling another node's /v1/jobs."""
from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import io
import logging
import time
from concurrent.futures import Executor as PoolExecutor
from typing import Any, Protocol

from PIL import Image

from .config import Config
from .db import DB
from .engine.comfy.client import ComfyClient, ComfyUnavailable
from .engine.comfy.supervisor import ComfySupervisor
from .engine.ops import OpError, StepResult, op_kind
from .engine.workflows import workflow_models
from .pipeline import JobSpec, bind, selected_outputs
from .settings import Settings
from .storage import Storage, reencode_png

log = logging.getLogger("qis.executor")


class JobCanceled(Exception):
    pass


class Emit(Protocol):
    def __call__(self, type_: str, data: dict | None = None) -> Any: ...


class Executor(Protocol):
    def capabilities(self) -> dict: ...
    async def run(self, job: dict, spec: JobSpec, emit: Emit, cancel: asyncio.Event, degraded: bool) -> dict: ...
    async def cancel(self, job_id: str) -> None: ...


class _JobCtx:
    """OpContext for one job (see qis.engine.ops.OpContext)."""

    def __init__(self, ex: LocalComfyExecutor, job: dict, emit: Emit, cancel: asyncio.Event, degraded: bool):
        self.ex = ex
        self.job = job
        self.job_id = job["id"]
        self.emit = emit
        self.cancel_ev = cancel
        self.degraded = degraded
        self.models = ex.cfg.models.model_set()
        self.step = 0
        self.op_name = ""
        self.prev_text: str | None = None
        self.uploaded: dict[str, str] = {}
        self.prompt_id: str | None = None
        self._last_preview = 0.0
        self.created: list[str] = []

    # files
    def read(self, file_id: str) -> bytes:
        return self.ex.storage.read(file_id)

    def size(self, file_id: str) -> tuple[int, int]:
        row = self.ex.storage.get(file_id)
        if row and row.get("width"):
            return row["width"], row["height"]
        with Image.open(io.BytesIO(self.read(file_id))) as im:
            return im.size

    def store(self, data: bytes, name: str) -> str:
        info = reencode_png(data)
        row = self.ex.storage.put_image(info, purpose="intermediate", hours=self.ex.settings.current.storage.intermediate_hours,
                                        owner_key_id=self.job.get("api_key_id"), job_id=self.job_id, name=name)
        self.created.append(row["id"])
        return row["id"]

    async def upload(self, src: str | bytes) -> str:
        data = self.read(src) if isinstance(src, str) else src
        sha = hashlib.sha256(data).hexdigest()
        if sha not in self.uploaded:
            await self.ex.ready()
            self.uploaded[sha] = await self.ex.client.upload(data, f"{sha[:24]}.png")
        return self.uploaded[sha]

    # ComfyUI
    def _prepare(self, wf: dict) -> dict:
        if self.degraded:  # OOM retry: text encoder stays in RAM
            for node in wf.values():
                if node["class_type"] == "CLIPLoader":
                    node["inputs"]["device"] = "cpu"
        return wf

    async def _run(self, wf: dict, label: str, part: tuple[int, int]) -> dict:
        self.check_cancel()
        await self.ex.ready()
        i, n = part

        async def on_progress(value: int, maximum: int, node: str | None):
            if maximum:
                self.emit("progress", {"op_index": self.step, "op": self.op_name, "label": label, "step": value,
                                       "steps": maximum, "part": i + 1, "parts": n,
                                       "pct": round((i + value / maximum) / n, 4)})

        async def on_preview(data: bytes, mime: str):
            now = time.monotonic()
            if now - self._last_preview < 0.8:
                return
            self._last_preview = now
            self.emit("preview", {"op_index": self.step, "mime": mime, "b64": base64.b64encode(data).decode()})

        def submitted(pid: str):
            self.prompt_id = pid

        try:
            outputs = await self.ex.client.run(self._prepare(wf), on_progress=on_progress, on_preview=on_preview,
                                               on_submitted=submitted)
        finally:
            self.prompt_id = None
        loaded = workflow_models(wf)
        unets = [m for m in loaded if m.endswith((".gguf", ".safetensors"))]
        if unets:
            self.ex.loaded_model = unets[0]
        self.check_cancel()
        return outputs

    async def comfy(self, workflow: dict, node: str, label: str = "", progress_part: tuple[int, int] = (0, 1)) -> list[bytes]:
        outputs = await self._run(workflow, label or self.op_name, progress_part)
        images = await self.ex.client.images(outputs, node)
        if not images:
            raise OpError(f"ComfyUI returned no image from node {node}")
        return images

    async def comfy_text(self, workflow: dict, node: str) -> str:
        outputs = await self._run(workflow, "enhance", (0, 1))
        text = (outputs.get(node) or {}).get("text") or [""]
        return text[0] if isinstance(text, list) else str(text)

    async def cpu(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.ex.cpu_pool, functools.partial(fn, *args))

    # enhancer cache
    def cached_prompt(self, key: str):
        row = self.ex.db.one("SELECT prompt, ratio FROM prompt_cache WHERE key=?", (key,))
        if row:
            self.ex.db.execute("UPDATE prompt_cache SET hits=hits+1 WHERE key=?", (key,))
            return row["prompt"], row["ratio"] or ""
        return None

    def cache_prompt(self, key: str, request: str, prompt: str, ratio: str) -> None:
        self.ex.db.execute("INSERT OR REPLACE INTO prompt_cache(key, request, prompt, ratio, created_at, hits) VALUES(?,?,?,?,?,0)",
                           (key, request, prompt, ratio, time.time()))

    def check_cancel(self) -> None:
        if self.cancel_ev.is_set():
            raise JobCanceled()


class LocalComfyExecutor:
    def __init__(self, cfg: Config, db: DB, settings: Settings, storage: Storage, client: ComfyClient,
                 supervisor: ComfySupervisor, cpu_pool: PoolExecutor, ready_timeout_s: float = 180):
        self.cfg = cfg
        self.db = db
        self.settings = settings
        self.storage = storage
        self.client = client
        self.supervisor = supervisor
        self.cpu_pool = cpu_pool
        self.ready_timeout_s = ready_timeout_s
        self.loaded_model: str | None = None
        self._jobs: dict[str, _JobCtx] = {}

    def capabilities(self) -> dict:
        m = self.cfg.models.model_set()
        return {"modes": m.modes(), "unets": m.unets, "upscalers": list(m.upscalers), "comfy": self.supervisor.status()}

    async def ready(self) -> None:
        if not self.supervisor.ready.is_set() and not await self.supervisor.wait_ready(self.ready_timeout_s):
            raise ComfyUnavailable(f"ComfyUI is {self.supervisor.state}: {self.supervisor.last_error}".rstrip(": "))

    async def cancel(self, job_id: str) -> None:
        ctx = self._jobs.get(job_id)
        if ctx:
            ctx.cancel_ev.set()
            if ctx.prompt_id:
                await self.client.interrupt(ctx.prompt_id)

    async def run(self, job: dict, spec: JobSpec, emit: Emit, cancel: asyncio.Event, degraded: bool = False) -> dict:
        ctx = _JobCtx(self, job, emit, cancel, degraded)
        self._jobs[job["id"]] = ctx
        results: list[StepResult] = []
        timings = []
        perf = self.settings.current.perf
        try:
            for i, op in enumerate(spec.ops):
                ctx.check_cancel()
                ctx.step, ctx.op_name = i, op.op
                ctx.prev_text = results[-1].text if results else None
                bound, refs = bind(op, spec, results, i)
                emit("op_started", {"op_index": i, "op": op.op, "kind": op_kind(op)})
                n_in = sum(len(v) for v in refs.values())
                mp_in = 1.0
                first = next((v[0] for v in refs.values() if v), None)
                if first:
                    w, h = ctx.size(first)
                    mp_in = w * h / 1e6
                est = bound.estimate(perf, n_in, mp_in) / max(perf.calib.get(op.op, 1.0), 1e-6)
                t = time.monotonic()
                res = await bound.run(ctx, refs)
                dt = time.monotonic() - t
                timings.append({"op": op.op, "seconds": round(dt, 3), "estimate": round(est, 3), "kind": op_kind(op)})
                results.append(res)
                emit("op_done", {"op_index": i, "op": op.op, "images": res.images, "extra": res.extra,
                                 "text": res.text, "data": res.data, "seconds": round(dt, 2)})
        except BaseException:
            self._jobs.pop(job["id"], None)
            raise
        self._jobs.pop(job["id"], None)
        chosen = selected_outputs(spec, results)
        hours = job.get("retention_hours") or self.settings.current.storage.output_hours
        keep = []
        outputs = []
        for k in chosen:
            r = results[k]
            keep += r.images + [f for v in r.extra.values() for f in v]
            outputs.append({"step": k, "op": spec.ops[k].op, "images": [self._file_ref(f) for f in r.images],
                            "extra": {n: [self._file_ref(f) for f in v] for n, v in r.extra.items()},
                            "text": r.text, "data": r.data})
        self.storage.set_expiry(keep, hours, purpose="output")
        return {"outputs": outputs, "timings": timings, "degraded": degraded,
                "steps": [{"op": spec.ops[i].op, "images": r.images, "extra": r.extra, "data": r.data}
                          for i, r in enumerate(results)]}

    def _file_ref(self, fid: str) -> dict:
        row = self.storage.get(fid) or {}
        return {"file_id": fid, "width": row.get("width"), "height": row.get("height"),
                "has_alpha": bool(row.get("has_alpha")), "bytes": row.get("bytes")}

