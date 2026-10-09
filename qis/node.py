"""The node: wires config, storage, the ComfyUI supervisor, the executor and the job queue together."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import __version__
from .auth import ApiKeys, Sessions, Users
from .config import Config
from .db import DB
from .engine import hardware
from .engine.comfy.client import ComfyClient
from .engine.comfy.supervisor import ComfySupervisor
from .events import EventBus
from .executor import LocalComfyExecutor
from .ids import new_id
from .presets import Presets
from .scheduler import JobService
from .settings import Settings
from .storage import Storage

log = logging.getLogger("qis.node")


def load_identity(data_dir: Path) -> dict:
    """node_id + ed25519 key pair, created on first start (peer auth for the mesh, DESIGN §15)."""
    path = data_dir / "node.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    key = Ed25519PrivateKey.generate()
    ident = {
        "node_id": new_id("node"),
        "private_key": base64.b64encode(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                          serialization.NoEncryption())).decode(),
        "public_key": base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,
                                                                     serialization.PublicFormat.Raw)).decode(),
        "created_at": time.time(),
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ident, indent=1), encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return ident


class Node:
    def __init__(self, cfg: Config, *, client: ComfyClient | None = None, cpu_pool=None, start_comfy: bool = True):
        self.cfg = cfg
        self.started_at = time.time()
        data = cfg.data_dir
        data.mkdir(parents=True, exist_ok=True)
        self.identity = load_identity(data)
        self.node_id = self.identity["node_id"]
        self.db = DB(data / "qis.db")
        self.settings = Settings(self.db)
        self.secret = cfg.secret()
        self.storage = Storage(self.db, data / "blobs", self.secret)
        self.events = EventBus(self.db)
        self.keys = ApiKeys(self.db, self.secret)
        self.users = Users(self.db)
        self.sessions = Sessions(self.db)
        cfg_dir = Path(cfg.source).parent if cfg.source else data
        self.presets = Presets(cfg_dir / "presets.yaml" if cfg.source else data / "presets.yaml")
        self.client = client or ComfyClient(cfg.comfy_url)
        self.supervisor = ComfySupervisor(cfg, self.client)
        self.start_comfy = start_comfy
        self.cpu_pool = cpu_pool or self._make_pool()
        self.executor = LocalComfyExecutor(cfg, self.db, self.settings, self.storage, self.client, self.supervisor, self.cpu_pool)
        self.jobs = JobService(self.db, self.settings, self.events, self.executor, self.storage, self.node_id)
        self._tasks: list[asyncio.Task] = []
        self.gpu_info: dict = {}
        self.worker = None  # WorkerService when [worker] enabled = true (docs/MESH.md)
        if cfg.worker.enabled:
            from .mesh.worker import WorkerService
            self.worker = WorkerService(self)
        if not self.db.one("SELECT id FROM nodes WHERE id=?", (self.node_id,)):
            self.db.insert("nodes", {"id": self.node_id, "name": self.settings.current.node.name or hardware.hostname(),
                                     "public_url": cfg.server.public_url, "pubkey": self.identity["public_key"],
                                     "created_at": time.time()})

    def _make_pool(self):
        workers = self.settings.current.cpu.workers
        try:
            return ProcessPoolExecutor(max_workers=workers)
        except (OSError, NotImplementedError):  # restricted environments
            return ThreadPoolExecutor(max_workers=workers)

    @property
    def public_url(self) -> str:
        return (self.settings.current.node.public_url or self.cfg.server.public_url).rstrip("/")

    @property
    def name(self) -> str:
        return self.settings.current.node.name or hardware.hostname()

    async def start(self) -> None:
        if self.start_comfy:
            await self.supervisor.start()
        await self.jobs.start()
        if self.worker:
            await self.worker.start()
        self._tasks.append(asyncio.create_task(self._housekeeping()))
        self._tasks.append(asyncio.create_task(self._gpu_sampler()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(BaseException):
                await t
        if self.worker:   # before the engine goes away, so running jobs can finish and the hub hears `bye`
            await self.worker.stop()
        await self.jobs.stop()
        if self.start_comfy:
            await self.supervisor.stop()
        await self.client.close()
        self.cpu_pool.shutdown(wait=False, cancel_futures=True)
        self.db.close()

    async def _housekeeping(self) -> None:
        last_backup = 0.0
        while True:
            try:
                st = self.settings.current.storage
                res = await asyncio.to_thread(self.storage.cleanup, int(st.max_gb * 1024 ** 3))
                if any(res.values()):
                    log.info("storage cleanup: %s", res)
                self._clean_comfy_dirs()
                self.db.execute("DELETE FROM stats_minute WHERE ts < ?", (time.time() - 30 * 86400,))
                self.db.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
                self.db.execute("DELETE FROM job_events WHERE job_id IN (SELECT id FROM jobs WHERE finished_at < ?)",
                                (time.time() - 7 * 86400,))
                if time.time() - last_backup > 86400:
                    await asyncio.to_thread(self.backup)
                    last_backup = time.time()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("housekeeping failed")
            await asyncio.sleep(600)

    def _clean_comfy_dirs(self) -> None:
        """ComfyUI writes every output/upload to disk; the service keeps its own copy, so drop old ones."""
        cutoff = time.time() - 3600
        for sub in ("output/qis", "input/qis", "temp"):
            root = self.cfg.data_dir / "comfy" / sub
            if not root.exists():
                continue
            for f in root.rglob("*"):
                with contextlib.suppress(OSError):
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink()

    def backup(self) -> Path:
        dest_dir = self.cfg.data_dir / "backups"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"qis-{time.strftime('%Y%m%d-%H%M%S')}.db"
        self.db.backup(dest)
        for old in sorted(dest_dir.glob("qis-*.db"))[:-7]:
            with contextlib.suppress(OSError):
                old.unlink()
        return dest

    async def _gpu_sampler(self) -> None:
        while True:
            try:
                self.gpu_info = await asyncio.to_thread(hardware.nvml_sample, self.cfg.gpu.device)
                if self.events.admin_watchers:
                    self.events.admin({"type": "node.status", "data": self.status()})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("gpu sample failed: %s", e)
            await asyncio.sleep(2 if self.events.admin_watchers else 30)

    def status(self) -> dict:
        return {"node_id": self.node_id, "name": self.name, "version": __version__, "uptime_s": round(time.time() - self.started_at),
                "comfy": self.supervisor.status(), "gpu": {**self.supervisor.gpu(), **self.gpu_info},
                "queue": self.jobs.queue_stats(), "loaded_model": self.executor.loaded_model,
                "worker": self.worker.status() if self.worker else {"enabled": False, "link": "disabled"}}

    @property
    def worker_blocks_local_jobs(self) -> bool:
        """Worker mode: the job-creating /v1 routes answer 409 worker_mode (MESH.md section 1.2 point 3)."""
        return self.cfg.worker.enabled and not self.cfg.worker.accept_local_jobs

    def info(self) -> dict:
        """GET /v1/node/info: the capability contract other nodes of a mesh will read (versioned)."""
        models = self.cfg.models.model_set()
        perf = self.settings.current.perf
        return {
            "schema": 1, "node_id": self.node_id, "name": self.name, "version": __version__,
            "public_key": self.identity["public_key"], "public_url": self.public_url,
            "gpu": {**self.supervisor.gpu(), **{k: v for k, v in self.gpu_info.items() if k in ("name", "memory_total", "driver")}},
            "profile": self.cfg.profile.tier,
            "models": {"unets": models.unets, "clip": models.clip, "vae": models.vae, "upscalers": models.upscalers},
            "presets": sorted(self.presets.available(models)),
            "queue": self.jobs.queue_stats(),
            "perf": {"s_per_step": perf.s_per_step, "avg_job_s": perf.avg_job_s, "calib": perf.calib},
            "status": self.supervisor.state, "warm": self.supervisor.warm,
        }
