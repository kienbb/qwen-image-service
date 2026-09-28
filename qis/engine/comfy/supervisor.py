"""Runs ComfyUI as a child process bound to loopback, watches its health and restarts it when it dies.

With comfy.manage = false it only attaches to a ComfyUI that is already running (dev machines).
"""
from __future__ import annotations

import asyncio
import contextlib
import filecmp
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from ...config import Config
from ..workflows import GenParams, generate
from .client import ComfyClient, ComfyError, ComfyUnavailable

log = logging.getLogger("qis.comfy")

NODES_SRC = Path(__file__).resolve().parents[2] / "comfy_nodes"
REQUIRED_NODES = ["UnetLoaderGGUF", "TextEncodeQwenImage21", "QwenImage21Cache", "ViggleTurboSigmas", "PixelArtRetro",
                  "UpscaleModelLoader", "TextGenerate"]


def install_custom_nodes(comfy_dir: Path) -> list[str]:
    """Copy the vendored custom nodes into ComfyUI/custom_nodes (skips ones that already point at ours)."""
    changed = []
    dst_root = comfy_dir / "custom_nodes"
    dst_root.mkdir(parents=True, exist_ok=True)
    for src in NODES_SRC.iterdir():
        if src.name.startswith(("_", ".")):
            continue
        dst = dst_root / src.name
        with contextlib.suppress(OSError):
            if dst.exists() and dst.resolve() == src.resolve():
                continue
        if src.is_dir():
            for f in src.rglob("*"):
                if f.is_file() and "__pycache__" not in f.parts:
                    t = dst / f.relative_to(src)
                    if not t.exists() or not filecmp.cmp(f, t, shallow=False):
                        t.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, t)
                        changed.append(str(t))
        elif src.suffix == ".py" and (not dst.exists() or not filecmp.cmp(src, dst, shallow=False)):
            shutil.copy2(src, dst)
            changed.append(str(dst))
    return changed


