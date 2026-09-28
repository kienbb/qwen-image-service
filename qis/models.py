"""Model manifest, tier profiles, resumable verified downloads and writing the chosen profile into config.toml."""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import httpx
import yaml

from .config import Config

log = logging.getLogger("qis.models")
ROLE_FIELD = {"turbo": "turbo", "base": "base", "base_q8": "base_q8", "clip": "clip", "vae": "vae"}


@dataclass
class ModelEntry:
    id: str
    role: str
    file: str
    dir: str
    url: str
    sha256: str
    bytes: int
    min_vram_gb: float = 0
    license: str = ""
    commercial_ok: bool = False
    note: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Manifest:
    models: dict[str, ModelEntry] = field(default_factory=dict)
    profiles: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path | None = None) -> Manifest:
        text = Path(path).read_text(encoding="utf-8") if path else \
            resources.files("qis.data").joinpath("models.yaml").read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        models = {m["id"]: ModelEntry(**m) for m in data["models"]}
        for name, p in data["profiles"].items():
            for mid in p["models"]:
                if mid not in models:
                    raise ValueError(f"profile {name} references unknown model {mid}")
        return cls(models, data["profiles"])

    def profile_models(self, tier: str, variant: str = "recommended") -> list[str]:
        p = self.profiles[tier]
        ids = list(p["models"])
        mod = p.get(variant) or {}
        for role, mid in mod.items():
            if role == "drop":
                ids = [i for i in ids if i not in mid]
                continue
            ids = [i for i in ids if self.models[i].role != role] + [mid]
        return ids

    def total_bytes(self, ids: list[str]) -> int:
        return sum(self.models[i].bytes for i in ids)


def models_root(cfg: Config) -> Path:
    if env := os.environ.get("QIS_MODELS_DIR"):
        return Path(env)
    return Path(cfg.paths.models) if cfg.paths.models else Path(cfg.paths.comfyui) / "models"


def installed(cfg: Config, manifest: Manifest) -> list[dict]:
    root = models_root(cfg)
    active = set(cfg.models.model_set().unets.values()) | {cfg.models.clip, cfg.models.vae} | set(cfg.models.upscalers.values())
    out = []
    for m in manifest.models.values():
        p = root / m.dir / m.file
        size = p.stat().st_size if p.exists() else 0
        # present = usable file on disk; matches_manifest = the exact published build (a local build of the same
        # name, e.g. a self-baked GGUF, is still usable but is not the manifest's file)
        out.append({**m.to_dict(), "path": str(p), "present": size > 0, "matches_manifest": size == m.bytes,
                    "partial": p.with_name(p.name + ".part").exists(), "size_on_disk": size, "active": m.file in active})
    # files configured but not in the manifest (e.g. a self-baked GGUF)
    known = {m.file for m in manifest.models.values()}
    for role, fname in [("turbo", cfg.models.turbo), ("base", cfg.models.base), ("base_q8", cfg.models.base_q8),
                        ("clip", cfg.models.clip), ("vae", cfg.models.vae)]:
        if fname and fname not in known:
            sub = {"clip": "text_encoders", "vae": "vae"}.get(role, "diffusion_models")
            p = root / sub / fname
            out.append({"id": f"custom:{fname}", "role": role, "file": fname, "dir": sub, "url": "", "sha256": "",
                        "bytes": p.stat().st_size if p.exists() else 0, "path": str(p), "present": p.exists(),
                        "partial": False, "active": True, "license": "unknown", "commercial_ok": False, "note": "not in manifest"})
    return out


def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


class DownloadError(RuntimeError):
    pass


