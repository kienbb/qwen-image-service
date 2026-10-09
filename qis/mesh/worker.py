"""WorkerService: ties the hub link, the capability report and the attempt runner to a Node (docs/MESH.md section 1.2).

Owns the worker-level behaviour: the `state` the hub sees (warming / ready / draining / paused), drain / resume /
leave, the heartbeat content, the graceful stop and the status blocks for /healthz, /admin/api/status and the tray.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from ..models import Manifest
from .caps import Capabilities
from .link import HubLink, Timing
from .protocol import (
    Bye,
    ByeReason,
    Close,
    Heartbeat,
    HeartbeatAck,
    HeartbeatGpu,
    Hello,
    Message,
    StateMsg,
    Welcome,
)
from .runner import AttemptRunner
from .state import WorkerState, normalize_hub_url

log = logging.getLogger("qis.mesh.worker")
MIB = 1024 * 1024


class WorkerService:
    def __init__(self, node, *, timing: Timing | None = None, manifest: Manifest | None = None):
        self.node = node
        cfg = node.cfg
        w = cfg.worker
        self.state = WorkerState(enabled=True, hub_url=normalize_hub_url(w.hub_url), worker_id=node.node_id,
                                 name=w.name or node.name)
        self.token = cfg.worker_token()
        self.caps = Capabilities(node, manifest)
        self.link = HubLink(self.state.hub_url, self.token, node.node_id, self.state, build_hello=self._hello,
                            on_message=self._on_message, on_session=self._on_session, on_session_end=self._on_session_end,
                            build_heartbeat=self._heartbeat, timing=timing or Timing(heartbeat_s=w.heartbeat_s))
        self.runner = AttemptRunner(node, self.link, self.caps, self.state, self.effective_state)
        self.shutdown_grace_s = 25.0     # WinSW allows 45 s to stop (service_install); leave room for the rest
        self._last_state = ""
        self._resync = False
        self._watch: asyncio.Task | None = None
        self._drain_task: asyncio.Task | None = None
        self._sampler = None
        self.caps.on_hashes_ready = self._hashes_ready

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if not self.token:
            self.state.sticky = "auth_failed"
            self.state.last_error = "no join token configured: run `qis worker join <hub_url> --token <token>`"
            log.error(self.state.last_error)
            return
        self.caps.start_hashing()
        await self.link.start()

    async def stop(self) -> None:
        self.runner.accepting = False
        deadline = time.monotonic() + self.shutdown_grace_s
        while time.monotonic() < deadline and (self.runner.unfinished_ids() or
                                               (self.runner.unacked_refs() and self.link.is_open)):
            await asyncio.sleep(0.2)
        if self.link.is_open:
            await self.link.send(Bye(reason=ByeReason.SHUTDOWN, abandon=self.runner.unfinished_ids()))
        for t in (self._drain_task, self._watch):
            if t:
                t.cancel()
                with contextlib.suppress(BaseException):
                    await t
        await self.link.stop()
        await self.runner.cancel_all()
        await self.runner.close()
        await self.caps.stop_hashing()

    # ------------------------------------------------------------------ what the hub sees
    def effective_state(self) -> str:
        s = self.state
        if s.draining:
            return "draining"
        if s.paused:
            return "paused"
        sup = self.node.supervisor
        return "ready" if sup.ready.is_set() and sup.warm else "warming"

    async def _hello(self) -> Hello:
        st = self.effective_state()
        self._last_state = st
        self._resync = False
        return await self.caps.hello(state=st, warm_eta_s=None, running=self.runner.running_refs(),
                                     unacked=self.runner.unacked_refs(), name=self.state.name)

    def _heartbeat(self) -> Heartbeat:
        n = self.node
        g, cg = n.gpu_info or {}, n.supervisor.gpu()
        total = g.get("memory_total") or cg.get("vram_total") or 0
        used = g.get("memory_used")
        if used is None and cg.get("vram_total"):
            used = cg["vram_total"] - cg.get("vram_free", 0)
        sysm = {}
        with contextlib.suppress(Exception):
            if self._sampler is None:
                from ..tray import SystemSampler
                self._sampler = SystemSampler()
            sysm = self._sampler.sample()
        gpu = HeartbeatGpu(util=g.get("util_gpu"), vram_used_mb=int(used / MIB) if used is not None else None,
                           vram_total_mb=int(total / MIB) if total else None, temp_c=g.get("temperature"),
                           power_w=g.get("power_w"))
        ram = sysm.get("ram_used")
        return Heartbeat(state=self.effective_state(), running=self.runner.running_refs(), free_slots=self.runner.free_slots(),
                         loaded_model=n.executor.loaded_model, gpu=gpu, cpu_pct=sysm.get("cpu_pct"),
                         ram_used_mb=int(ram / MIB) if ram else None, comfy=n.supervisor.state)

    # ------------------------------------------------------------------ link callbacks
    async def _on_message(self, msg: Message) -> None:
        if isinstance(msg, HeartbeatAck):
            self.runner.on_heartbeat_ack()
            return
        await self.runner.handle(msg)

    async def _on_session(self, welcome: Welcome) -> None:
        await self.runner.on_session(welcome)
        if self._watch is None or self._watch.done():
            self._watch = asyncio.create_task(self._watcher(), name="qis-worker-watch")

    async def _on_session_end(self) -> None:
        await self.runner.on_session_end()

    async def _watcher(self) -> None:
        """Tell the hub right away when the state changes (heartbeats are 10 s apart); re-hello when the
        capabilities changed (model hashes finished) and nothing is running."""
        while True:
            await asyncio.sleep(0.25)
            if not self.link.is_open:
                continue
            st = self.effective_state()
            if st != self._last_state:
                reason = {"draining": "operator", "paused": "hub", "warming": "engine", "ready": "engine"}[st]
                if await self.link.send(StateMsg(state=st, reason=reason)):
                    self._last_state = st
            if self._resync and self.runner.idle():
                self._resync = False
                log.info("model hashes are known now: reconnecting to publish the new fingerprints")
                self.link.request_reconnect()

    def _hashes_ready(self) -> None:
        self._resync = True

    # ------------------------------------------------------------------ operator actions
    async def drain(self, wait: bool = False, timeout: float | None = None, reason: str = ByeReason.DRAINED) -> dict:
        """Stop accepting jobs, finish the running ones, then say `bye` and disconnect."""
        st = self.state
        if not st.draining or reason == ByeReason.LEAVE:
            st.draining = True
            if self._drain_task and not self._drain_task.done():
                self._drain_task.cancel()
            self._drain_task = asyncio.create_task(self._drain_monitor(str(reason)), name="qis-worker-drain")
        if wait:
            end = time.monotonic() + (timeout if timeout is not None else 3600)
            while not st.parked and time.monotonic() < end:
                await asyncio.sleep(0.2)
        return self.status()

    async def _drain_monitor(self, reason: str) -> None:
        st = self.state
        while not self.runner.idle():
            await asyncio.sleep(0.2)
            if not st.draining:
                return
        if not st.draining:
            return
        if self.link.is_open:
            await self.link.send(Bye(reason=reason))
        st.parked = True
        self.link.park()
        await self.link.close(Close.NORMAL, reason)
        log.info("drained (%s): disconnected from the hub", reason)

    async def resume(self) -> dict:
        st = self.state
        st.draining = False
        if self._drain_task and not self._drain_task.done():
            self._drain_task.cancel()
        if st.parked:
            st.parked = False
            self.link.unpark()
        return self.status()

    async def leave(self, timeout: float | None = 120) -> dict:
        return await self.drain(wait=True, timeout=timeout, reason=ByeReason.LEAVE)

    # ------------------------------------------------------------------ status blocks
    def status(self) -> dict:
        return self.state.snapshot(running=self.runner.status_running(), unacked=len(self.runner.unacked_refs()),
                                   state=self.effective_state())

    def health(self) -> dict:
        return self.state.health()
