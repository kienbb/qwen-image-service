"""AttemptRunner: runs the attempts the hub assigns (docs/MESH.md section 2.6).

assign -> accept -> download the inputs from the presigned URLs -> submit a local `kind="hub"` job (internal, so the
normal engine / scheduler / ComfyUI path runs it) -> relay progress -> upload the outputs with presigned PUTs ->
`result`. Whatever happens, an attempt ends in exactly one `result` or `failure`, which stays in an in-memory outbox
until the hub confirms it (and is replayed after every reconnect).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from PIL import Image
from pydantic import ValidationError

from .. import __version__
from ..api.common import store_input
from ..api.openai_images import ImageGenRequest, _build
from ..api.present import convert
from ..engine.ops import OpError
from ..pipeline import JobSpec
from ..scheduler import TERMINAL, AdmissionError
from ..storage import InputError
from .caps import Capabilities
from .link import HubLink
from .protocol import (
    PREVIEW_MAX_BYTES,
    Accept,
    Assign,
    Cancel,
    Failure,
    FailureCode,
    ImageObj,
    Message,
    OutputItem,
    Phase,
    Progress,
    Reject,
    RejectReason,
    Result,
    ResultAck,
    ResultMeta,
    RunningRef,
    Slots,
    Timings,
    UnackedRef,
    UploadDenied,
    UploadFileReq,
    UploadGrant,
    UploadRequest,
    Welcome,
)
from .state import WorkerState

log = logging.getLogger("qis.mesh.runner")

RETRYABLE = {FailureCode.OOM, FailureCode.ENGINE_UNAVAILABLE, FailureCode.INPUT_FETCH_FAILED, FailureCode.UPLOAD_FAILED,
             FailureCode.TIMEOUT, FailureCode.INTERNAL}
JOB_ERROR_CODES = {  # local job error.code -> failure.code (section 2.6)
    "oom": FailureCode.OOM,
    "invalid_request": FailureCode.INVALID_INPUT, "workflow_rejected": FailureCode.INVALID_INPUT,
    "invalid_spec": FailureCode.INVALID_INPUT, "job_too_large": FailureCode.INVALID_INPUT,
    "invalid_pipeline": FailureCode.INVALID_INPUT, "file_not_found": FailureCode.INVALID_INPUT,
    "engine_unavailable": FailureCode.ENGINE_UNAVAILABLE, "timeout": FailureCode.TIMEOUT,
    "canceled": FailureCode.CANCELLED, "expired": FailureCode.TIMEOUT,
}
EXT = {"png": "png", "webp": "webp", "jpeg": "jpg"}
CTYPE = {"png": "image/png", "webp": "image/webp", "jpeg": "image/jpeg"}
MAX_TEXT_BYTES = 256 * 1024
KEEP_DONE_S = 300.0   # how long a confirmed attempt stays known (a duplicate assign is answered from memory)


class AttemptFailed(Exception):
    def __init__(self, code: FailureCode, message: str, retryable: bool | None = None):
        super().__init__(message)
        self.code, self.message = code, message
        self.retryable = code in RETRYABLE if retryable is None else retryable


class _Canceled(Exception):
    """The hub asked to stop this attempt: finish with failure(cancelled)."""


class _Dropped(Exception):
    """The hub no longer wants anything from this attempt (stale / drop): clean up, send nothing."""


class _Reject(Exception):
    def __init__(self, reason: RejectReason, message: str):
        super().__init__(message)
        self.reason, self.message = reason, message


@dataclass
class Planned:
    name: str
    content_type: str
    data: bytes
    width: int | None = None
    height: int | None = None
    has_alpha: bool = False
    sha256: str = ""


@dataclass
class Attempt:
    assign: Assign
    spec: JobSpec | None
    build_error: str = ""
    gpu: bool = True
    phase: str = Phase.FETCHING
    pct: float = 0.0
    op_index: int | None = None
    op: str | None = None
    local_job_id: str | None = None
    local_files: list[str] = field(default_factory=list)
    out_files: list[str] = field(default_factory=list)
    task: asyncio.Task | None = None
    cancel_ev: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_reason: str = ""
    drop: bool = False
    final: Message | None = None
    final_sent: bool = False
    sent_at_ack: int = -1
    acked: bool = False
    done_at: float | None = None
    grants: asyncio.Queue = field(default_factory=asyncio.Queue)
    last_progress: float = 0.0
    previews: int = 0
    fetch_s: float = 0.0
    upload_s: float = 0.0
    weights: list[float] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.assign.attempt_id

    @property
    def active(self) -> bool:
        return self.final is None


def remap(obj, mapping: dict[str, str]):
    """Replace every string equal to a hub file id by the local one (refs, inputs, op image fields)."""
    if isinstance(obj, str):
        return mapping.get(obj, obj)
    if isinstance(obj, list):
        return [remap(x, mapping) for x in obj]
    if isinstance(obj, dict):
        return {k: remap(v, mapping) for k, v in obj.items()}
    return obj


def map_job_error(job: dict) -> AttemptFailed:
    e = job.get("error") or {}
    status = job.get("status")
    code = JOB_ERROR_CODES.get(e.get("code") or status, FailureCode.INTERNAL)
    if status == "canceled":
        code = FailureCode.CANCELLED
    return AttemptFailed(code, e.get("message") or f"local job {status}")


def slug(name: str, limit: int) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:limit] or "x"


class AttemptRunner:
    def __init__(self, node, link: HubLink, caps: Capabilities, state: WorkerState, get_state: Callable[[], str], *,
                 http: httpx.AsyncClient | None = None):
        self.node, self.link, self.caps, self.state, self.get_state = node, link, caps, state, get_state
        self.cfg = node.cfg.worker
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15))
        self._own_http = http is None
        self.attempts: dict[str, Attempt] = {}
        self.accepting = True
        # waits of section 2.6 / 9 (tests shrink them)
        self.put_retry_delays: tuple[float, ...] = (1.0, 4.0, 16.0)
        self.fetch_retry_delays: tuple[float, ...] = (1.0, 3.0)
        self.regrant_s = 60.0
        self.upload_budget_s = 1800.0
        self.grant_wait_s = 20.0
        self.progress_interval_s = 1.0

    async def close(self) -> None:
        if self._own_http:
            await self.http.aclose()

    # ------------------------------------------------------------------ views for hello / heartbeat / status
    def running_refs(self) -> list[RunningRef]:
        return [RunningRef(attempt_id=a.id, phase=str(a.phase), pct=round(a.pct, 3)) for a in self.attempts.values() if a.active]

    def unacked_refs(self) -> list[UnackedRef]:
        return [UnackedRef(attempt_id=a.id) for a in self.attempts.values() if a.final is not None and not a.acked]

    def unfinished_ids(self) -> list[str]:
        return [a.id for a in self.attempts.values() if a.active]

    def idle(self) -> bool:
        """Nothing running and every result confirmed by the hub."""
        return not self.unfinished_ids() and not self.unacked_refs()

    def free_slots(self) -> Slots:
        gpu_n = sum(1 for a in self.attempts.values() if a.active and a.gpu)
        cpu_n = sum(1 for a in self.attempts.values() if a.active and not a.gpu)
        return Slots(gpu=0 if gpu_n else 1, prefetch=max(0, self.cfg.prefetch - max(0, gpu_n - 1)),
                     cpu=max(0, self.cfg.cpu_slots - cpu_n))

    def status_running(self) -> list[dict]:
        return [{"attempt_id": a.id, "job_id": a.assign.job_id, "phase": str(a.phase), "pct": round(a.pct, 3)}
                for a in self.attempts.values() if a.active]

    # ------------------------------------------------------------------ message entry point
    async def handle(self, msg: Message) -> None:
        if isinstance(msg, Assign):
            await self.on_assign(msg)
        elif isinstance(msg, UploadGrant | UploadDenied):
            a = self.attempts.get(msg.attempt_id)
            if a:
                a.grants.put_nowait(msg)
        elif isinstance(msg, ResultAck):
            self.on_result_ack(msg)
        elif isinstance(msg, Cancel):
            self.on_cancel(msg)

    def on_heartbeat_ack(self) -> None:
        """A failure has no ack message of its own: once a heartbeat_ack arrived after it was sent, it is delivered."""
        for a in self.attempts.values():
            if isinstance(a.final, Failure) and a.final_sent and not a.acked and self.link.ack_count > a.sent_at_ack:
                a.acked = True
                a.done_at = time.monotonic()
        self._prune()

    def _prune(self) -> None:
        now = time.monotonic()
        for aid in [k for k, a in self.attempts.items() if a.acked and a.done_at and now - a.done_at > KEEP_DONE_S]:
            self.attempts.pop(aid, None)

    # ------------------------------------------------------------------ session hooks
    async def on_session(self, welcome: Welcome) -> None:
        for aid, what in welcome.resume.items():
            a = self.attempts.get(aid)
            if a is not None and what == "drop":
                log.info("hub says drop attempt %s", aid)
                self._drop(a)
        for a in list(self.attempts.values()):
            if a.final is not None and not a.acked:
                await self._send_final(a)     # unacked results / failures are replayed right after welcome

    async def on_session_end(self) -> None:
        for a in self.attempts.values():
            a.final_sent = False

    # ------------------------------------------------------------------ assign
    async def on_assign(self, m: Assign) -> None:
        known = self.attempts.get(m.attempt_id)
        if known is not None:     # duplicate delivery of the same attempt: same local job, never a second one
            if known.active:
                await self.link.send(Accept(attempt_id=m.attempt_id))
            else:
                await self._send_final(known)
            return
        try:
            att = self._admit(m)
        except _Reject as r:
            log.info("rejecting %s: %s (%s)", m.attempt_id, r.reason, r.message)
            await self.link.send(Reject(attempt_id=m.attempt_id, reason=r.reason, message=r.message))
            return
        self.attempts[m.attempt_id] = att
        await self.link.send(Accept(attempt_id=m.attempt_id))
        att.task = asyncio.create_task(self._run(att), name=f"qis-attempt-{m.attempt_id}")

    def _admit(self, m: Assign) -> Attempt:
        if self.state.draining or not self.accepting:
            raise _Reject(RejectReason.DRAINING, "this worker is draining")
        if self.state.paused:
            raise _Reject(RejectReason.BUSY, "this worker is paused by the hub")
        if m.kind not in ("images.generations", "images.edits", "pipeline"):
            raise _Reject(RejectReason.INVALID, f"unknown job kind {m.kind!r}")
        if self.get_state() == "warming":
            raise _Reject(RejectReason.BUSY, "the engine is still warming up")
        models = self.node.cfg.models.model_set()
        if m.kind != "pipeline":
            model = (m.payload.get("request") or {}).get("model") or self.node.settings.current.api.default_preset
            if model not in self.node.presets.available(models):
                raise _Reject(RejectReason.PRESET_UNKNOWN, f"preset {model!r} is not available on this worker")
            mine = self.caps.presets().get(model)
            if mine is None:
                raise _Reject(RejectReason.MODEL_MISSING, f"model files for {model!r} are missing on disk")
            if m.model_fingerprint and m.model_fingerprint != mine.fingerprint:   # the client pinned other model files
                raise _Reject(RejectReason.MODEL_MISSING,
                              f"fingerprint {m.model_fingerprint} is not what this worker has ({mine.fingerprint})")
        spec, error = None, ""
        try:
            spec = self._build_spec(m)
        except (OpError, ValidationError, ValueError, KeyError, TypeError) as e:
            error = str(e)    # a job the worker cannot even build: accepted, then failed as invalid_input (no retry)
        gpu = spec.gpu() if spec else False
        if spec is not None and m.kind == "pipeline":
            missing = self.caps.missing_for(spec)
            if missing:
                raise _Reject(RejectReason.MODEL_MISSING, "missing model files: " + ", ".join(missing))
        if spec is not None:
            active = [a for a in self.attempts.values() if a.active]
            if gpu and sum(1 for a in active if a.gpu) >= 1 + self.cfg.prefetch:
                raise _Reject(RejectReason.BUSY, "all GPU slots are taken")
            if not gpu and sum(1 for a in active if not a.gpu) >= self.cfg.cpu_slots:
                raise _Reject(RejectReason.BUSY, "all CPU slots are taken")
        att = Attempt(assign=m, spec=spec, build_error=error, gpu=gpu)
        if spec is not None:
            perf = self.node.settings.current.perf
            w = [max(op.estimate(perf, 1, 1.0), 0.01) for op in spec.ops]
            att.weights = [x / sum(w) for x in w]
        return att

    def _build_spec(self, m: Assign) -> JobSpec:
        if m.kind == "pipeline":
            spec = JobSpec.model_validate(m.payload.get("spec") or {})
        else:
            body = ImageGenRequest.model_validate(m.payload.get("request") or {})
            req = body.model_dump()
            req.update(body.model_extra or {})
            refs = list(m.payload.get("refs") or [])
            if m.kind == "images.edits" and not refs:
                raise OpError("images.edits needs at least one reference image")
            _, spec = _build(self.node, {}, req, refs or None)
        update = {"webhook_url": None, "retention_hours": None}   # the hub owns webhooks and retention
        if m.priority in ("interactive", "batch"):
            update["priority"] = m.priority
        return spec.model_copy(update=update)

    # ------------------------------------------------------------------ one attempt
    async def _run(self, att: Attempt) -> None:
        t0 = time.monotonic()
        try:
            await self._progress(att, Phase.FETCHING, force=True)
            if att.spec is None:
                raise AttemptFailed(FailureCode.INVALID_INPUT, att.build_error or "invalid job")
            mapping = await self._guard(att, self._fetch_inputs(att))
            await self._set_phase(att, Phase.QUEUED_LOCAL)
            job = self._submit(att, mapping)
            job = await self._follow(att, job)
            if job["status"] != "succeeded":
                if att.cancel_ev.is_set():
                    raise _Dropped() if att.drop else _Canceled()
                raise map_job_error(job)
            await self._set_phase(att, Phase.UPLOADING)
            result = await self._guard(att, self._upload_and_build(att, job))
            await self._finish(att, result)
        except _Dropped:
            self._forget(att)
        except _Canceled:
            await self._fail(att, AttemptFailed(FailureCode.CANCELLED, f"canceled ({att.cancel_reason or 'by the hub'})", False))
        except AttemptFailed as e:
            await self._fail(att, e)
        except asyncio.CancelledError:
            if att.final is None:   # shutting down: `bye.abandon` tells the hub; nothing more to send
                log.info("attempt %s interrupted by shutdown", att.id)
            raise
        except Exception as e:
            log.exception("attempt %s crashed", att.id)
            await self._fail(att, AttemptFailed(FailureCode.INTERNAL, f"{type(e).__name__}: {e}"))
        finally:
            log.info("attempt %s finished in %.1fs", att.id, time.monotonic() - t0)

    async def _guard(self, att: Attempt, coro):
        """Await `coro`, but abandon it as soon as the hub cancels or drops the attempt."""
        task = asyncio.ensure_future(coro)
        waiter = asyncio.ensure_future(att.cancel_ev.wait())
        try:
            done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                return task.result()
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            raise _Dropped() if att.drop else _Canceled()
        finally:
            waiter.cancel()
            if not task.done():
                task.cancel()

    # ---- inputs
    async def _fetch_inputs(self, att: Attempt) -> dict[str, str]:
        t = time.monotonic()
        limit = self.node.settings.current.inputs.max_bytes
        mapping: dict[str, str] = {}
        for inp in att.assign.inputs:
            data = await self._download(inp.url, limit)
            try:
                fid = await asyncio.to_thread(store_input, self.node, None, data, f"{inp.file_id}.png")
            except InputError as e:
                raise AttemptFailed(FailureCode.INVALID_INPUT, f"input {inp.file_id}: {e}") from e
            mapping[inp.file_id] = fid
            att.local_files.append(fid)
        att.fetch_s = round(time.monotonic() - t, 3)
        return mapping

    async def _download(self, url: str, limit: int) -> bytes:
        last = "download failed"
        for delay in (0.0, *self.fetch_retry_delays):
            if delay:
                await asyncio.sleep(delay)
            try:
                async with self.http.stream("GET", url, timeout=60) as r:
                    if r.status_code != 200:
                        last = f"HTTP {r.status_code}"
                        if r.status_code in (400, 401, 403, 404):
                            break      # an expired or wrong URL will not heal by itself
                        continue
                    data = bytearray()
                    async for chunk in r.aiter_bytes():
                        data += chunk
                        if len(data) > limit:
                            raise AttemptFailed(FailureCode.INVALID_INPUT, f"input is larger than {limit} bytes")
                    return bytes(data)
            except (httpx.HTTPError, OSError) as e:
                last = f"{type(e).__name__}: {e}".rstrip(": ")
        raise AttemptFailed(FailureCode.INPUT_FETCH_FAILED, f"cannot fetch an input image ({last})")

    # ---- local job
    def _submit(self, att: Attempt, mapping: dict[str, str]) -> dict:
        data = remap(att.spec.model_dump(mode="json", exclude_none=True), mapping)
        try:
            spec = JobSpec.model_validate(data)
            job = self.node.jobs.submit(spec, key=None, kind="hub", idempotency_key=att.id, internal=True,
                                        origin_node="hub")
        except AdmissionError as e:
            raise AttemptFailed(FailureCode.INVALID_INPUT if e.status < 500 else FailureCode.INTERNAL,
                                f"{e.code}: {e.message}") from e
        except (OpError, ValidationError, ValueError) as e:
            raise AttemptFailed(FailureCode.INVALID_INPUT, str(e)) from e
        att.local_job_id = job["id"]
        return job

    async def _follow(self, att: Attempt, job: dict) -> dict:
        """Relay the local job's progress to the hub until it ends; returns the final job view."""
        jobs, jid = self.node.jobs, job["id"]
        q = self.node.events.subscribe(jid)
        cancel_deadline: float | None = None
        try:
            while True:
                cur = jobs.get(jid)
                if cur is None or cur["status"] in TERMINAL:
                    return cur or {"status": "failed", "error": {"code": "server_error", "message": "job vanished"}}
                if att.cancel_ev.is_set():
                    if cancel_deadline is None:
                        cancel_deadline = time.monotonic() + 10
                        await jobs.cancel(jid, actor="hub")
                        continue
                    if time.monotonic() > cancel_deadline:
                        raise _Dropped() if att.drop else _Canceled()
                if cur["status"] == "running" and att.phase == Phase.QUEUED_LOCAL:
                    await self._set_phase(att, Phase.RUNNING)
                try:
                    ev = await asyncio.wait_for(q.get(), 0.25)
                except TimeoutError:
                    continue
                await self._on_event(att, ev)
        finally:
            self.node.events.unsubscribe(jid, q)

    async def _on_event(self, att: Attempt, ev: dict) -> None:
        typ, data = ev["type"], ev.get("data") or {}
        if typ == "started" and att.phase == Phase.QUEUED_LOCAL:
            await self._set_phase(att, Phase.RUNNING)
        elif typ == "op_started":
            att.op_index, att.op = data.get("op_index"), data.get("op")
        elif typ == "progress":
            i = data.get("op_index", att.op_index or 0)
            att.op_index, att.op = i, data.get("op", att.op)
            frac = data.get("pct") or 0.0
            w = att.weights
            att.pct = min(0.99, (sum(w[:i]) + (w[i] if i < len(w) else 0) * frac) if w else frac)
            await self._progress(att, Phase.RUNNING, step=data.get("step"), steps=data.get("steps"))
        elif typ == "preview" and att.previews < att.assign.output.partial_images:
            jpeg = self._preview_jpeg(data)
            if jpeg:
                att.previews += 1
                await self._progress(att, Phase.RUNNING, force=True, preview=base64.b64encode(jpeg).decode())

    @staticmethod
    def _preview_jpeg(data: dict) -> bytes | None:
        try:
            raw = base64.b64decode(data["b64"])
            if data.get("mime") == "image/jpeg" and len(raw) <= PREVIEW_MAX_BYTES:
                return raw
            with Image.open(io.BytesIO(raw)) as im:
                im.thumbnail((512, 512))
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "JPEG", quality=70)
            return buf.getvalue() if len(buf.getvalue()) <= PREVIEW_MAX_BYTES else None
        except Exception:
            return None

    async def _set_phase(self, att: Attempt, phase: Phase) -> None:
        att.phase = phase
        await self._progress(att, phase, force=True)

    async def _progress(self, att: Attempt, phase: str, *, force: bool = False, step: int | None = None,
                        steps: int | None = None, preview: str | None = None) -> None:
        now = time.monotonic()
        if not force and now - att.last_progress < self.progress_interval_s:
            return
        att.last_progress = now
        kw = {k: v for k, v in (("op_index", att.op_index), ("op", att.op), ("step", step), ("steps", steps),
                                ("preview_jpeg_b64", preview)) if v is not None}
        await self.link.send(Progress(attempt_id=att.id, phase=str(phase), pct=round(att.pct, 4), **kw))

    # ---- outputs
    async def _upload_and_build(self, att: Attempt, job: dict) -> Result:
        res = job["result"]
        plan, layout = await asyncio.to_thread(self._plan_files, att, res)
        grants = await self._upload(att, plan)
        by_name = {p.name: p for p in plan}

        def image(name: str) -> ImageObj:
            p = by_name[name]
            return ImageObj(key=grants[name].key, content_type=p.content_type, bytes=len(p.data), width=p.width,
                            height=p.height, has_alpha=p.has_alpha, sha256=p.sha256)

        outputs = []
        for o in layout["outputs"]:
            text, data = o["text"], o["data"]
            if text and len(text.encode()) > MAX_TEXT_BYTES:
                text = text[:MAX_TEXT_BYTES // 4]
            if len(json.dumps(data, default=str)) > MAX_TEXT_BYTES:
                data = {"truncated": True}
            outputs.append(OutputItem(step=o["step"], op=o["op"], images=[image(n) for n in o["images"]],
                                      extra={k: [image(n) for n in v] for k, v in o["extra"].items()},
                                      text=text, data=json.loads(json.dumps(data, default=str))))
        timings = self._timings(att, job, res)
        gen = next((s for s in res.get("steps", []) if s["op"] == "generate"), {})
        gdata = gen.get("data") or {}
        meta = ResultMeta(revised_prompt=gdata.get("revised_prompt") or gdata.get("prompt"), seeds=gdata.get("seeds") or [],
                          degraded=bool(res.get("degraded")), cold_start=bool(res.get("cold_start")),
                          model_fingerprint=self._fingerprint(att))
        return Result(attempt_id=att.id, job_id=att.assign.job_id, outputs=outputs,
                      raw=[image(n) for n in layout["raw"]], meta=meta, timings=timings, worker_version=__version__)

    def _fingerprint(self, att: Attempt) -> str | None:
        """Presets: the fingerprint advertised for the model. Pipelines: the files the ops used."""
        a = att.assign
        if a.kind != "pipeline":
            model = (a.payload.get("request") or {}).get("model") or self.node.settings.current.api.default_preset
            pre = self.caps.presets().get(model)
            if pre:
                return pre.fingerprint
        return self.caps.spec_fingerprint(att.spec) if att.spec else None

    def _plan_files(self, att: Attempt, res: dict) -> tuple[list[Planned], dict]:
        a = att.assign
        fmt = a.output.format if a.output.format in EXT else "png"
        outputs = res["outputs"]
        last = len(outputs) - 1
        plan: list[Planned] = []
        layout: dict = {"outputs": [], "raw": []}

        def add(file_id: str, name: str, convert_to: str | None = None) -> str:
            data = self.node.storage.read(file_id)
            ctype = "image/png"
            if convert_to and convert_to != "png":
                data = convert(data, convert_to, a.output.compression)
                ctype = CTYPE[convert_to]
            with Image.open(io.BytesIO(data)) as im:
                w, h, alpha = im.width, im.height, im.mode in ("RGBA", "LA", "PA")
            plan.append(Planned(name, ctype, data, w, h, alpha, hashlib.sha256(data).hexdigest()))
            att.out_files.append(file_id)
            return name

        for oi, o in enumerate(outputs):
            step = o["step"]
            item = {"step": step, "op": o["op"], "text": o.get("text"), "data": o.get("data") or {}, "images": [], "extra": {}}
            for i, ref in enumerate(o.get("images") or []):
                ext = EXT[fmt] if oi == last else "png"
                item["images"].append(add(ref["file_id"], f"o{step}-{i}.{ext}", fmt if oi == last else None))
            for ename, refs in (o.get("extra") or {}).items():
                for i, ref in enumerate(refs):
                    item["extra"].setdefault(ename, []).append(
                        add(ref["file_id"], f"o{step}-{i}-{slug(ename, 40 - len(f'o{step}-{i}-'))}.png"))
            layout["outputs"].append(item)
        steps = res.get("steps") or []
        gen = next((s for s in steps if s["op"] == "generate"), None)
        if a.kind != "pipeline" and len(steps) > 1 and gen and gen.get("images"):
            layout["raw"] = [add(fid, f"raw-{i}.png") for i, fid in enumerate(gen["images"])]
        return plan, layout

    def _timings(self, att: Attempt, job: dict, res: dict) -> Timings:
        ops = res.get("timings") or []
        run_s = sum(t["seconds"] for t in ops)
        started = job.get("started_at") or job.get("created_at") or 0
        return Timings.model_validate({
            "fetch_s": att.fetch_s, "local_queue_s": round(max(0.0, started - (job.get("created_at") or started)), 3),
            "run_s": round(run_s, 3), "upload_s": att.upload_s,
            "ops": [{"op": t["op"], "class": t.get("class"), "seconds": t["seconds"], "estimate": t.get("estimate"),
                     "kind": t.get("kind"), "calls": t.get("calls") or []} for t in ops]})

    # ---- upload
    async def _upload(self, att: Attempt, plan: list[Planned]) -> dict:
        t0 = time.monotonic()
        pending = {p.name: p for p in plan}
        granted: dict = {}
        while pending:
            grants = await self._request_grants(att, list(pending.values()))
            retry_later = False
            for name, p in list(pending.items()):
                g = grants.get(name)
                if g is None:
                    raise AttemptFailed(FailureCode.UPLOAD_FAILED, f"the hub did not grant an upload URL for {name}")
                outcome = await self._put(g, p)
                if outcome == "ok":
                    granted[name] = g
                    pending.pop(name)
                else:
                    retry_later = retry_later or outcome == "later"
                    break
            if not pending:
                break
            if time.monotonic() - t0 + self.regrant_s > self.upload_budget_s:
                raise AttemptFailed(FailureCode.UPLOAD_FAILED, "could not upload the outputs to storage in time")
            if retry_later:
                log.warning("attempt %s: storage unreachable, asking for fresh URLs in %.0fs", att.id, self.regrant_s)
                await asyncio.sleep(self.regrant_s)
        att.upload_s = round(time.monotonic() - t0, 3)
        return granted

    async def _request_grants(self, att: Attempt, files: list[Planned]) -> dict:
        req = UploadRequest(attempt_id=att.id, files=[UploadFileReq(name=p.name, content_type=p.content_type, bytes=len(p.data))
                                                       for p in files])
        while True:
            while not att.grants.empty():
                att.grants.get_nowait()
            if not self.link.is_open:
                await asyncio.sleep(0.2)     # nothing to ask yet: wait for the reconnect
                continue
            await self.link.send(req)
            try:
                msg = await asyncio.wait_for(att.grants.get(), self.grant_wait_s)
            except TimeoutError:
                continue                       # the request or the answer was lost: ask again
            if isinstance(msg, UploadDenied):
                if msg.reason == "too_large":
                    raise AttemptFailed(FailureCode.UPLOAD_FAILED, "the hub refused the outputs: too large", False)
                raise _Dropped()               # stale / canceled: the job is no longer ours
            return {g.name: g for g in msg.files}

    async def _put(self, grant, p: Planned) -> str:
        """PUT one file. -> "ok", "later" (storage unreachable: retry after a pause) or "regrant" (URL refused)."""
        for i, delay in enumerate((0.0, *self.put_retry_delays)):
            if delay:
                await asyncio.sleep(delay)
            try:
                r = await self.http.put(grant.url, content=p.data, headers=grant.headers)
            except (httpx.HTTPError, OSError) as e:
                log.warning("PUT %s failed (%s), try %d", p.name, type(e).__name__, i + 1)
                continue
            if r.status_code < 300:
                return "ok"
            if r.status_code in (400, 401, 403, 404):
                return "regrant"
            log.warning("PUT %s -> HTTP %s, try %d", p.name, r.status_code, i + 1)
        return "later"

    # ------------------------------------------------------------------ ending an attempt
    async def _finish(self, att: Attempt, result: Result) -> None:
        att.final, att.phase = result, "done"
        att.pct = 1.0
        await self._send_final(att)

    async def _fail(self, att: Attempt, e: AttemptFailed) -> None:
        log.info("attempt %s failed: %s (%s)", att.id, e.code, e.message)
        self.state.session_failed += 1
        att.final = Failure(attempt_id=att.id, code=str(e.code), message=e.message[:500], retryable=e.retryable,
                            timings={"fetch_s": att.fetch_s, "upload_s": att.upload_s})
        att.phase = "done"
        self._delete_local(att)
        await self._send_final(att)

    async def _send_final(self, att: Attempt) -> None:
        if att.final is None:
            return
        if await self.link.send(att.final):
            att.final_sent = True
            att.sent_at_ack = self.link.ack_count

    def on_result_ack(self, m: ResultAck) -> None:
        att = self.attempts.get(m.attempt_id)
        if att is None:
            return
        att.acked, att.done_at = True, time.monotonic()
        if m.accepted:
            self.state.session_done += 1
            # the hub holds the images now: keep the local copy only briefly (MESH.md 1.2 point 5)
            with contextlib.suppress(Exception):
                self.node.storage.set_expiry(att.local_files + att.out_files, 1)
        else:
            log.warning("hub did not accept the result of %s (%s)", att.id, m.reason)
            self._delete_local(att)

    def on_cancel(self, m: Cancel) -> None:
        att = self.attempts.get(m.attempt_id)
        if att is None or att.final is not None:
            return
        att.cancel_reason = m.reason
        att.cancel_ev.set()

    def _drop(self, att: Attempt) -> None:
        att.drop = True
        att.cancel_ev.set()
        if att.final is not None:
            att.acked, att.done_at = True, time.monotonic()
            self._delete_local(att)

    def _forget(self, att: Attempt) -> None:
        self._delete_local(att)
        self.attempts.pop(att.id, None)

    def _delete_local(self, att: Attempt) -> None:
        for fid in att.local_files + att.out_files:
            with contextlib.suppress(Exception):
                self.node.storage.delete(fid)
        att.local_files, att.out_files = [], []

    # ------------------------------------------------------------------ shutdown
    async def cancel_all(self) -> None:
        tasks = [a.task for a in self.attempts.values() if a.task and not a.task.done()]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(BaseException):
                await t