def download(m: ModelEntry, root: Path, progress: Callable[[int, int], None] | None = None,
             hf_token: str | None = None, verify: bool = True, reuse_dirs: list[Path] | None = None,
             cancel: Callable[[], bool] | None = None) -> Path:
    """Resumable download into root/<dir>/<file> with sha256 check; reuses an identical file found elsewhere."""
    dest = root / m.dir / m.file
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size == m.bytes and (not verify or sha256_file(dest) == m.sha256):
        if progress:
            progress(m.bytes, m.bytes)
        return dest
    for d in reuse_dirs or []:
        for cand in list(Path(d).rglob(m.file)) if Path(d).exists() else []:
            if cand.stat().st_size == m.bytes and (not verify or sha256_file(cand) == m.sha256):
                log.info("reusing %s", cand)
                try:
                    os.link(cand, dest)
                except OSError:
                    shutil.copy2(cand, dest)
                if progress:
                    progress(m.bytes, m.bytes)
                return dest
    part = dest.with_name(dest.name + ".part")
    headers = {"User-Agent": "qwen-image-service"}
    token = hf_token or os.environ.get("HF_TOKEN")
    if token and "huggingface.co" in m.url:
        headers["Authorization"] = f"Bearer {token}"
    h = hashlib.sha256()
    done = 0
    if part.exists():
        done = part.stat().st_size
        if done > m.bytes:
            part.unlink()
            done = 0
        else:
            with open(part, "rb") as f:
                while b := f.read(8 * 1024 * 1024):
                    h.update(b)
    for attempt in range(5):
        if done:
            headers["Range"] = f"bytes={done}-"
        try:
            with httpx.stream("GET", m.url, headers=headers, follow_redirects=True, timeout=httpx.Timeout(60, read=120)) as r:
                if r.status_code == 416 and done == m.bytes:
                    break
                if r.status_code in (401, 403):
                    raise DownloadError(f"{m.url}: HTTP {r.status_code} (gated or private: set HF_TOKEN)")
                if r.status_code not in (200, 206):
                    raise DownloadError(f"{m.url}: HTTP {r.status_code}")
                if r.status_code == 200 and done:  # server ignored Range: restart
                    done, h = 0, hashlib.sha256()
                    part.unlink(missing_ok=True)
                last = 0.0
                with open(part, "ab") as f:
                    for chunk in r.iter_bytes(4 * 1024 * 1024):
                        if cancel and cancel():
                            raise DownloadError("canceled")
                        f.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        if progress and time.monotonic() - last > 0.5:
                            progress(done, m.bytes)
                            last = time.monotonic()
            break
        except (httpx.HTTPError, OSError) as e:
            if attempt == 4:
                raise DownloadError(f"{m.file}: {e}") from e
            log.warning("download %s interrupted (%s), resuming", m.file, e)
            time.sleep(2 * (attempt + 1))
    if done != m.bytes:
        raise DownloadError(f"{m.file}: got {done} bytes, expected {m.bytes}")
    if verify and h.hexdigest() != m.sha256:
        part.unlink(missing_ok=True)
        raise DownloadError(f"{m.file}: sha256 mismatch (file removed, retry the download)")
    part.replace(dest)
    if progress:
        progress(m.bytes, m.bytes)
    return dest


# ------------------------------------------------------------------ config.toml writing
def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def dump_toml(data: dict) -> str:
    lines: list[str] = []
    for section, values in data.items():
        if not isinstance(values, dict):
            continue
        simple = {k: v for k, v in values.items() if not isinstance(v, dict) and v is not None}
        nested = {k: v for k, v in values.items() if isinstance(v, dict)}
        lines.append(f"[{section}]")
        lines += [f"{k} = {_toml_value(v)}" for k, v in simple.items()]
        lines.append("")
        for k, sub in nested.items():
            lines.append(f"[{section}.{k}]")
            lines += [f'"{kk}" = {_toml_value(vv)}' for kk, vv in sub.items()]
            lines.append("")
    return "\n".join(lines)


def profile_config(manifest: Manifest, ids: list[str], tier: str) -> dict:
    """[models] / [profile] / comfy.reserve_vram values for a set of model ids."""
    models: dict = {"turbo": None, "base": None, "base_q8": None, "upscalers": {}}
    for i in ids:
        m = manifest.models[i]
        if m.role == "upscaler":
            models["upscalers"][m.id] = m.file
        else:
            models[ROLE_FIELD[m.role]] = m.file
    return {"models": models, "profile": {"tier": tier, "model_ids": ids},
            "comfy": {"reserve_vram": manifest.profiles[tier].get("comfy", {}).get("reserve_vram", 1.0)}}


def write_config(path: Path, updates: dict) -> None:
    """Merge section updates into config.toml (keeps the other keys)."""
    import tomllib
    data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for section, values in updates.items():
        cur = data.setdefault(section, {})
        for k, v in values.items():
            if v is None:
                cur.pop(k, None)
            else:
                cur[k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("# Qwen Image Service node config (changes need a restart; live settings are in the dashboard)\n\n"
                   + dump_toml(data), encoding="utf-8")
    tmp.replace(path)
