"""FakeHub: an in-process QIS Hub speaking the worker protocol (docs/MESH.md section 10.1, item 4).

A FastAPI app served by uvicorn inside the test's event loop: `/worker/ws` is the WebSocket, `GET/PUT /blob/{key}` plays
the object store (the "presigned" URLs are just URLs to this app). Tests script it:

    hub = FakeHub(); await hub.start()
    await hub.wait_connected()
    a = await hub.assign(request={"model": "qwen-image-2.1-turbo", "prompt": "a knight"})
    result = await hub.expect("result", attempt_id=a.attempt_id)
    hub.blob(result.outputs[0].images[0].key)

By default it behaves like a polite hub: welcomes, acks heartbeats, grants every upload, acks results after checking
the objects exist with the right size. The `auto_*` flags and `*_fail` counters switch that off.
"""
from __future__ import annotations

import asyncio
import contextlib
import time

import uvicorn
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect

from qis.ids import new_id
from qis.mesh.protocol import (
    PROTO_MAX,
    PROTO_MIN,
    Assign,
    AssignInput,
    Cancel,
    Close,
    Failure,
    GrantFile,
    HeartbeatAck,
    Hello,
    Message,
    OutputSpec,
    Pause,
    ProtocolError,
    Result,
    ResultAck,
    UploadDenied,
    UploadGrant,
    UploadRequest,
    Welcome,
    parse_message,
)

from .fake_comfy import free_port