class ComfySupervisor:
    def __init__(self, cfg: Config, client: ComfyClient):
        self.cfg = cfg
        self.client = client
        self.proc: subprocess.Popen | None = None
        self.state = "stopped"  # stopped | starting | ready | crashed | unavailable
        self.warm = False
        self.restarts = 0
        self.last_error = ""
        self.stats: dict = {}
        self.missing_nodes: list[str] = []
        self.ready = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._log_file = None

    # ---------- process ----------
    @property
    def managed(self) -> bool:
        return self.cfg.comfy.manage

    def _command(self) -> list[str]:
        c = self.cfg.comfy
        data = self.cfg.data_dir / "comfy"
        py = c.python or sys.executable
        cmd = [py, "-s", "main.py", "--listen", c.host, "--port", str(c.port), "--disable-auto-launch",
               "--output-directory", str(data / "output"), "--input-directory", str(data / "input"),
               "--temp-directory", str(data), "--cuda-device", str(self.cfg.gpu.device)]
        if c.reserve_vram:
            cmd += ["--reserve-vram", str(c.reserve_vram)]
        if sys.platform == "win32" and "python_embeded" in py:
            cmd.append("--windows-standalone-build")
        if self.cfg.paths.models:
            cmd += ["--extra-model-paths-config", str(self._write_model_paths())]
        return cmd + list(c.extra_args)

    def _write_model_paths(self) -> Path:
        """Point ComfyUI at the node's model root (kept outside the ComfyUI folder so updates never touch it)."""
        root = Path(self.cfg.paths.models)
        path = self.cfg.data_dir / "comfy" / "extra_model_paths.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        dirs = ["diffusion_models", "text_encoders", "vae", "upscale_models", "loras", "clip"]
        lines = ["qis:", f"  base_path: {json.dumps(str(root))}"] + [f"  {d}: {d}" for d in dirs]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        for d in dirs:
            (root / d).mkdir(parents=True, exist_ok=True)
        return path

    @property
    def _pid_file(self) -> Path:
        return self.cfg.data_dir / "comfy" / "comfyui.pid"

    def _kill_stale(self) -> None:
        """A ComfyUI left over from a gateway that was killed hard would hold the port: stop it first."""
        try:
            pid = int(self._pid_file.read_text().strip())
        except (OSError, ValueError):
            return
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=20)
            else:
                os.kill(pid, signal.SIGTERM)
                time.sleep(2)
            log.warning("stopped a stale ComfyUI (pid %d)", pid)
        except (OSError, subprocess.SubprocessError):
            pass
        self._pid_file.unlink(missing_ok=True)

    def _spawn(self) -> None:
        comfy_dir = Path(self.cfg.paths.comfyui)
        if not (comfy_dir / "main.py").exists():
            raise RuntimeError(f"ComfyUI not found at {comfy_dir} (paths.comfyui)")
        changed = install_custom_nodes(comfy_dir)
        if changed:
            log.info("updated %d custom node files", len(changed))
        logs = self.cfg.data_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / "comfyui.log"
        if path.exists() and path.stat().st_size > 20 * 1024 * 1024:
            for i in range(4, 0, -1):
                old = logs / f"comfyui.log.{i}"
                if old.exists():
                    old.replace(logs / f"comfyui.log.{i + 1}")
            path.replace(logs / "comfyui.log.1")
        if self._log_file:
            self._log_file.close()
        self._log_file = open(path, "ab")  # noqa: SIM115
        self._log_file.write(f"\n==== qis start {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n".encode())
        self._log_file.flush()
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        self._kill_stale()
        cmd = self._command()
        log.info("starting ComfyUI: %s", " ".join(cmd))
        self.proc = subprocess.Popen(cmd, cwd=comfy_dir, stdout=self._log_file, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, creationflags=flags)
        self._pid_file.parent.mkdir(parents=True, exist_ok=True)
        self._pid_file.write_text(str(self.proc.pid))

    def _kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self.proc = None
        self._pid_file.unlink(missing_ok=True)

    # ---------- lifecycle ----------
    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
        if self.managed:
            await asyncio.to_thread(self._kill)
        if self._log_file:
            self._log_file.close()
            self._log_file = None
        self.state = "stopped"

    async def restart(self) -> None:
        """Kill and respawn (dashboard / after a crash). Attach mode can only wait for it to come back."""
        self._set_down("restarting")
        if self.managed:
            await asyncio.to_thread(self._kill)

    def _set_down(self, state: str) -> None:
        self.state = state
        self.warm = False
        self.ready.clear()

    async def wait_ready(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self.ready.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def _loop(self) -> None:
        backoff = 2.0
        fails = 0
        while not self._stopping:
            if self.managed and (self.proc is None or self.proc.poll() is not None):
                if self.proc is not None:
                    self.restarts += 1
                    self.last_error = f"ComfyUI exited with code {self.proc.returncode}"
                    log.warning("%s, restarting in %.0fs", self.last_error, backoff)
                    self._set_down("crashed")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                try:
                    self.state = "starting"
                    await asyncio.to_thread(self._spawn)
                except Exception as e:
                    self.last_error = str(e)
                    self.state = "crashed"
                    log.error("cannot start ComfyUI: %s", e)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                if not await self._wait_up():
                    continue
                backoff = 2.0
            try:
                self.stats = await self.client.system_stats()
                fails = 0
                if not self.ready.is_set():
                    await self._on_up()
            except (ComfyUnavailable, Exception) as e:  # noqa: B014
                fails += 1
                if self.ready.is_set() and fails >= 3:
                    self.last_error = f"health check failed: {e}"
                    log.warning(self.last_error)
                    if self.managed:
                        self._set_down("crashed")
                        self.restarts += 1
                        await asyncio.to_thread(self._kill)
                        continue
                if not self.managed and fails >= 1:
                    self._set_down("unavailable")
            await asyncio.sleep(self.cfg.comfy.health_interval_s)

    async def _wait_up(self) -> bool:
        deadline = time.monotonic() + self.cfg.comfy.start_timeout_s
        while time.monotonic() < deadline and not self._stopping:
            if self.proc and self.proc.poll() is not None:
                return False
            try:
                self.stats = await self.client.system_stats()
                return True
            except Exception:
                await asyncio.sleep(1)
        self.last_error = "ComfyUI did not come up in time"
        await asyncio.to_thread(self._kill)
        return False

    async def _on_up(self) -> None:
        try:
            info = await self.client.object_info()
            self.missing_nodes = [n for n in REQUIRED_NODES if n not in info]
        except Exception as e:
            log.warning("object_info failed: %s", e)
        if self.missing_nodes:
            log.warning("ComfyUI is missing nodes: %s", ", ".join(self.missing_nodes))
        self.state = "ready"
        self.ready.set()
        log.info("ComfyUI ready (%s)", self.cfg.comfy_url)
        if self.cfg.comfy.warmup and not self.warm:
            asyncio.create_task(self.warmup())
        else:
            self.warm = True

    async def warmup(self) -> None:
        """Load the turbo model once (256 px, 4 steps) so the first real request skips the 60-90 s cold start."""
        models = self.cfg.models.model_set()
        if "turbo" not in models.unets or "ViggleTurboSigmas" in self.missing_nodes:
            self.warm = True
            return
        t = time.monotonic()
        try:
            await self.client.run(generate(GenParams(width=256, height=256, steps=4), "warm up", 1, [], models,
                                           prefix="qis/_warmup"))
            log.info("warm-up done in %.0fs", time.monotonic() - t)
        except (ComfyError, ComfyUnavailable, Exception) as e:  # noqa: B014
            log.warning("warm-up failed: %s", e)
        self.warm = True

    # ---------- info ----------
    def gpu(self) -> dict:
        dev = (self.stats.get("devices") or [{}])[0]
        return {"name": dev.get("name", ""), "vram_total": dev.get("vram_total", 0), "vram_free": dev.get("vram_free", 0),
                "torch_vram_total": dev.get("torch_vram_total", 0), "torch_vram_free": dev.get("torch_vram_free", 0)}

    def status(self) -> dict:
        return {"state": self.state, "warm": self.warm, "managed": self.managed, "restarts": self.restarts,
                "last_error": self.last_error, "missing_nodes": self.missing_nodes, "url": self.cfg.comfy_url,
                "version": (self.stats.get("system") or {}).get("comfyui_version", ""), "pid": self.proc.pid if self.proc else None}
