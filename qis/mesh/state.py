"""Worker connection state (docs/MESH.md section 8.3): one small object the link, the runner, /healthz,
/admin/api/status and the tray all read. `link` is derived at read time so it can never go stale."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

LINK_STATES = ("disabled", "connecting", "connected", "disconnected", "auth_failed", "outdated", "draining", "paused")
DISCONNECTED_AFTER_S = 60.0   # no session for this long (while still retrying) = disconnected
ACK_FRESH_S = 30.0            # `connected` needs a frame from the hub newer than this
WS_PATH = "/worker/ws"


def normalize_hub_url(url: str) -> str:
    """https://qis.runnow.cc -> wss://qis.runnow.cc/worker/ws; http -> ws; ws(s):// kept; a bare host means wss."""
    url = url.strip()
    if "://" not in url:
        url = "wss://" + url
    u = urlparse(url)
    scheme = {"https": "wss", "http": "ws", "wss": "wss", "ws": "ws"}.get(u.scheme)
    if scheme is None or not u.netloc:
        raise ValueError(f"not a hub URL: {url!r}")
    path = u.path if u.path not in ("", "/") else WS_PATH
    return f"{scheme}://{u.netloc}{path}"


def hub_http_url(ws_url: str) -> str:
    """The hub's web address for 'Open hub dashboard': wss://host/worker/ws -> https://host."""
    u = urlparse(ws_url)
    scheme = {"wss": "https", "ws": "http"}.get(u.scheme, u.scheme or "https")
    return f"{scheme}://{u.netloc}"


def hub_host(ws_url: str) -> str:
    return urlparse(ws_url).netloc or ws_url


@dataclass
class WorkerState:
    enabled: bool = True
    hub_url: str = ""
    worker_id: str = ""
    name: str = ""
    started_mono: float = field(default_factory=time.monotonic)
    # connection
    connected: bool = False
    session_id: str = ""
    connected_since: float | None = None      # epoch
    last_rx_mono: float | None = None         # any frame from the hub
    last_ack_mono: float | None = None
    disconnected_mono: float | None = None    # when the last session ended (None = never connected)
    retry_at_mono: float | None = None
    sticky: str = ""                          # "auth_failed" | "outdated" until the next welcome
    last_error: str = ""
    last_close_code: int | None = None
    replaced_count: int = 0
    # operator / hub intent
    draining: bool = False
    paused: bool = False
    parked: bool = False                      # connection closed on purpose (drained / left)
    # counters for this process
    session_done: int = 0
    session_failed: int = 0

    def last_ack_age_s(self, now: float | None = None) -> float | None:
        now = now if now is not None else time.monotonic()
        ref = self.last_ack_mono if self.last_ack_mono is not None else self.last_rx_mono
        return None if ref is None else round(now - ref, 1)

    def link(self, now: float | None = None) -> str:
        now = now if now is not None else time.monotonic()
        if not self.enabled:
            return "disabled"
        if self.sticky:
            return self.sticky
        if self.parked and self.draining:
            return "draining"
        if not self.connected:
            since = self.disconnected_mono if self.disconnected_mono is not None else self.started_mono
            return "disconnected" if now - since > DISCONNECTED_AFTER_S else "connecting"
        age = self.last_ack_age_s(now)
        if age is not None and age > ACK_FRESH_S:
            return "connecting"
        if self.draining:
            return "draining"
        if self.paused:
            return "paused"
        return "connected"

    def retry_in_s(self, now: float | None = None) -> int | None:
        now = now if now is not None else time.monotonic()
        if self.connected or self.retry_at_mono is None:
            return None
        return max(0, round(self.retry_at_mono - now))

    def health(self) -> dict:
        """The `worker` block of /healthz (no secrets, readable without an admin key)."""
        if not self.enabled:
            return {"enabled": False}
        return {"enabled": True, "link": self.link(), "hub": hub_host(self.hub_url)}

    def snapshot(self, running: list[dict] | None = None, unacked: int = 0, state: str = "") -> dict:
        """The `worker` block of /admin/api/status."""
        if not self.enabled:
            return {"enabled": False, "link": "disabled"}
        now = time.monotonic()
        return {"enabled": True, "link": self.link(now), "hub_url": self.hub_url, "hub": hub_host(self.hub_url),
                "worker_id": self.worker_id, "name": self.name, "state": state,
                "session_id": self.session_id or None,
                "connected_since": self.connected_since if self.connected else None,
                "last_ack_age_s": self.last_ack_age_s(now), "retry_in_s": self.retry_in_s(now),
                "last_error": self.last_error, "last_close_code": self.last_close_code,
                "draining": self.draining, "paused": self.paused, "replaced_count": self.replaced_count,
                "running": running or [], "unacked": unacked,
                "session_done": self.session_done, "session_failed": self.session_failed}