class FakeHub:
    def __init__(self, tokens: tuple[str, ...] = ("qisw_test",)):
        self.port = free_port()
        self.tokens = set(tokens)
        self.proto_range = (PROTO_MIN, PROTO_MAX)
        self.heartbeat_s = 0.3
        self.hello_timeout = 10.0
        # behaviour switches
        self.auto_welcome = True
        self.auto_heartbeat_ack = True
        self.auto_grant = True
        self.auto_result_ack = True
        self.welcome_paused = False
        self.resume: dict[str, str] = {}            # what `welcome.resume` answers per attempt id
        self.put_fail = 0                           # next N PUTs answer 503
        self.put_status = 503
        self.deny_upload: str | None = None         # answer upload_request with upload_denied(<reason>)
        # observations
        self.messages: list[Message] = []
        self.sessions: list[dict] = []
        self.hellos: list[Hello] = []
        self.blobs: dict[str, bytes] = {}
        self.blob_types: dict[str, str] = {}
        self.assigns: dict[str, Assign] = {}
        self.upgrade_headers: dict[str, str] = {}
        self.puts = 0
        self.ws: WebSocket | None = None
        self.server: uvicorn.Server | None = None
        self._serve: asyncio.Task | None = None
        self._new = asyncio.Event()
        self._consumed: set[int] = set()
        self.app = self._build()

    # ------------------------------------------------------------------ addresses
    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/worker/ws"

    @property
    def http(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ------------------------------------------------------------------ server
    def _build(self) -> FastAPI:
        app = FastAPI()
        hub = self

        @app.websocket("/worker/ws")
        async def worker_ws(ws: WebSocket):
            await ws.accept()
            hub.upgrade_headers = dict(ws.headers)
            token = ws.headers.get("authorization", "")[7:]
            if token not in hub.tokens:
                await ws.close(Close.AUTH)
                return
            try:
                first = await asyncio.wait_for(ws.receive_text(), hub.hello_timeout)
                hello = parse_message(first)
            except (TimeoutError, ProtocolError, WebSocketDisconnect):
                with contextlib.suppress(Exception):
                    await ws.close(Close.BAD_HELLO)
                return
            if not isinstance(hello, Hello):
                await ws.close(Close.BAD_HELLO)
                return
            if not hub.proto_range[0] <= hello.v <= hub.proto_range[1]:
                await ws.close(Close.PROTOCOL)
                return
            old = hub.ws
            if old is not None:
                with contextlib.suppress(Exception):
                    await old.close(Close.REPLACED)
            hub.ws = ws
            session = {"id": new_id("ses"), "hello": hello, "t": time.time(), "closed": None}
            hub.sessions.append(session)
            hub.hellos.append(hello)
            hub._push(hello)
            if hub.auto_welcome:
                await hub.send(Welcome(session_id=session["id"], heartbeat_s=hub.heartbeat_s, paused=hub.welcome_paused,
                                       resume={a.attempt_id: hub.resume[a.attempt_id] for a in hello.running
                                               if a.attempt_id in hub.resume}))
            try:
                while True:
                    msg = parse_message(await ws.receive_text())
                    hub._push(msg)
                    await hub._auto(msg)
            except WebSocketDisconnect as e:
                session["closed"] = e.code
            except Exception as e:  # keep the fake alive whatever the worker sends
                session["closed"] = repr(e)
            finally:
                if hub.ws is ws:
                    hub.ws = None

        @app.put("/blob/{key:path}")
        async def put_blob(key: str, request: Request):
            hub.puts += 1
            if hub.put_fail > 0:
                hub.put_fail -= 1
                return Response(status_code=hub.put_status)
            hub.blobs[key] = await request.body()
            hub.blob_types[key] = request.headers.get("content-type", "")
            return Response(status_code=200)

        @app.get("/blob/{key:path}")
        async def get_blob(key: str):
            if key not in hub.blobs:
                return Response(status_code=404)
            return Response(hub.blobs[key], media_type=hub.blob_types.get(key, "application/octet-stream"))

        return app

    async def start(self) -> FakeHub:
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off")
        self.server = uvicorn.Server(cfg)
        self._serve = asyncio.create_task(self.server.serve())
        for _ in range(200):
            if self.server.started:
                return self
            await asyncio.sleep(0.02)
        raise RuntimeError("fake hub did not start")

    async def stop(self) -> None:
        if self.server:
            self.server.should_exit = True
        if self._serve:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(self._serve, 5)

    # ------------------------------------------------------------------ scripted behaviour
    def _push(self, msg: Message) -> None:
        self.messages.append(msg)
        self._new.set()

    async def _auto(self, msg: Message) -> None:
        if msg.type == "heartbeat" and self.auto_heartbeat_ack:
            await self.send(HeartbeatAck(server_time=time.time()))
        elif isinstance(msg, UploadRequest) and self.auto_grant:
            a = self.assigns.get(msg.attempt_id)
            if self.deny_upload:
                await self.send(UploadDenied(attempt_id=msg.attempt_id, reason=self.deny_upload))
                return
            prefix = f"out/test/{a.job_id if a else 'job_x'}/a{a.attempt_no if a else 1}"
            await self.send(UploadGrant(attempt_id=msg.attempt_id, files=[
                GrantFile(name=f.name, key=f"{prefix}/{f.name}", url=f"{self.http}/blob/{prefix}/{f.name}", method="PUT",
                          headers={"Content-Type": f.content_type}, expires_at=time.time() + 900) for f in msg.files]))
        elif isinstance(msg, Result) and self.auto_result_ack:
            await self.send(self.check_result(msg))

    def check_result(self, r: Result) -> ResultAck:
        keys = [(i.key, i.bytes) for o in r.outputs for i in o.images + [x for v in o.extra.values() for x in v]]
        keys += [(i.key, i.bytes) for i in r.raw]
        for key, size in keys:
            if key not in self.blobs:
                return ResultAck(attempt_id=r.attempt_id, accepted=False, reason="missing_object")
            if len(self.blobs[key]) != size:
                return ResultAck(attempt_id=r.attempt_id, accepted=False, reason="size_mismatch")
        return ResultAck(attempt_id=r.attempt_id, accepted=True)

    async def send(self, msg: Message) -> bool:
        ws = self.ws
        if ws is None:
            return False
        try:
            await ws.send_text(msg.to_json())
            return True
        except Exception:
            return False

    def put_input(self, file_id: str, data: bytes) -> str:
        self.blobs[f"in/{file_id}"] = data
        self.blob_types[f"in/{file_id}"] = "image/png"
        return f"{self.http}/blob/in/{file_id}"

    async def assign(self, *, kind: str = "images.generations", request: dict | None = None, spec: dict | None = None,
                     inputs: dict[str, bytes] | None = None, input_urls: dict[str, str] | None = None,
                     output: dict | None = None, priority: str = "interactive", attempt_no: int = 1,
                     attempt_id: str | None = None, job_id: str | None = None, fingerprint: str | None = None,
                     send: bool = True) -> Assign:
        """Give the connected worker an attempt. `inputs` = {hub_file_id: png bytes} (served from /blob)."""
        urls = {fid: self.put_input(fid, data) for fid, data in (inputs or {}).items()}
        urls.update(input_urls or {})
        payload: dict = {}
        if kind == "pipeline":
            payload["spec"] = spec or {}
        else:
            payload["request"] = request or {}
            if kind == "images.edits":
                payload["refs"] = list(urls)
        a = Assign(attempt_id=attempt_id or new_id("att"), job_id=job_id or new_id("job"), attempt_no=attempt_no, kind=kind,
                   payload=payload, inputs=[AssignInput(file_id=f, url=u, content_type="image/png") for f, u in urls.items()],
                   output=OutputSpec(**(output or {})), model_fingerprint=fingerprint, est_s=30, deadline_s=300,
                   priority=priority)
        self.assigns[a.attempt_id] = a
        if send and not await self.send(a):
            raise AssertionError("no worker connected")
        return a

    async def cancel(self, attempt_id: str, reason: str = "client") -> None:
        await self.send(Cancel(attempt_id=attempt_id, reason=reason))

    async def pause(self) -> None:
        await self.send(Pause(reason="admin"))

    async def close(self, code: int = Close.NORMAL) -> None:
        if self.ws is not None:
            await self.ws.close(code)

    def drop_connection(self) -> None:
        """Kill the TCP connection without a close handshake (network cable pulled)."""
        for c in list(self.server.server_state.connections):
            with contextlib.suppress(Exception):
                c.transport.abort()

    def blob(self, key: str) -> bytes:
        return self.blobs[key]

    # ------------------------------------------------------------------ waiting
    def of_type(self, typ: str, attempt_id: str | None = None) -> list[Message]:
        return [m for m in self.messages if m.type == typ and (attempt_id is None or getattr(m, "attempt_id", None) == attempt_id)]

    async def expect(self, typ: str, *, timeout: float = 20, attempt_id: str | None = None, where=None) -> Message:
        """Next not-yet-consumed message of this type (optionally for one attempt / matching `where`)."""
        end = time.monotonic() + timeout
        while True:
            for m in self.messages:
                if id(m) in self._consumed or m.type != typ:
                    continue
                if attempt_id is not None and getattr(m, "attempt_id", None) != attempt_id:
                    continue
                if where is not None and not where(m):
                    continue
                self._consumed.add(id(m))
                return m
            left = end - time.monotonic()
            if left <= 0:
                raise AssertionError(f"no {typ!r} within {timeout}s; received {[m.type for m in self.messages][-12:]}")
            self._new.clear()
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(self._new.wait(), left)

    async def wait_for(self, predicate, timeout: float = 20, what: str = "condition") -> None:
        end = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > end:
                raise AssertionError(f"timed out waiting for {what}")
            await asyncio.sleep(0.05)

    async def wait_connected(self, timeout: float = 20, sessions: int = 1) -> Hello:
        await self.wait_for(lambda: len(self.sessions) >= sessions and self.ws is not None, timeout, "a worker session")
        return self.hellos[sessions - 1]

    async def wait_for_failure(self, attempt_id: str, timeout: float = 30) -> Failure:
        return await self.expect("failure", attempt_id=attempt_id, timeout=timeout)  # type: ignore[return-value]
