"""QIS mesh wire protocol, version 1 (docs/MESH.md section 2). THE source file: the hub repo copies it verbatim to
app/hub/protocol.py (first line there records the source commit). It imports only pydantic and the standard library.

Every frame is one JSON object: {"v", "type", "id", "ts", ...fields}. Unknown fields are ignored (forward
compatible); a missing required field makes `parse_message` raise ProtocolError(code="bad_message").
Enumerated values (reasons, codes, phases) are plain strings on the wire so that a newer peer never breaks an older
one; the StrEnum classes below list what version 1 defines.
"""
from __future__ import annotations

import json
import os
import re
import time
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PROTO = 1
PROTO_MIN = 1
PROTO_MAX = 1
MAX_FRAME_BYTES = 2 * 1024 * 1024
PREVIEW_MAX_BYTES = 256 * 1024
OUTPUT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,39}\.(png|webp|jpg|json|txt)$")

_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_message_id() -> str:
    """m_<ULID>: 48 bit millisecond time + 80 random bits, Crockford base32 (sortable, unique)."""
    n = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    return "m_" + "".join(_B32[(n >> (5 * i)) & 31] for i in reversed(range(26)))


class Close:
    """WebSocket close codes (section 2.7)."""
    NORMAL = 1000
    GOING_AWAY = 1001
    RESTART = 1012
    BAD_HELLO = 4400
    AUTH = 4401
    REPLACED = 4409
    PROTOCOL = 4426


class FailureCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    OOM = "oom"
    CANCELLED = "cancelled"
    ENGINE_UNAVAILABLE = "engine_unavailable"
    INPUT_FETCH_FAILED = "input_fetch_failed"
    UPLOAD_FAILED = "upload_failed"
    TIMEOUT = "timeout"
    INTERNAL = "internal"


class RejectReason(StrEnum):
    BUSY = "busy"
    DRAINING = "draining"
    PRESET_UNKNOWN = "preset_unknown"
    MODEL_MISSING = "model_missing"
    VRAM_INSUFFICIENT = "vram_insufficient"
    INVALID = "invalid"


class WorkerStateName(StrEnum):
    WARMING = "warming"
    READY = "ready"
    DRAINING = "draining"
    PAUSED = "paused"


class Phase(StrEnum):
    FETCHING = "fetching"
    QUEUED_LOCAL = "queued_local"
    RUNNING = "running"
    UPLOADING = "uploading"


class ByeReason(StrEnum):
    SHUTDOWN = "shutdown"
    LEAVE = "leave"
    DRAINED = "drained"


class CancelReason(StrEnum):
    CLIENT = "client"
    TIMEOUT = "timeout"
    SUPERSEDED = "superseded"
    ADMIN = "admin"


class DeniedReason(StrEnum):
    STALE = "stale"
    CANCELED = "canceled"
    TOO_LARGE = "too_large"


class AckReason(StrEnum):
    STALE = "stale"
    MISSING_OBJECT = "missing_object"
    SIZE_MISMATCH = "size_mismatch"
    BAD_KEY = "bad_key"


