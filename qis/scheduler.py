"""Job queue: admission control, a scoring dispatcher (priority, aging, model affinity, fair share), retries and
crash recovery. The real queue lives here; ComfyUI only ever sees the job that is running (DESIGN §5).
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

import httpx
from pydantic import ValidationError

from .db import DB
from .engine.comfy.client import ComfyError, ComfyInterrupted, ComfyRejected, ComfyUnavailable
from .engine.ops import OpError, op_kind
from .engine.workflows import WorkflowError
from .events import EventBus
from .executor import Executor, JobCanceled
from .ids import new_id
from .pipeline import FILE_ID, JobSpec, validate_refs
from .settings import Settings
from .storage import Storage

log = logging.getLogger("qis.scheduler")

TERMINAL = {"succeeded", "failed", "canceled", "expired"}


class AdmissionError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after: int | None = None, param: str | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.retry_after, self.param = status, code, message, retry_after, param


@dataclass
class QItem:
    id: str
    key_id: str | None
    priority: str
    created_at: float
    cost: float
    affinity: str | None
    gpu: bool


@dataclass
class RItem:
    id: str
    key_id: str | None
    gpu: bool
    cost: float
    started: float
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None


class JobService:
    def __init__(self, db: DB, settings: Settings, events: EventBus, executor: Executor, storage: Storage, node_id: str,
                 gpu_slots: int = 1):
        self.db = db
        self.settings = settings
        self.events = events
        self.executor = executor
        self.storage = storage
        self.node_id = node_id
        self.gpu_slots = gpu_slots
        self.queued: dict[str, QItem] = {}
        self.running: dict[str, RItem] = {}
        self._starts: deque[tuple[float, str | None]] = deque()
        self._rpm: dict[str | None, deque[float]] = defaultdict(deque)
        self._done_events: dict[str, asyncio.Event] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._bg: set[asyncio.Task] = set()
        self._http: httpx.AsyncClient | None = None
        self.paused = False
        settings.on_change(lambda *_: self._wake.set())

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._http = httpx.AsyncClient(timeout=15)
        n = self.db.execute("UPDATE jobs SET status='queued' WHERE status='running' AND exec_node=?", (self.node_id,)).rowcount
        if n:
            log.warning("recovered %d job(s) that were running when the node stopped", n)
        for row in self.db.all("SELECT id, api_key_id, priority, created_at, cost_s, model_affinity, spec FROM jobs "
                               "WHERE status='queued' ORDER BY created_at"):
            try:
                gpu = JobSpec.model_validate(row["spec"]).gpu()
            except ValidationError:
                self._finish(row["id"], "failed", error={"code": "invalid_spec", "message": "stored spec no longer valid"})
                continue
            self.queued[row["id"]] = QItem(row["id"], row["api_key_id"], row["priority"], row["created_at"],
                                           row["cost_s"] or 0, row["model_affinity"], gpu)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
        for r in list(self.running.values()):
            if r.task:
                r.task.cancel()
        for r in list(self.running.values()):
            if r.task:
                with contextlib.suppress(BaseException):
                    await r.task
        for t in list(self._bg):
            t.cancel()
        if self._http:
            await self._http.aclose()

    # ------------------------------------------------------------------ cost / eta
    def estimate(self, spec: JobSpec) -> float:
        counts = validate_refs(spec)
        perf = self.settings.current.perf
        total = 0.0
        mp_in = 1.0
        ids = spec.file_ids()
        if ids:
            row = self.db.one("SELECT width, height FROM files WHERE id=?", (ids[0],))
            if row and row["width"]:
                mp_in = row["width"] * row["height"] / 1e6
        for i, op in enumerate(spec.ops):
            n_in = counts[i - 1] if i and op.image_fields else (len(ids) or 1)
            total += op.estimate(perf, n_in, mp_in)
            if op.op == "generate":
                mp_in = op._mp()
        return round(total, 2)

    def _running_remaining(self, gpu: bool = True) -> float:
        now = time.time()
        return sum(max(r.cost - (now - r.started), 1.0) for r in self.running.values() if r.gpu == gpu)

    def _score(self, q: QItem, now: float, recent: dict) -> float:
        s = self.settings.current.scheduler
        score = (s.interactive_weight if q.priority == "interactive" else 0) + s.aging_per_min * (now - q.created_at) / 60
        loaded = getattr(self.executor, "loaded_model", None)
        if q.affinity and loaded and q.affinity == loaded:
            score += s.affinity_bonus
        return score - s.fair_penalty * recent.get(q.key_id, 0)

    def _recent_starts(self, now: float) -> dict:
        window = self.settings.current.scheduler.fair_window_s
        while self._starts and self._starts[0][0] < now - window:
            self._starts.popleft()
        counts: dict = defaultdict(int)
        for _, k in self._starts:
            counts[k] += 1
        return counts

    def ordered(self, gpu: bool = True) -> list[QItem]:
        now = time.time()
        recent = self._recent_starts(now)
        items = [q for q in self.queued.values() if q.gpu == gpu]
        return sorted(items, key=lambda q: (-self._score(q, now, recent), q.created_at))

    def positions(self) -> dict[str, dict]:
        out = {}
        for gpu in (True, False):
            acc = self._running_remaining(gpu)
            for pos, q in enumerate(self.ordered(gpu), 1):
                acc += q.cost
                out[q.id] = {"position": pos, "eta_s": round(acc)}
        return out

    def queue_stats(self) -> dict:
        gpu_q = [q for q in self.queued.values() if q.gpu]
        return {"queued": len(gpu_q), "queued_cpu": len(self.queued) - len(gpu_q),
                "running": sum(1 for r in self.running.values() if r.gpu),
                "running_cpu": sum(1 for r in self.running.values() if not r.gpu),
                "max_pending": self.settings.current.queue.max_pending,
                "eta_s": round(self._running_remaining(True) + sum(q.cost for q in gpu_q)), "paused": self.paused}

    # ------------------------------------------------------------------ submit
    def submit(self, spec: JobSpec, *, key: dict | None = None, user_id: str | None = None, kind: str = "pipeline",
               idempotency_key: str | None = None) -> dict:
        st = self.settings.current
        key_id = key["id"] if key else None
        if idempotency_key:
            row = self.db.one("SELECT * FROM jobs WHERE api_key_id IS ? AND idempotency_key=? AND created_at>?",
                              (key_id, idempotency_key, time.time() - 86400))
            if row:
                return self.job_view(row)
        try:
            cost = self.estimate(spec)
        except OpError as e:
            raise AdmissionError(400, "invalid_pipeline", str(e)) from e
        if cost > st.job.max_cost_s:
            raise AdmissionError(400, "job_too_large",
                                 f"estimated {cost:.0f}s of GPU time exceeds the limit of {st.job.max_cost_s}s")
        for fid in spec.file_ids():
            row = self.db.one("SELECT owner_key_id FROM files WHERE id=?", (fid,))
            if not row:
                raise AdmissionError(400, "file_not_found", f"file {fid} does not exist", param="inputs")
            if key and row["owner_key_id"] not in (None, key_id) and "admin:*" not in (key.get("scopes") or []):
                raise AdmissionError(403, "file_forbidden", f"file {fid} belongs to another key", param="inputs")
        gpu = spec.gpu()
        now = time.time()
        if key:
            max_pending = key.get("max_pending") or st.keys.default_max_pending
            mine = sum(1 for q in self.queued.values() if q.key_id == key_id)
            if mine >= max_pending:
                raise AdmissionError(429, "key_limit", f"this key already has {mine} queued jobs (limit {max_pending})",
                                     retry_after=max(5, round(self._running_remaining())))
            rpm = key.get("rpm") or st.keys.default_rpm
            win = self._rpm[key_id]
            while win and win[0] < now - 60:
                win.popleft()
            if len(win) >= rpm:
                raise AdmissionError(429, "rate_limit", f"more than {rpm} requests per minute for this key",
                                     retry_after=max(1, round(60 - (now - win[0]))))
        if gpu:
            gpu_q = [q for q in self.queued.values() if q.gpu]
            eta = self._running_remaining() + sum(q.cost for q in gpu_q)
            if len(gpu_q) >= st.queue.max_pending:
                over = len(gpu_q) - st.queue.max_pending + 1
                avg = (sum(q.cost for q in gpu_q) / len(gpu_q)) if gpu_q else cost
                raise AdmissionError(429, "queue_full", f"queue is full ({len(gpu_q)} jobs waiting)",
                                     retry_after=max(5, round(self._running_remaining() + over * avg)))
            if gpu_q and eta + cost > st.queue.max_wait_s:
                raise AdmissionError(429, "queue_full", f"estimated wait {eta:.0f}s exceeds {st.queue.max_wait_s}s",
                                     retry_after=max(5, round(eta + cost - st.queue.max_wait_s)))
        elif sum(1 for q in self.queued.values() if not q.gpu) >= st.queue.cpu_max_pending:
            raise AdmissionError(429, "queue_full", "CPU queue is full", retry_after=5)
        if key:
            self._rpm[key_id].append(now)

        models = getattr(self.executor, "cfg", None)
        ms = models.models.model_set() if models else None
        affinity = next((op.model_key(ms) for op in spec.ops if op_kind(op) == "gpu"), None) if ms else None
        priority = spec.priority or (key or {}).get("priority") or "interactive"
        job_id = new_id("job")
        row = {"id": job_id, "api_key_id": key_id, "user_id": user_id, "origin_node": self.node_id, "exec_node": None,
               "kind": kind, "spec": spec.model_dump(mode="json", exclude_none=True), "preset": spec.preset,
               "status": "queued", "priority": priority, "model_affinity": affinity, "cost_s": cost, "attempts": 0,
               "created_at": now, "idempotency_key": idempotency_key, "webhook_url": spec.webhook_url,
               "retention_hours": spec.retention_hours}
        try:
            self.db.insert("jobs", row)
        except Exception as e:  # idempotency race
            if idempotency_key and "UNIQUE" in str(e):
                return self.job_view(self.db.one("SELECT * FROM jobs WHERE api_key_id IS ? AND idempotency_key=?",
                                                 (key_id, idempotency_key)))
            raise
        self.queued[job_id] = QItem(job_id, key_id, priority, now, cost, affinity, gpu)
        pos = self.positions().get(job_id, {})
        self.events.emit(job_id, "queued", {"position": pos.get("position"), "eta_s": pos.get("eta_s"), "cost_s": cost})
        self._wake.set()
        return self.job_view(self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,)))

    # ------------------------------------------------------------------ read
    def get(self, job_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        return self.job_view(row) if row else None

    def job_view(self, row: dict) -> dict:
        v = {k: row.get(k) for k in ("id", "status", "kind", "preset", "priority", "attempts", "created_at", "started_at",
                                     "finished_at", "api_key_id", "cost_s", "error", "result", "progress")}
        v["degraded"] = bool(row.get("degraded"))
        v["spec"] = row.get("spec")
        if row["status"] == "queued":
            v.update(self.positions().get(row["id"], {}))
        elif row["status"] == "running":
            r = self.running.get(row["id"])
            if r:
                v["eta_s"] = round(max(r.cost - (time.time() - r.started), 0))
        return v

    def list(self, *, key_id: str | None = None, status: str | None = None, limit: int = 50, before: str | None = None,
             all_keys: bool = False) -> list[dict]:
        sql, params = "SELECT * FROM jobs WHERE 1=1", []
        if not all_keys:
            sql += " AND api_key_id IS ?"
            params.append(key_id)
        elif key_id:
            sql += " AND api_key_id=?"
            params.append(key_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        if before:
            sql += " AND id<?"
            params.append(before)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        return [self.job_view(r) for r in self.db.all(sql, params)]

    async def wait(self, job_id: str, timeout: float | None = None) -> dict | None:
        job = self.get(job_id)
        if job is None or job["status"] in TERMINAL:
            return job
        ev = self._done_events.setdefault(job_id, asyncio.Event())
        with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(ev.wait(), timeout)
        return self.get(job_id)

    # ------------------------------------------------------------------ cancel
    async def cancel(self, job_id: str, actor: str = "api") -> dict | None:
        job = self.db.one("SELECT status FROM jobs WHERE id=?", (job_id,))
        if not job:
            return None
        if job_id in self.queued:
            self.queued.pop(job_id)
            self._finish(job_id, "canceled", error={"code": "canceled", "message": f"canceled by {actor}"})
        elif job_id in self.running:
            r = self.running[job_id]
            r.cancel.set()
            await self.executor.cancel(job_id)
        return self.get(job_id)

    # ------------------------------------------------------------------ dispatch
    async def _loop(self) -> None:
        last_expire = 0.0
        while True:
            try:
                self._wake.clear()
                now = time.time()
                if now - last_expire > 30:
                    self._expire(now)
                    last_expire = now
                if not self.paused:
                    self._dispatch()
                with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), 5)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("dispatcher error")
                await asyncio.sleep(1)

    def _dispatch(self) -> None:
        while sum(1 for r in self.running.values() if r.gpu) < self.gpu_slots:
            order = self.ordered(True)
            if not order:
                break
            self._start(order[0])
        cpu_slots = self.settings.current.cpu.workers
        while sum(1 for r in self.running.values() if not r.gpu) < cpu_slots:
            order = self.ordered(False)
            if not order:
                break
            self._start(order[0])

    def _expire(self, now: float) -> None:
        age = self.settings.current.queue.max_queue_age_s
        for q in list(self.queued.values()):
            if now - q.created_at > age:
                self.queued.pop(q.id, None)
                self._finish(q.id, "expired", error={"code": "expired", "message": f"waited more than {age}s in the queue"})

    def _start(self, q: QItem) -> None:
        self.queued.pop(q.id, None)
        now = time.time()
        r = RItem(q.id, q.key_id, q.gpu, q.cost, now)
        self.running[q.id] = r
        self._starts.append((now, q.key_id))
        self.db.execute("UPDATE jobs SET status='running', started_at=COALESCE(started_at, ?), exec_node=?, "
                        "attempts=attempts+1 WHERE id=?", (now, self.node_id, q.id))
        r.task = asyncio.create_task(self._run(r, q))

    async def _run(self, r: RItem, q: QItem) -> None:
        row = self.db.one("SELECT * FROM jobs WHERE id=?", (r.id,))
        st = self.settings.current
        timeout = max(st.job.timeout_min_s, r.cost * st.job.timeout_factor)
        emit = self._emitter(r.id)
        emit("started", {"attempt": row["attempts"], "exec_node": self.node_id, "degraded": bool(row["degraded"])})
        requeue = False
        try:
            spec = JobSpec.model_validate(row["spec"])
            result = await asyncio.wait_for(self.executor.run(row, spec, emit, r.cancel, bool(row["degraded"])), timeout)
            self._calibrate(result.get("timings", []))
            self._finish(r.id, "succeeded", result=result)
        except (JobCanceled, asyncio.CancelledError) as e:
            if isinstance(e, asyncio.CancelledError) and not r.cancel.is_set():
                # node shutting down: leave it queued so it runs after the restart
                self.db.execute("UPDATE jobs SET status='queued' WHERE id=?", (r.id,))
                raise
            self._finish(r.id, "canceled", error={"code": "canceled", "message": "canceled"})
        except TimeoutError:
            await self.executor.cancel(r.id)
            self._finish(r.id, "failed", error={"code": "timeout", "message": f"job exceeded {timeout:.0f}s"})
        except ComfyInterrupted:
            if r.cancel.is_set():
                self._finish(r.id, "canceled", error={"code": "canceled", "message": "canceled"})
            else:
                requeue = self._retry_or_fail(r, row, "interrupted", "ComfyUI interrupted the run")
        except ComfyRejected as e:
            self._finish(r.id, "failed", error={"code": "workflow_rejected", "message": str(e), "details": e.details})
        except ComfyError as e:
            if e.is_oom and not row["degraded"] and row["attempts"] < st.job.max_attempts:
                log.warning("job %s ran out of GPU memory, retrying with the text encoder on CPU", r.id)
                self.db.execute("UPDATE jobs SET degraded=1 WHERE id=?", (r.id,))
                requeue = True
            else:
                self._finish(r.id, "failed", error={"code": "oom" if e.is_oom else "execution_error", "message": str(e)})
        except ComfyUnavailable as e:
            requeue = self._retry_or_fail(r, row, "engine_unavailable", str(e))
        except (OpError, WorkflowError, ValidationError, KeyError) as e:
            self._finish(r.id, "failed", error={"code": "invalid_request", "message": str(e)})
        except Exception as e:
            log.exception("job %s crashed", r.id)
            self._finish(r.id, "failed", error={"code": "server_error", "message": f"{type(e).__name__}: {e}"})
        finally:
            self.running.pop(r.id, None)
            if requeue:
                self.db.execute("UPDATE jobs SET status='queued' WHERE id=?", (r.id,))
                self.queued[r.id] = q
                emit("requeued", {"attempt": row["attempts"]})
            self._wake.set()

    def _retry_or_fail(self, r: RItem, row: dict, code: str, message: str) -> bool:
        if row["attempts"] < self.settings.current.job.max_attempts:
            log.warning("job %s: %s, will retry (attempt %d)", r.id, message, row["attempts"])
            return True
        self._finish(r.id, "failed", error={"code": code, "message": message})
        return False

    def _emitter(self, job_id: str):
        last = [0.0]

        def emit(type_: str, data: dict | None = None):
            ev = self.events.emit(job_id, type_, data)
            if type_ in ("progress", "op_started", "op_done"):
                now = time.monotonic()
                if type_ != "progress" or now - last[0] > 2:
                    last[0] = now
                    prog = {k: v for k, v in (data or {}).items() if k not in ("data", "text")}
                    with contextlib.suppress(Exception):
                        self.db.update("jobs", "id", job_id, {"progress": {"type": type_, **prog}})
            return ev
        return emit

    def _calibrate(self, timings: list[dict]) -> None:
        perf = self.settings.current.perf
        calib = dict(perf.calib)
        changed = False
        for t in timings:
            if t.get("kind") != "gpu" or not t.get("estimate") or t["seconds"] < 0.5:
                continue
            ratio = min(max(t["seconds"] / t["estimate"], 0.1), 10)
            calib[t["op"]] = round(0.8 * calib.get(t["op"], 1.0) + 0.2 * ratio, 4)
            changed = True
        gpu_total = sum(t["seconds"] for t in timings if t.get("kind") == "gpu")
        if changed:
            avg = round(0.9 * perf.avg_job_s + 0.1 * gpu_total, 2) if gpu_total else perf.avg_job_s
            with contextlib.suppress(Exception):
                self.settings.update("perf", {"calib": calib, "avg_job_s": avg}, actor="calibration")

    def _finish(self, job_id: str, status: str, *, result: dict | None = None, error: dict | None = None) -> None:
        now = time.time()
        fields = {"status": status, "finished_at": now, "result": result, "error": error}
        self.db.update("jobs", "id", job_id, fields)
        self.events.emit(job_id, status, {"result": result} if result else {"error": error})
        self._stats(status, result)
        ev = self._done_events.pop(job_id, None)
        if ev:
            ev.set()
        row = self.db.one("SELECT webhook_url FROM jobs WHERE id=?", (job_id,))
        if row and row.get("webhook_url") and self._http:
            t = asyncio.ensure_future(self._webhook(job_id, row["webhook_url"]))
            self._bg.add(t)
            t.add_done_callback(self._bg.discard)

    def _stats(self, status: str, result: dict | None) -> None:
        minute = int(time.time() // 60 * 60)
        images = sum(len(o.get("images", [])) for o in (result or {}).get("outputs", []))
        gpu_s = sum(t["seconds"] for t in (result or {}).get("timings", []) if t.get("kind") == "gpu")
        with contextlib.suppress(Exception):
            self.db.execute(
                "INSERT INTO stats_minute(ts, jobs_done, jobs_failed, gpu_s, queue_depth, images) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(ts) DO UPDATE SET jobs_done=jobs_done+excluded.jobs_done, jobs_failed=jobs_failed+excluded.jobs_failed, "
                "gpu_s=gpu_s+excluded.gpu_s, queue_depth=MAX(queue_depth, excluded.queue_depth), images=images+excluded.images",
                (minute, int(status == "succeeded"), int(status == "failed"), gpu_s, len(self.queued), images))

    async def _webhook(self, job_id: str, url: str) -> None:
        job = self.get(job_id)
        body = json.dumps({"type": f"job.{job['status']}", "job": job}, default=str).encode()
        secret = self.storage.secret
        sig = hmac.new(secret, body, hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-Qis-Signature": f"sha256={sig}", "X-Qis-Job-Id": job_id}
        for attempt in range(3):
            try:
                r = await self._http.post(url, content=body, headers=headers)
                if r.status_code < 300:
                    return
                log.warning("webhook %s -> %s", url, r.status_code)
            except httpx.HTTPError as e:
                log.warning("webhook %s failed: %s", url, e)
            await asyncio.sleep(2 * 4 ** attempt)


def is_file_id(v: str) -> bool:
    return bool(FILE_ID.match(v))
