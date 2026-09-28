"""Static node configuration (config.toml). Changing it needs a restart; live knobs live in qis.settings."""
from __future__ import annotations

import os
import secrets
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

from .engine.constants import ModelSet


class ServerCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080
    public_url: str = ""  # e.g. https://img.example.com (the Cloudflare hostname); used for signed URLs


class PathsCfg(BaseModel):
    data: str = ""        # sqlite, blobs, logs; default <config dir>/../data or ./.qis-dev
    comfyui: str = ""     # directory that holds ComfyUI's main.py
    models: str = ""      # model root (diffusion_models/, text_encoders/, vae/, upscale_models/); default <comfyui>/models


class ComfyCfg(BaseModel):
    manage: bool = True               # spawn + supervise ComfyUI; False = attach to one already running
    python: str = ""                  # interpreter for ComfyUI (default: the gateway's)
    host: str = "127.0.0.1"
    port: int = 8188
    reserve_vram: float = 1.2
    extra_args: list[str] = Field(default_factory=list)
    warmup: bool = True
    health_interval_s: float = 5.0
    start_timeout_s: float = 300.0


class GpuCfg(BaseModel):
    device: int = 0


class SecurityCfg(BaseModel):
    trust_proxy_from: list[str] = Field(default_factory=lambda: ["127.0.0.1", "::1"])
    cors_origins: list[str] = Field(default_factory=list)


class ProfileCfg(BaseModel):
    tier: str = "T1"
    model_ids: list[str] = Field(default_factory=list)


class ModelsCfg(BaseModel):
    turbo: str | None = "qwen-image-2.1-viggle-Q4_K_M.gguf"
    base: str | None = "qwen-image-2.1-Q4_K_M.gguf"
    base_q8: str | None = None
    clip: str = "qwen3vl_8b_int8_convrot.safetensors"
    vae: str = "qwen_image_2.1_vae_bf16.safetensors"
    upscalers: dict[str, str] = Field(default_factory=lambda: {
        "realesrgan-x2": "RealESRGAN_x2plus.pth", "realesrgan-x4": "RealESRGAN_x4plus.pth"})

    def model_set(self) -> ModelSet:
        unets = {k: v for k, v in (("turbo", self.turbo), ("base", self.base), ("base_q8", self.base_q8)) if v}
        return ModelSet(unets=unets, clip=self.clip, vae=self.vae, upscalers=dict(self.upscalers))


class Config(BaseModel):
    server: ServerCfg = Field(default_factory=ServerCfg)
    paths: PathsCfg = Field(default_factory=PathsCfg)
    comfy: ComfyCfg = Field(default_factory=ComfyCfg)
    gpu: GpuCfg = Field(default_factory=GpuCfg)
    security: SecurityCfg = Field(default_factory=SecurityCfg)
    profile: ProfileCfg = Field(default_factory=ProfileCfg)
    models: ModelsCfg = Field(default_factory=ModelsCfg)
    source: str = ""  # path the config was read from

    @property
    def data_dir(self) -> Path:
        return Path(self.paths.data).expanduser().resolve()

    @property
    def comfy_url(self) -> str:
        return f"http://{self.comfy.host}:{self.comfy.port}"

    def secret(self) -> bytes:
        """Per-node secret for HMAC (signed URLs, webhooks, key pepper); created on first use."""
        path = self.data_dir / "secret.key"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(secrets.token_hex(32), encoding="utf-8")
            try:
                path.chmod(0o600)
            except OSError:
                pass
        return bytes.fromhex(path.read_text(encoding="utf-8").strip())


def default_config_path() -> Path | None:
    if env := os.environ.get("QIS_CONFIG"):
        return Path(env)
    for cand in (Path.cwd() / "config.toml", Path.cwd() / "config" / "config.toml"):
        if cand.exists():
            return cand
    return None


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path) if path else default_config_path()
    data: dict = {}
    if path and path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    cfg = Config.model_validate(data)
    cfg.source = str(path or "")
    base = path.parent if path else Path.cwd()
    if not cfg.paths.data:
        cfg.paths.data = str((base / ("../data" if base.name == "config" else ".qis-dev")).resolve())
    elif not Path(cfg.paths.data).is_absolute():
        cfg.paths.data = str((base / cfg.paths.data).resolve())
    for attr in ("comfyui", "models"):
        v = getattr(cfg.paths, attr)
        if v and not Path(v).is_absolute():
            setattr(cfg.paths, attr, str((base / v).resolve()))
    if port := os.environ.get("QIS_PORT"):
        cfg.server.port = int(port)
    return cfg