# ---------------------------------------------------------------- envelope
class Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class Message(Model):
    v: int = PROTO
    type: str
    id: str = Field(default_factory=new_message_id)
    ts: float = Field(default_factory=time.time)

    def to_wire(self) -> dict[str, Any]:
        """JSON-ready dict: envelope first, then only the fields that were set (round-trips fixtures exactly)."""
        body = self.model_dump(mode="json", by_alias=True, exclude_unset=True)
        head = {"v": self.v, "type": self.type, "id": self.id, "ts": self.ts}
        return {**head, **{k: v for k, v in body.items() if k not in head}}

    def to_json(self) -> str:
        return json.dumps(self.to_wire(), separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------- shared parts
class GpuInfo(Model):
    name: str
    vram_mb: int
    driver: str | None = None
    compute_cap: str | None = None


class ModelFile(Model):
    model_config = ConfigDict(extra="allow")
    role: str                       # diffusion | text_encoder | vae | upscaler
    file: str
    bytes: int
    mode: str | None = None         # diffusion files: turbo | base | base_q8
    key: str | None = None          # upscaler files: realesrgan-x2 ...
    manifest_id: str | None = None
    sha256: str | None = None


class PresetInfo(Model):
    model_config = ConfigDict(extra="allow")   # the definition from presets.yaml is kept as is
    description: str = ""
    ops: list[dict[str, Any]]
    files: list[str] = Field(default_factory=list)
    fingerprint: str


class Limits(Model):
    max_n: int
    max_refs: int
    max_pixels: int
    max_input_bytes: int


class Slots(Model):
    gpu: int
    prefetch: int
    cpu: int


class PerfClass(Model):
    a1: float
    aN: float
    b: float
    samples: int = 0


class PerfModel(Model):
    source: str = "history"
    classes: dict[str, PerfClass] = Field(default_factory=dict)
    fallback: dict[str, Any] = Field(default_factory=dict)


class RunningRef(Model):
    attempt_id: str
    phase: str | None = None
    pct: float | None = None


class UnackedRef(Model):
    attempt_id: str


class HeartbeatGpu(Model):
    util: float | None = None
    vram_used_mb: int | None = None
    vram_total_mb: int | None = None
    temp_c: float | None = None
    power_w: float | None = None


class UploadFileReq(Model):
    name: str
    content_type: str
    bytes: int

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not OUTPUT_NAME_RE.match(v):
            raise ValueError(f"file name {v!r} does not match {OUTPUT_NAME_RE.pattern}")
        return v


class ImageObj(Model):
    key: str
    content_type: str
    bytes: int
    width: int | None = None
    height: int | None = None
    has_alpha: bool = False
    sha256: str | None = None


class OutputItem(Model):
    step: int
    op: str
    images: list[ImageObj] = Field(default_factory=list)
    extra: dict[str, list[ImageObj]] = Field(default_factory=dict)
    text: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class CallTiming(Model):
    seconds: float
    sample_s: float = 0.0


class OpTiming(Model):
    op: str
    cls: str | None = Field(None, alias="class")
    seconds: float
    estimate: float | None = None
    kind: str | None = None
    calls: list[CallTiming] = Field(default_factory=list)


class Timings(Model):
    fetch_s: float = 0.0
    local_queue_s: float = 0.0
    run_s: float = 0.0
    upload_s: float = 0.0
    ops: list[OpTiming] = Field(default_factory=list)


class ResultMeta(Model):
    model_config = ConfigDict(extra="allow")
    revised_prompt: str | None = None
    seeds: list[int] = Field(default_factory=list)
    degraded: bool = False
    cold_start: bool = False
    model_fingerprint: str | None = None


class AssignInput(Model):
    file_id: str
    url: str
    bytes: int | None = None
    content_type: str | None = None
    width: int | None = None
    height: int | None = None


class OutputSpec(Model):
    format: str = "png"
    compression: int | None = None
    partial_images: int = 0


class GrantFile(Model):
    name: str
    key: str
    url: str
    method: str = "PUT"
    headers: dict[str, str] = Field(default_factory=dict)
    expires_at: float | None = None


# ---------------------------------------------------------------- worker -> hub
class Hello(Message):
    type: Literal["hello"] = "hello"
    worker_id: str
    name: str
    version: str
    proto: int = PROTO
    gpu: GpuInfo
    ram_mb: int | None = None
    tier: str | None = None
    model_files: list[ModelFile] = Field(default_factory=list)
    modes: list[str] = Field(default_factory=list)
    upscalers: list[str] = Field(default_factory=list)
    ops: list[str] = Field(default_factory=list)
    presets: dict[str, PresetInfo] = Field(default_factory=dict)
    limits: Limits | None = None
    slots: Slots | None = None
    perf_model: PerfModel | None = None
    state: str
    warm_eta_s: float | None = None
    loaded_model: str | None = None
    running: list[RunningRef] = Field(default_factory=list)
    unacked: list[UnackedRef] = Field(default_factory=list)


class Heartbeat(Message):
    type: Literal["heartbeat"] = "heartbeat"
    state: str
    running: list[RunningRef] = Field(default_factory=list)
    free_slots: Slots | None = None
    loaded_model: str | None = None
    gpu: HeartbeatGpu | None = None
    cpu_pct: float | None = None
    ram_used_mb: int | None = None
    comfy: str | None = None


class Accept(Message):
    type: Literal["accept"] = "accept"
    attempt_id: str


class Reject(Message):
    type: Literal["reject"] = "reject"
    attempt_id: str
    reason: str
    message: str = ""


class Progress(Message):
    type: Literal["progress"] = "progress"
    attempt_id: str
    phase: str
    op_index: int | None = None
    op: str | None = None
    step: int | None = None
    steps: int | None = None
    pct: float | None = None
    preview_jpeg_b64: str | None = None


class UploadRequest(Message):
    type: Literal["upload_request"] = "upload_request"
    attempt_id: str
    files: list[UploadFileReq]


class Result(Message):
    type: Literal["result"] = "result"
    attempt_id: str
    job_id: str
    outputs: list[OutputItem]
    raw: list[ImageObj] = Field(default_factory=list)
    meta: ResultMeta = Field(default_factory=ResultMeta)
    timings: Timings = Field(default_factory=Timings)
    worker_version: str | None = None


class Failure(Message):
    type: Literal["failure"] = "failure"
    attempt_id: str
    code: str
    message: str = ""
    retryable: bool = False
    timings: dict[str, Any] | None = None


class StateMsg(Message):
    type: Literal["state"] = "state"
    state: str
    reason: str | None = None
    warm_eta_s: float | None = None


class Bye(Message):
    type: Literal["bye"] = "bye"
    reason: str
    abandon: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- hub -> worker
class LimitsInfo(Model):
    max_frame_bytes: int = MAX_FRAME_BYTES


class Welcome(Message):
    type: Literal["welcome"] = "welcome"
    session_id: str
    heartbeat_s: float = 10
    lease_ttl_s: float = 45
    accept_timeout_s: float = 10
    paused: bool = False
    resume: dict[str, str] = Field(default_factory=dict)   # attempt_id -> "keep" | "drop"
    limits: LimitsInfo = Field(default_factory=LimitsInfo)


class HeartbeatAck(Message):
    type: Literal["heartbeat_ack"] = "heartbeat_ack"
    server_time: float | None = None


class Assign(Message):
    type: Literal["assign"] = "assign"
    attempt_id: str
    job_id: str
    attempt_no: int = 1
    kind: str                       # images.generations | images.edits | pipeline
    payload: dict[str, Any]
    inputs: list[AssignInput] = Field(default_factory=list)
    output: OutputSpec = Field(default_factory=OutputSpec)
    model_fingerprint: str | None = None
    est_s: float | None = None
    deadline_s: float | None = None
    priority: str = "interactive"


class UploadGrant(Message):
    type: Literal["upload_grant"] = "upload_grant"
    attempt_id: str
    files: list[GrantFile]


class UploadDenied(Message):
    type: Literal["upload_denied"] = "upload_denied"
    attempt_id: str
    reason: str


class ResultAck(Message):
    type: Literal["result_ack"] = "result_ack"
    attempt_id: str
    accepted: bool
    reason: str | None = None


class Cancel(Message):
    type: Literal["cancel"] = "cancel"
    attempt_id: str
    reason: str = "client"


class Pause(Message):
    type: Literal["pause"] = "pause"
    reason: str | None = None


class Resume(Message):
    type: Literal["resume"] = "resume"
    reason: str | None = None


class Error(Message):
    type: Literal["error"] = "error"
    reply_to: str | None = None
    code: str
    message: str = ""


KINDS = ("images.generations", "images.edits", "pipeline")

WORKER_TO_HUB: dict[str, type[Message]] = {m.model_fields["type"].default: m for m in (
    Hello, Heartbeat, Accept, Reject, Progress, UploadRequest, Result, Failure, StateMsg, Bye)}
HUB_TO_WORKER: dict[str, type[Message]] = {m.model_fields["type"].default: m for m in (
    Welcome, HeartbeatAck, Assign, UploadGrant, UploadDenied, ResultAck, Cancel, Pause, Resume, Error)}
MESSAGES: dict[str, type[Message]] = {**WORKER_TO_HUB, **HUB_TO_WORKER}


class ProtocolError(ValueError):
    """A frame that cannot be used. `code` is "bad_json", "bad_message" or "unknown_type"."""

    def __init__(self, code: str, message: str, reply_to: str | None = None):
        super().__init__(message)
        self.code, self.message, self.reply_to = code, message, reply_to


def parse_message(raw: str | bytes | dict) -> Message:
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise ProtocolError("bad_json", f"not valid JSON: {e}") from e
    else:
        data = raw
    if not isinstance(data, dict):
        raise ProtocolError("bad_message", "a frame must be a JSON object")
    typ = data.get("type")
    cls = MESSAGES.get(typ) if isinstance(typ, str) else None
    if cls is None:
        raise ProtocolError("unknown_type", f"unknown message type {typ!r}", data.get("id"))
    try:
        return cls.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0] if e.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []))
        raise ProtocolError("bad_message", f"{typ}: {loc}: {first.get('msg', 'invalid')}", data.get("id")) from e
