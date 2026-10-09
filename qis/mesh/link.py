"""HubLink: the outbound WebSocket to the hub (docs/MESH.md sections 2.1-2.7).

Dials, authenticates with the join token, sends `hello`, waits for `welcome`, then reads frames until the socket
dies. Reconnects forever with full-jitter backoff and the close-code rules of section 2.7. Everything the link does
not understand itself (assign, cancel, upload_grant, ...) goes to the `on_message` callback; the service layer
decides what to send in `hello` and in each heartbeat.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI

from .protocol import (
    MAX_FRAME_BYTES,
    Close,
    Error,
    Heartbeat,
    HeartbeatAck,
    Hello,
    Message,
    Pause,
    ProtocolError,
    Resume,
    Welcome,
    parse_message,
)
from .state import WorkerState

log = logging.getLogger("qis.mesh.link")


@dataclass
class Timing:
    """All the waits of section 2.7 in one place so tests can shrink them."""
    heartbeat_s: float = 10.0
    rx_timeout_s: float = 30.0        # no frame from the hub for this long: drop the socket and reconnect
    welcome_timeout_s: float = 10.0
    connect_timeout_s: float = 10.0
    ping_interval_s: float = 20.0
    backoff_cap_s: float = 60.0
    stable_s: float = 60.0            # a session this old resets the backoff
    restart_delay: tuple[float, float] = (1.0, 3.0)   # after 1000 / 1001 / 1012
    auth_retry_s: float = 300.0       # 4401
    replaced_wait_s: float = 60.0     # 4409
    outdated_retry_s: float = 600.0   # 4426


class HubLink:
    def __init__(self, url: str, token: str, worker_id: str, state: WorkerState, *,
                 build_hello: Callable[[], Awaitable[Hello]],
                 on_message: Callable[[Message], Awaitable[None]],
                 on_session: Callable[[Welcome], Awaitable[None]],
                 on_session_end: Callable[[], Awaitable[None]] | None = None,
                 build_heartbeat: Callable[[], Heartbeat] | None = None,
                 timing: Timing | None = None, rng: random.Random | None = None):
        self.url, self.token, self.worker_id, self.state = url, token, worker_id, state
        self.build_hello, self.on_message, self.on_session = build_hello, on_message, on_session
        self.on_session_end, self.build_heartbeat = on_session_end, build_heartbeat
        self.t = timing or Timing()
        self.rng = rng or random.Random()
        self.welcome: Welcome | None = None
        self.ack_count = 0
        self.sessions = 0
        self._ws = None
        self._task: asyncio.Task | None = None
        self._hb_task: asyncio.Task | None = None
        self._stop = False
        self._wake = asyncio.Event()      # cuts a retry sleep short
        self._reconnect = False           # ask the current session to end (capabilities changed)
        self._k = 0                       # consecutive failures, for the backoff

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._stop = False
        self._task = asyncio.create_task(self._run(), name="qis-hub-link")

    async def stop(self) -> None:
        self._stop = True
        self._wake.set()
        await self.close(Close.NORMAL, "shutdown")
        if self._task:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task

    @property
    def is_open(self) -> bool:
        return self._ws is not None and self.state.connected

    async def send(self, msg: Message) -> bool:
        """Send one frame; False when there is no session (callers keep what matters in their own outbox)."""
        ws = self._ws
        if ws is None or not self.state.connected:
            return False
        try:
            await ws.send(msg.to_json())
            return True
        except ConnectionClosed:
            return False

    async def close(self, code: int = Close.NORMAL, reason: str = "") -> None:
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close(code, reason)

    def park(self) -> None:
        """Stay disconnected on purpose (drained, left) until `unpark`."""
        self.state.parked = True

    def unpark(self) -> None:
        self.state.parked = False
        self._k = 0
        self._wake.set()

    def request_reconnect(self) -> None:
        """End the current session softly and dial again (new `hello`), e.g. after model hashes finished."""
        self._reconnect = True
        ws = self._ws
        if ws is not None:
            asyncio.ensure_future(self.close(Close.NORMAL, "resync"))

    # ------------------------------------------------------------------ main loop
    async def _run(self) -> None:
        st = self.state
        while not self._stop:
            if st.parked:
                st.retry_at_mono = None
                await self._sleep(3600)
                continue
            started = time.monotonic()
            code: int | None = None
            try:
                code = await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let the loop die: log and back off
                log.exception("hub link crashed")
                st.last_error = f"{type(e).__name__}: {e}"
            finally:
                await self._after_session()
            if self._stop:
                break
            if self._reconnect:
                self._reconnect = False
                continue
            if st.parked:
                continue
            if st.connected_since is not None and time.monotonic() - started >= self.t.stable_s:
                self._k = 0
            delay = self._delay_for(code)
            st.retry_at_mono = time.monotonic() + delay
            log.info("hub link down (%s), retrying in %.1fs", st.last_error or code, delay)
            await self._sleep(delay)
            st.retry_at_mono = None

    async def _sleep(self, delay: float) -> None:
        self._wake.clear()
        with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(self._wake.wait(), delay)

    def _delay_for(self, code: int | None) -> float:
        st, t = self.state, self.t
        if code == Close.AUTH:
            st.sticky = "auth_failed"
            st.last_error = "join token rejected by the hub (close 4401): run `qis worker join` with a new token"
            return t.auth_retry_s
        if code == Close.PROTOCOL:
            st.sticky = "outdated"
            st.last_error = "the hub does not support this protocol version (close 4426): run `qis update`"
            return t.outdated_retry_s
        if code == Close.REPLACED:
            st.replaced_count += 1
            st.last_error = "replaced by another session with the same worker_id (is the data folder copied?)"
            return t.replaced_wait_s
        if code in (Close.NORMAL, Close.GOING_AWAY, Close.RESTART):
            if not st.last_error:
                st.last_error = f"hub closed the connection ({code})"
            return self.rng.uniform(*t.restart_delay)
        # 4400, abnormal closes, connection errors: full jitter, delay = random(0, min(cap, 2^k))
        ceiling = min(t.backoff_cap_s, 2.0 ** self._k)
        self._k += 1
        return self.rng.uniform(0, ceiling)

    # ------------------------------------------------------------------ one session
    async def _session(self) -> int | None:
        st, t = self.state, self.t
        headers = {"Authorization": f"Bearer {self.token}", "X-Qis-Worker-Id": self.worker_id}
        self.welcome = None
        try:
            async with connect(self.url, additional_headers=headers, open_timeout=t.connect_timeout_s,
                               ping_interval=t.ping_interval_s, ping_timeout=t.ping_interval_s,
                               max_size=MAX_FRAME_BYTES + 4096, close_timeout=3) as ws:
                self._ws = ws
                await ws.send((await self.build_hello()).to_json())
                welcome = await self._expect_welcome(ws)
                self._on_welcome(welcome)
                await self.on_session(welcome)
                self._hb_task = asyncio.create_task(self._heartbeats(welcome.heartbeat_s), name="qis-hub-heartbeat")
                try:
                    await self._read_loop(ws)
                finally:
                    self._hb_task.cancel()
                    with contextlib.suppress(BaseException):
                        await self._hb_task
        except ConnectionClosed as e:
            code = e.rcvd.code if e.rcvd else (e.sent.code if e.sent else 1006)
            st.last_close_code = code
            if code not in (Close.AUTH, Close.PROTOCOL, Close.REPLACED):
                st.last_error = f"connection closed ({code}{': ' + e.rcvd.reason if e.rcvd and e.rcvd.reason else ''})"
            return code
        except InvalidStatus as e:
            status = e.response.status_code
            st.last_error = f"hub answered HTTP {status} to the upgrade request"
            return Close.AUTH if status in (401, 403) else None
        except (OSError, TimeoutError, InvalidURI) as e:
            st.last_error = f"cannot reach the hub: {type(e).__name__}: {e}".rstrip(": ")
            return None
        except _SessionEnd as e:
            if str(e):
                st.last_error = str(e)
            return e.code
        return Close.NORMAL

    async def _expect_welcome(self, ws) -> Welcome:
        deadline = time.monotonic() + self.t.welcome_timeout_s
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise _SessionEnd(None, "no welcome from the hub in time")
            try:
                raw = await asyncio.wait_for(ws.recv(), left)
            except TimeoutError:
                raise _SessionEnd(None, "no welcome from the hub in time") from None
            try:
                msg = parse_message(raw)
            except ProtocolError as e:
                log.warning("bad frame while waiting for welcome: %s", e)
                continue
            if isinstance(msg, Welcome):
                return msg
            if isinstance(msg, Error):
                self.state.last_error = f"hub error {msg.code}: {msg.message}"

    def _on_welcome(self, w: Welcome) -> None:
        st = self.state
        now = time.monotonic()
        self.welcome = w
        self.sessions += 1
        st.connected, st.sticky = True, ""
        st.session_id, st.connected_since = w.session_id, time.time()
        st.last_rx_mono = st.last_ack_mono = now
        st.retry_at_mono = None
        st.paused = bool(w.paused)
        st.last_close_code = None
        log.info("connected to the hub (session %s)", w.session_id)

    async def _after_session(self) -> None:
        st = self.state
        was = st.connected
        st.connected = False
        self._ws = None
        if was:
            st.disconnected_mono = time.monotonic()   # never connected: "disconnected" counts from process start
        if was and self.on_session_end:
            with contextlib.suppress(Exception):
                await self.on_session_end()

    async def _read_loop(self, ws) -> None:
        st, t = self.state, self.t
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), t.rx_timeout_s)
            except TimeoutError:
                st.last_error = f"no frame from the hub for {t.rx_timeout_s:.0f}s, reconnecting"
                await self.close(Close.GOING_AWAY, "rx timeout")
                raise _SessionEnd(None, st.last_error) from None
            st.last_rx_mono = time.monotonic()
            try:
                msg = parse_message(raw)
            except ProtocolError as e:
                if e.code != "unknown_type":   # unknown types are ignored (forward compatible)
                    log.warning("bad frame from the hub: %s", e)
                continue
            if isinstance(msg, HeartbeatAck):
                st.last_ack_mono = time.monotonic()
                self.ack_count += 1
            elif isinstance(msg, Welcome):
                continue
            elif isinstance(msg, Pause):
                st.paused = True
            elif isinstance(msg, Resume):
                st.paused = False
            elif isinstance(msg, Error):
                log.warning("hub error %s: %s (reply to %s)", msg.code, msg.message, msg.reply_to)
                st.last_error = f"hub error {msg.code}: {msg.message}"
            try:
                await self.on_message(msg)
            except Exception:
                log.exception("handling %s failed", msg.type)

    async def _heartbeats(self, interval: float) -> None:
        interval = max(0.05, interval or self.t.heartbeat_s)
        while True:
            if self.build_heartbeat is not None:
                try:
                    await self.send(self.build_heartbeat())
                except Exception:
                    log.exception("could not build a heartbeat")
            await asyncio.sleep(interval)


class _SessionEnd(Exception):
    def __init__(self, code: int | None, reason: str = ""):
        super().__init__(reason)
        self.code = code
