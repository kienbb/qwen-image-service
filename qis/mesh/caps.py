"""What this worker can do, as the hub needs to know it (docs/MESH.md sections 2.4 and 3.2): GPU, model file identities,
per-preset availability with a model fingerprint, limits, slots and the learnt time model.

Model identity: a file that matches models.yaml (same name and size) is identified by the manifest's sha256; any other
file (for example a turbo GGUF merged locally) is hashed once in the background and cached in <data>/model_hashes.json
by (path, bytes, mtime). Until the hash is known its identity is `name:bytes`. A preset's fingerprint is
"mf_" + sha256 of its sorted file identities: equal fingerprints produce equal images for equal seeds.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

from .. import __version__
from ..engine import hardware
from ..engine.constants import MODES
from ..engine.ops import OP_TYPES
from ..models import Manifest, models_root
from ..pipeline import JobSpec
from .perf import fit
from .protocol import (
    PROTO,
    GpuInfo,
    Hello,
    Limits,
    ModelFile,
    PerfModel,
    PresetInfo,
    RunningRef,
    Slots,
    UnackedRef,
)

log = logging.getLogger("qis.mesh.caps")

ROLE_DIRS = {"diffusion": "diffusion_models", "text_encoder": "text_encoders", "vae": "vae", "upscaler": "upscale_models"}
GPU_OPS = ("generate", "enhance_prompt", "refine", "outpaint")


class Capabilities:
    def __init__(self, node, manifest: Manifest | None = None):
        self.node = node
        self.cfg = node.cfg
        try:
            self.manifest = manifest or Manifest.load()
        except Exception as e:  # a broken manifest only costs us the sha256 shortcut
            log.warning("cannot read the model manifest: %s", e)
            self.manifest = Manifest()
        self.cache_path = self.cfg.data_dir / "model_hashes.json"
        self._cache: dict[str, dict] = {}
        with contextlib.suppress(OSError, ValueError):
            self._cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self._gpu: GpuInfo | None = None
        self._hash_task: asyncio.Task | None = None
        self._stop_hash = threading.Event()
        self.on_hashes_ready: Callable[[], None] | None = None

    # ------------------------------------------------------------------ model files
    def _root(self) -> Path | None:
        if not (self.cfg.paths.models or self.cfg.paths.comfyui):
            if not os.environ.get("QIS_MODELS_DIR"):
                return None          # attach / dev mode: no model folder known, trust the configuration
        root = models_root(self.cfg)
        return root if root.is_dir() else None

    def _configured(self) -> list[tuple[str, str | None, str | None, str]]:
        """(role, mode, upscaler key, file name) of every file the configuration names."""
        ms = self.cfg.models.model_set()
        out: list[tuple[str, str | None, str | None, str]] = [("diffusion", m, None, f) for m, f in ms.unets.items()]
        out.append(("text_encoder", None, None, ms.clip))
        out.append(("vae", None, None, ms.vae))
        out += [("upscaler", None, k, f) for k, f in ms.upscalers.items()]
        return out

    def model_files(self) -> list[ModelFile]:
        """Identity of every configured model file that is usable on this machine."""
        root = self._root()
        by_name = {m.file: m for m in self.manifest.models.values()}
        files: list[ModelFile] = []
        for role, mode, key, name in self._configured():
            size, mtime = 0, 0.0
            if root is not None:
                p = root / ROLE_DIRS[role] / name
                try:
                    st = p.stat()
                except OSError:
                    continue            # configured but not on disk: not usable here
                size, mtime = st.st_size, st.st_mtime
            else:
                p = Path(name)
            entry = by_name.get(name)
            kw = {k: v for k, v in (("mode", mode), ("key", key)) if v is not None}
            mf = ModelFile(role=role, file=name, bytes=size, manifest_id=None, sha256=None, **kw)
            if entry is not None and entry.bytes == size:
                mf.manifest_id, mf.sha256 = entry.id, entry.sha256
            elif root is not None:
                c = self._cache.get(str(p))
                if c and c.get("bytes") == size and c.get("mtime") == mtime:
                    mf.sha256 = c["sha256"]
            files.append(mf)
        return files

    @staticmethod
    def identity(mf: ModelFile) -> str:
        return f"{mf.role}:{mf.sha256}" if mf.sha256 else f"{mf.file}:{mf.bytes}"

    @staticmethod
    def fingerprint(files: list[ModelFile]) -> str:
        blob = "|".join(sorted(Capabilities.identity(f) for f in files))
        return "mf_" + hashlib.sha256(blob.encode()).hexdigest()[:16]

    def needed(self, ops: list[dict], files: list[ModelFile] | None = None) -> tuple[list[ModelFile], list[str]]:
        """Model files an op list loads, and descriptions of the ones this worker lacks."""
        files = files if files is not None else self.model_files()
        index = {(f.role, f.mode or f.key): f for f in files}
        used: dict[tuple, ModelFile] = {}
        missing: list[str] = []

        def want(role: str, key: str | None = None):
            mf = index.get((role, key))
            if mf is None:
                missing.append(f"{role}{':' + key if key else ''}")
            else:
                used[(role, key)] = mf

        for op in ops:
            name = op.get("op")
            if name in ("generate", "refine", "outpaint"):
                mode = "turbo" if name == "outpaint" else ((op.get("mode") if name == "generate" else op.get("engine")) or "turbo")
                want("diffusion", MODES[mode]["unet"] if mode in MODES else mode)
                want("text_encoder")
                want("vae")
                if name == "outpaint":
                    want("upscaler", op.get("upscaler") or "realesrgan-x4")
            elif name == "enhance_prompt":
                want("text_encoder")
            elif name == "upscale" and op.get("method", "esrgan") == "esrgan":
                key = op.get("model") or ("realesrgan-x4" if (op.get("scale") or 2) > 2.01 else "realesrgan-x2")
                if (("upscaler", key) not in index) and not op.get("model"):
                    key = next((k for (r, k) in index if r == "upscaler"), key)
                want("upscaler", key)
        order = {"diffusion": 0, "text_encoder": 1, "vae": 2, "upscaler": 3}
        return sorted(used.values(), key=lambda f: (order[f.role], f.file)), missing

    # ------------------------------------------------------------------ presets
    def presets(self) -> dict[str, PresetInfo]:
        """Presets this worker can run now (Presets.available + files present), with their fingerprints."""
        files = self.model_files()
        out: dict[str, PresetInfo] = {}
        for name, p in self.node.presets.available(self.cfg.models.model_set()).items():
            used, missing = self.needed(p["ops"], files)
            if missing:
                continue
            extra = {k: v for k, v in p.items() if k not in ("description", "ops")}
            out[name] = PresetInfo(description=p.get("description", ""), ops=p["ops"], files=[f.file for f in used],
                                   fingerprint=self.fingerprint(used), **extra)
        return out

    def spec_fingerprint(self, spec: JobSpec) -> str:
        ops = [o.model_dump() for o in spec.ops]
        used, _ = self.needed(ops)
        return self.fingerprint(used)

    def missing_for(self, spec: JobSpec) -> list[str]:
        return self.needed([o.model_dump() for o in spec.ops])[1]

    def op_names(self) -> list[str]:
        has_gpu = bool(self.cfg.models.model_set().unets)
        return [n for n in OP_TYPES if has_gpu or n not in GPU_OPS]

    # ------------------------------------------------------------------ background hashing (Q21)
    def pending_hashes(self) -> list[tuple[Path, ModelFile]]:
        root = self._root()
        if root is None:
            return []
        return [(root / ROLE_DIRS[f.role] / f.file, f) for f in self.model_files()
                if f.manifest_id is None and f.sha256 is None and f.bytes > 0]

    def start_hashing(self) -> None:
        if self._hash_task is None and self.pending_hashes():
            self._stop_hash.clear()
            self._hash_task = asyncio.create_task(self._hash_all(), name="qis-model-hash")

    async def stop_hashing(self) -> None:
        self._stop_hash.set()
        if self._hash_task:
            self._hash_task.cancel()
            with contextlib.suppress(BaseException):
                await self._hash_task
            self._hash_task = None

    async def _hash_all(self) -> None:
        done_any = False
        for path, mf in self.pending_hashes():
            log.info("hashing %s (%.1f GB) once to identify the model", mf.file, mf.bytes / 1024 ** 3)
            sha = await asyncio.to_thread(self._sha256, path)
            if sha is None:
                return
            st = path.stat()
            self._cache[str(path)] = {"bytes": st.st_size, "mtime": st.st_mtime, "sha256": sha}
            self._save_cache()
            done_any = True
        if done_any and self.on_hashes_ready:
            self.on_hashes_ready()

    def _sha256(self, path: Path) -> str | None:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while b := f.read(8 * 1024 * 1024):
                if self._stop_hash.is_set():
                    return None
                h.update(b)
        return h.hexdigest()

    def _save_cache(self) -> None:
        with contextlib.suppress(OSError):
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache, indent=1), encoding="utf-8")
            tmp.replace(self.cache_path)

    # ------------------------------------------------------------------ hello pieces
    async def gpu_info(self) -> GpuInfo:
        if self._gpu is None:
            gpus = await asyncio.to_thread(hardware.detect_gpus)
            g = next((x for x in gpus if x.index == self.cfg.gpu.device), gpus[0] if gpus else None)
            if g:
                self._gpu = GpuInfo(name=g.name, vram_mb=g.vram_mb, driver=g.driver, compute_cap=g.compute_cap)
            else:
                live = {**self.node.supervisor.gpu(), **self.node.gpu_info}
                total = live.get("memory_total") or live.get("vram_total") or 0
                return GpuInfo(name=live.get("name") or "unknown", vram_mb=int(total / 1024 ** 2),
                               driver=live.get("driver"))
        return self._gpu

    def limits(self) -> Limits:
        st = self.node.settings.current
        return Limits(max_n=st.api.max_n, max_refs=st.inputs.max_refs, max_pixels=st.inputs.max_pixels,
                      max_input_bytes=st.inputs.max_bytes)

    def slots(self) -> Slots:
        w = self.cfg.worker
        return Slots(gpu=1, prefetch=w.prefetch, cpu=w.cpu_slots)

    def perf_model(self) -> PerfModel:
        db = self.node.db
        rows = list(reversed(db.all("SELECT spec, result FROM jobs WHERE status='succeeded' AND result IS NOT NULL "
                                    "ORDER BY finished_at DESC LIMIT 400")))

        def size_of(fid: str):
            r = db.one("SELECT width, height FROM files WHERE id=?", (fid,))
            return (r["width"], r["height"]) if r and r.get("width") else None

        classes = fit(rows, size_of)
        perf = self.node.settings.current.perf
        fallback = {"overhead_s": perf.overhead_s, "s_per_step": perf.s_per_step, "ref_mp": perf.ref_mp,
                    "calib": perf.calib, "enhance_s": perf.enhance_s, "upscale_s_per_mp": perf.upscale_s_per_mp}
        return PerfModel(source="history" if classes else "calibration", classes=classes, fallback=fallback)

    async def hello(self, *, state: str, warm_eta_s: float | None, running: list[RunningRef],
                    unacked: list[UnackedRef], name: str) -> Hello:
        gpu = await self.gpu_info()
        ms = self.cfg.models.model_set()
        perf = await asyncio.to_thread(self.perf_model)
        presets = await asyncio.to_thread(self.presets)
        files = await asyncio.to_thread(self.model_files)
        return Hello(worker_id=self.node.node_id, name=name, version=__version__, proto=PROTO, gpu=gpu,
                     ram_mb=int(hardware.ram_gb() * 1024) or None, tier=self.cfg.profile.tier, model_files=files,
                     modes=ms.modes(), upscalers=list(ms.upscalers), ops=self.op_names(), presets=presets,
                     limits=self.limits(), slots=self.slots(), perf_model=perf, state=state, warm_eta_s=warm_eta_s,
                     loaded_model=self.node.executor.loaded_model, running=running, unacked=unacked)
