"""Async client for a local ComfyUI: submit a workflow, follow it over the WebSocket, fetch the outputs.

One WebSocket per client (clientId), reconnected on demand. Completion is detected from WS messages with a
/history poll as a fallback, so a dropped socket never hangs a job.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx
import websockets

log = logging.getLogger("qis.comfy")

ProgressCb = Callable[[int, int, str | None], Awaitable[None] | None]
PreviewCb = Callable[[bytes, str], Awaitable[None] | None]


class ComfyError(Exception):
    """ComfyUI ran the workflow and reported an error."""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}

    @property
    def is_oom(self) -> bool:
        text = (str(self) + json.dumps(self.details)).lower()
        return "out of memory" in text or "outofmemory" in text or "allocation on device" in text


class ComfyRejected(ComfyError):
    """ComfyUI refused the workflow (validation error: unknown node, missing model file...)."""


class ComfyUnavailable(Exception):
    """ComfyUI is not reachable (not started, crashed, restarting)."""


class ComfyInterrupted(Exception):
    """The run was interrupted (cancel / timeout)."""


@dataclass
class _Run:
    prompt_id: str
    done: asyncio.Future
    on_progress: ProgressCb | None = None
    on_preview: PreviewCb | None = None
    outputs: dict = field(default_factory=dict)


async def _call(cb, *args):
    if cb is None:
        return
    res = cb(*args)
    if asyncio.iscoroutine(res):
        await res


class ComfyClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.client_id = uuid.uuid4().hex
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)
        self._runs: dict[str, _Run] = {}
        self._current: str | None = None  # prompt executing now (binary previews carry no prompt id)
        self._ws_task: asyncio.Task | None = None
        self._ws_ready = asyncio.Event()

    async def close(self) -> None:
        if self._ws_task:
            self._ws_task.cancel()
            with contextlib.suppress(BaseException):
                await self._ws_task
        await self._http.aclose()

    # ---------- plain HTTP ----------
    async def _req(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            return await self._http.request(method, path, **kw)
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectTimeout) as e:
            raise ComfyUnavailable(f"ComfyUI not reachable at {self.base_url}: {e}") from e

    async def system_stats(self) -> dict:
        r = await self._req("GET", "/system_stats", timeout=5)
        r.raise_for_status()
        return r.json()

    async def object_info(self, node: str | None = None) -> dict:
        r = await self._req("GET", f"/object_info/{node}" if node else "/object_info", timeout=60)
        r.raise_for_status()
        return r.json()

    async def upload(self, data: bytes, name: str) -> str:
        """Put an image in ComfyUI's input folder; returns the name LoadImage expects."""
        files = {"image": (name, data, "image/png")}
        r = await self._req("POST", "/upload/image", files=files, data={"overwrite": "true", "subfolder": "qis"}, timeout=120)
        r.raise_for_status()
        res = r.json()
        return f"{res['subfolder']}/{res['name']}" if res.get("subfolder") else res["name"]

    async def view(self, filename: str, subfolder: str, type_: str = "output") -> bytes:
        r = await self._req("GET", "/view", params={"filename": filename, "subfolder": subfolder, "type": type_}, timeout=120)
        r.raise_for_status()
        return r.content

    async def interrupt(self, prompt_id: str | None = None) -> None:
        with contextlib.suppress(ComfyUnavailable, httpx.HTTPError):
            if prompt_id:  # drop it from the pending queue too, in case it has not started
                await self._req("POST", "/queue", json={"delete": [prompt_id]})
            await self._req("POST", "/interrupt", json={"prompt_id": prompt_id} if prompt_id else {})

    async def free(self, unload_models: bool = True) -> None:
        with contextlib.suppress(ComfyUnavailable, httpx.HTTPError):
            await self._req("POST", "/free", json={"unload_models": unload_models, "free_memory": True})

    async def history(self, prompt_id: str) -> dict | None:
        r = await self._req("GET", f"/history/{prompt_id}")
        r.raise_for_status()
        return r.json().get(prompt_id)

    # ---------- WebSocket ----------
    async def _ensure_ws(self) -> None:
        if self._ws_task is None or self._ws_task.done():
            self._ws_ready.clear()
            self._ws_task = asyncio.create_task(self._ws_loop())
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._ws_ready.wait(), 5)

    async def _ws_loop(self) -> None:
        url = self.base_url.replace("http", "ws", 1) + f"/ws?clientId={self.client_id}"
        while True:
            try:
                async with websockets.connect(url, max_size=64 * 1024 * 1024, ping_interval=20) as ws:
                    self._ws_ready.set()
                    async for msg in ws:
                        if isinstance(msg, bytes):
                            await self._on_binary(msg)
                        else:
                            await self._on_message(json.loads(msg))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # reconnect; runs fall back to /history polling meanwhile
                log.debug("comfy ws dropped: %s", e)
                self._ws_ready.clear()
                if not self._runs:
                    return
                await asyncio.sleep(1)

    async def _on_message(self, m: dict) -> None:
        typ, data = m.get("type"), m.get("data") or {}
        pid = data.get("prompt_id")
        if typ == "execution_start":
            self._current = pid
        run = self._runs.get(pid) if pid else None
        if run is None:
            return
        if typ == "progress":
            await _call(run.on_progress, int(data.get("value", 0)), int(data.get("max", 0)), data.get("node"))
        elif typ == "executed":
            run.outputs[data.get("node")] = data.get("output") or {}
        elif typ in ("execution_success",) or (typ == "executing" and data.get("node") is None):
            if not run.done.done():
                run.done.set_result("success")
        elif typ == "execution_error":
            if not run.done.done():
                run.done.set_exception(ComfyError(data.get("exception_message") or "execution error", data))
        elif typ == "execution_interrupted":
            if not run.done.done():
                run.done.set_exception(ComfyInterrupted(pid))

    async def _on_binary(self, msg: bytes) -> None:
        if len(msg) < 8 or not self._current or self._current not in self._runs:
            return
        event = struct.unpack(">I", msg[:4])[0]
        run = self._runs[self._current]
        if event == 1:  # PREVIEW_IMAGE: [u32 image type][bytes]
            kind = struct.unpack(">I", msg[4:8])[0]
            await _call(run.on_preview, msg[8:], "image/png" if kind == 2 else "image/jpeg")
        elif event == 4:  # PREVIEW_IMAGE_WITH_METADATA: [u32 len][json][bytes]
            n = struct.unpack(">I", msg[4:8])[0]
            with contextlib.suppress(ValueError):
                meta = json.loads(msg[8:8 + n])
                await _call(run.on_preview, msg[8 + n:], meta.get("image_type", "image/jpeg"))

    # ---------- run ----------
    async def submit(self, workflow: dict, preview: bool = False) -> str:
        body = {"prompt": workflow, "client_id": self.client_id}
        if preview:
            body["extra_data"] = {"preview_method": "latent2rgb"}
        r = await self._req("POST", "/prompt", json=body)
        if r.status_code >= 400:
            try:
                err = r.json()
            except ValueError:
                err = {"error": r.text}
            msg = (err.get("error") or {}).get("message") if isinstance(err.get("error"), dict) else str(err.get("error"))
            raise ComfyRejected(f"ComfyUI rejected the workflow: {msg}", err)
        return r.json()["prompt_id"]

    async def run(self, workflow: dict, *, on_progress: ProgressCb | None = None, on_preview: PreviewCb | None = None,
                  on_submitted: Callable[[str], None] | None = None, poll_s: float = 2.0) -> dict:
        """Run a workflow to completion; returns {node_id: output} (images, text...)."""
        await self._ensure_ws()
        loop = asyncio.get_running_loop()
        pid = await self.submit(workflow, preview=on_preview is not None)
        run = _Run(pid, loop.create_future(), on_progress, on_preview)
        self._runs[pid] = run
        if on_submitted:
            on_submitted(pid)
        try:
            while True:
                try:
                    await asyncio.wait_for(asyncio.shield(run.done), poll_s)
                    break
                except TimeoutError:
                    # fallback: the socket may have missed the end of the run; a history entry only
                    # exists once the prompt finished (success, error or interrupt)
                    if await self.history(pid):
                        break
            if run.done.done() and run.done.exception():
                raise run.done.exception()
            # the history entry is authoritative for outputs (WS 'executed' may be missed on reconnect)
            h = await self.history(pid)
            outputs = (h or {}).get("outputs") or run.outputs
            status = (h or {}).get("status", {})
            if status.get("status_str") == "error":
                msgs = [x for x in status.get("messages", []) if x[0] == "execution_error"]
                detail = msgs[0][1] if msgs else {}
                raise ComfyError(detail.get("exception_message", "execution error"), detail)
            if any(x[0] == "execution_interrupted" for x in status.get("messages", [])):
                raise ComfyInterrupted(pid)
            return outputs
        finally:
            self._runs.pop(pid, None)
            if self._current == pid:
                self._current = None

    async def images(self, outputs: dict, node: str) -> list[bytes]:
        """Bytes of every image a node produced (SaveImage / PreviewImage)."""
        return [await self.view(img["filename"], img.get("subfolder", ""), img.get("type", "output"))
                for img in (outputs.get(node) or {}).get("images", [])]
