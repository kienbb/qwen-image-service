"""Runtime settings: validated by schema, stored in the settings table, applied without a restart.

Groups live in scope 'node' (this machine) or 'cluster' (shared by a future mesh: key defaults, presets).
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable

from pydantic import BaseModel, Field, ValidationError

from .db import DB

log = logging.getLogger("qis.settings")


class QueueS(BaseModel):
    max_pending: int = Field(20, ge=1, le=10000, description="Job GPU tối đa đang chờ; vượt thì trả 429 queue_full")
    target_wait_s: int = Field(600, ge=30, description="Thời gian chờ mục tiêu, dùng để tính max_pending mặc định sau benchmark")
    max_wait_s: int = Field(1800, ge=30, description="Từ chối khi tổng ETA của queue vượt ngưỡng này")
    max_queue_age_s: int = Field(3600, ge=60, description="Job chờ quá lâu chuyển sang expired")
    cpu_max_pending: int = Field(100, ge=1, description="Job chỉ dùng CPU (pixelize, compose...) tối đa đang chờ")


class SchedulerS(BaseModel):
    interactive_weight: float = Field(100, description="Điểm cộng cho priority interactive")
    aging_per_min: float = Field(5, ge=0, description="Điểm cộng mỗi phút chờ (chống đói)")
    affinity_bonus: float = Field(30, ge=0, description="Điểm cộng khi job dùng model đang nạp")
    fair_penalty: float = Field(10, ge=0, description="Điểm trừ cho mỗi job của cùng key đã chạy trong cửa sổ fair-share")
    fair_window_s: int = Field(300, ge=10)


class CpuS(BaseModel):
    workers: int = Field(default_factory=lambda: max(1, (os.cpu_count() or 2) // 2), ge=1, le=64,
                         description="Số process cho op CPU (pixelize, remove-bg, compose, fidelity)")


class ApiS(BaseModel):
    default_preset: str = "qwen-image-2.1-turbo"
    sync_keepalive: bool = Field(True, description="Gửi khoảng trắng mỗi 15 s cho request đồng bộ (tránh 524 của Cloudflare)")
    sync_max_s: int = Field(90, ge=10, description="Khi tắt keepalive: job ước tính dài hơn thì trả 400 use_async")
    keepalive_interval_s: int = Field(15, ge=5, le=90)
    max_n: int = Field(8, ge=1, le=16)
    allow_private_webhooks: bool = Field(False, description="Cho phép webhook tới IP nội bộ / localhost (mặc định chặn, chống SSRF)")
    max_body_mb: int = Field(64, ge=1, le=1024, description="Kích thước tối đa của một request body")


class InputsS(BaseModel):
    max_bytes: int = Field(20 * 1024 * 1024, ge=1024)
    max_pixels: int = Field(16_000_000, ge=65536)
    allow_url: bool = Field(False, description="Cho phép ảnh đầu vào dạng URL https (có chặn IP nội bộ)")
    max_refs: int = Field(3, ge=1, le=8)


class StorageS(BaseModel):
    input_hours: float = Field(24, gt=0)
    output_hours: float = Field(72, gt=0)
    intermediate_hours: float = Field(1, gt=0)
    url_ttl_s: int = Field(3600, ge=60)
    max_gb: float = Field(50, gt=0)


class JobS(BaseModel):
    timeout_min_s: int = Field(300, ge=30)
    timeout_factor: float = Field(5, ge=1)
    max_cost_s: int = Field(3600, ge=10, description="Job ước tính vượt mức này bị từ chối (400)")
    max_attempts: int = Field(3, ge=1, le=10)


class PerfS(BaseModel):
    """Cost model inputs; calibrated from real runs (EWMA)."""
    s_per_step: dict[str, float] = Field(default_factory=lambda: {"turbo": 3.8, "base": 4.0, "base_q8": 5.5})
    ref_mp: float = 0.59                   # megapixels the s_per_step numbers were measured at (768x768)
    overhead_s: float = 6.0                # encode + decode + transfer per generate
    enhance_s: float = 110.0
    upscale_s_per_mp: float = 3.0          # ESRGAN, per output megapixel
    cpu_op_s: float = 1.0
    calib: dict[str, float] = Field(default_factory=dict)  # op -> actual/estimated (EWMA)
    avg_job_s: float = 30.0


class KeysS(BaseModel):
    default_max_pending: int = Field(10, ge=1)
    default_rpm: int = Field(30, ge=1)


class NodeS(BaseModel):
    name: str = ""
    public_url: str = Field("", description="URL công khai (hostname Cloudflare); ưu tiên hơn config.toml")


class RuntimeSettings(BaseModel):
    queue: QueueS = Field(default_factory=QueueS)
    scheduler: SchedulerS = Field(default_factory=SchedulerS)
    cpu: CpuS = Field(default_factory=CpuS)
    api: ApiS = Field(default_factory=ApiS)
    inputs: InputsS = Field(default_factory=InputsS)
    storage: StorageS = Field(default_factory=StorageS)
    job: JobS = Field(default_factory=JobS)
    perf: PerfS = Field(default_factory=PerfS)
    keys: KeysS = Field(default_factory=KeysS)
    node: NodeS = Field(default_factory=NodeS)


SCOPES = {"keys": "cluster"}  # every other group is per node
RESTART_REQUIRED = {"cpu.workers"}


class SettingsError(ValueError):
    pass


class Settings:
    def __init__(self, db: DB):
        self.db = db
        self.current = RuntimeSettings()
        self._listeners: list[Callable[[RuntimeSettings, set[str]], None]] = []
        self.reload()

    def reload(self) -> None:
        data = self.current.model_dump()
        for row in self.db.all("SELECT scope, key, value FROM settings"):
            if row["key"] in data and isinstance(row["value"], dict):
                data[row["key"]].update(row["value"])
        try:
            self.current = RuntimeSettings.model_validate(data)
        except ValidationError as e:  # a bad stored value must not keep the node from starting
            log.error("stored settings are invalid, using defaults for the broken groups: %s", e)

    def on_change(self, fn: Callable[[RuntimeSettings, set[str]], None]) -> None:
        self._listeners.append(fn)

    def update(self, group: str, values: dict, actor: str = "system") -> RuntimeSettings:
        if group not in RuntimeSettings.model_fields:
            raise SettingsError(f"unknown settings group {group!r}")
        merged = {**getattr(self.current, group).model_dump(), **values}
        try:
            model = RuntimeSettings.model_fields[group].annotation.model_validate(merged)
        except ValidationError as e:
            raise SettingsError(str(e)) from e
        stored = self.db.one("SELECT value FROM settings WHERE scope=? AND key=?", (SCOPES.get(group, "node"), group))
        full = {**((stored or {}).get("value") or {}), **model.model_dump(include=set(merged))}
        self.db.execute("INSERT INTO settings(scope, key, value, updated_by, updated_at) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(scope, key) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by, "
                        "updated_at=excluded.updated_at", (SCOPES.get(group, "node"), group, full, actor, time.time()))
        old = self.current
        self.current = old.model_copy(update={group: model})
        changed = {f"{group}.{k}" for k, v in model.model_dump().items() if getattr(getattr(old, group), k) != v}
        for fn in self._listeners:
            try:
                fn(self.current, changed)
            except Exception:  # a listener bug must not undo a saved setting
                log.exception("settings listener failed")
        return self.current

    def schema(self) -> dict:
        s = RuntimeSettings.model_json_schema()
        s["x-restart-required"] = sorted(RESTART_REQUIRED)
        s["x-scopes"] = {g: SCOPES.get(g, "node") for g in RuntimeSettings.model_fields}
        return s
