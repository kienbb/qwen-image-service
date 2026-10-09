"""`qis tray`: notification-area monitor for the node operator (Windows).

The icon colour shows the node state (green ready, yellow starting / not warm, red down, grey service not installed);
in worker mode (docs/MESH.md 8.3) a ready engine shows the hub connection instead: green connected, yellow connecting,
orange hub unreachable, red join token rejected, dark red hub needs a newer QIS, blue draining, indigo paused by the hub;
its menu starts / stops / restarts the service, opens the dashboard and the logs, and toggles "Start with Windows".
The status window (tkinter) shows service + readiness, the queue, running jobs, GPU / system load, 24 h totals and
per-key usage, read from /healthz, /readyz and the admin API with an admin:* key kept in <root>/config/tray.key.

Threads: Tk owns the main thread, pystray runs detached, one poller thread does all HTTP / sc.exe calls (every ~2 s),
and service actions run in short-lived worker threads. Nothing blocking runs on the Tk or tray threads.
Everything above the GUI section is pure / testable without a display.
"""
from __future__ import annotations

import contextlib
import os
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import Config, load_config
from .mesh.state import hub_http_url

IS_WIN = sys.platform == "win32"
SERVICE = "qis"
KEY_FILE = "tray.key"
KEY_NAME = "tray"
POLL_S = 2.0
KEYS_EVERY = 5  # per-key usage is a GROUP BY over 24 h of jobs: refresh it every 5th poll only
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "QIS Tray"
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW: no console flash from sc.exe under pythonw

STATES = {  # overall state -> (headline, colour)
    "ready": ("Ready", "#16a34a"),
    "starting": ("Starting / warming up", "#ca8a04"),
    "down": ("Down", "#dc2626"),
    "not_installed": ("Service not installed", "#8a8a8a"),
}


# worker mode: hub connection state -> colour (docs/MESH.md 8.3). Every state has its own colour so an operator can
# tell at a glance why a worker is not taking jobs.
WORKER_COLOURS = {
    "connected": "#16a34a",     # green
    "connecting": "#ca8a04",    # yellow
    "disconnected": "#ea580c",  # orange
    "auth_failed": "#dc2626",   # red
    "outdated": "#9f1239",      # dark red
    "draining": "#2563eb",      # blue
    "paused": "#6366f1",        # indigo
}
HUB_ROWS = ("State", "Hub", "Worker ID", "Connected for", "Last heartbeat", "Jobs this session", "Last error")
NL = chr(10)
TOOLTIP_MAX = 127   # Windows caps notification-area tooltips at 128 characters including the terminator


# ------------------------------------------------------------------ admin key file
def key_path(root: Path) -> Path:
    return root / "config" / KEY_FILE


def read_key(path: Path) -> str | None:
    """The stored key, or None when the file is missing, empty or does not hold a qis key."""
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key if re.fullmatch(r"sk-qis-[A-Za-z0-9]{20,}", key) else None


def write_key(path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(key + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        tmp.chmod(0o600)
    os.replace(tmp, path)


def create_key(cfg: Config, path: Path) -> str:
    """New admin:* key for the tray (same code path as `qis key create --scopes admin:*`), written to `path`.
    Earlier tray keys are revoked so only one is ever active."""
    from .auth import ApiKeys
    from .db import DB
    db = DB(cfg.data_dir / "qis.db")
    keys = ApiKeys(db, cfg.secret())
    key, row = keys.create(KEY_NAME, scopes=["admin:*"])
    try:
        write_key(path, key)
    except OSError:
        keys.revoke(row["id"])
        raise
    for k in keys.list():
        if k["name"] == KEY_NAME and k["id"] != row["id"] and not k["revoked_at"]:
            keys.revoke(k["id"])
    db.audit("cli", "key.create", row["id"], {"name": KEY_NAME, "scopes": ["admin:*"]})
    return key


def ensure_key(root: Path, cfg: Config | None = None) -> str:
    """The tray key, created when missing or no longer valid (needs write access to the DB and config/)."""
    cfg = cfg or load_config(root / "config" / "config.toml")
    path = key_path(root)
    key = read_key(path)
    if key:
        from .auth import ApiKeys, AuthError
        from .db import DB
        with contextlib.suppress(AuthError):
            if "admin:*" in ApiKeys(DB(cfg.data_dir / "qis.db"), cfg.secret()).verify(key).get("scopes", []):
                return key
    return create_key(cfg, path)


# ------------------------------------------------------------------ service control (sc.exe)
_SC_STATE = re.compile(r":\s*[1-7]\s+(STOPPED|START_PENDING|STOP_PENDING|RUNNING|CONTINUE_PENDING|PAUSE_PENDING|PAUSED)\b")
SC_NOT_INSTALLED = 1060
SC_ACCESS_DENIED = 5


def parse_sc_state(output: str, returncode: int) -> str:
    """`sc query` output -> RUNNING / STOPPED / ... / NOT_INSTALLED / UNKNOWN (state names are not localized)."""
    if returncode == SC_NOT_INSTALLED:
        return "NOT_INSTALLED"
    m = _SC_STATE.search(output)
    return m.group(1) if m else "UNKNOWN"


def _sc(*args: str, timeout: float = 15) -> tuple[int, str]:
    try:
        r = subprocess.run(["sc.exe", *args], capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)
    return r.returncode, r.stdout + r.stderr


def service_state() -> str:
    if not IS_WIN:
        return "UNKNOWN"
    rc, out = _sc("query", SERVICE)
    return parse_sc_state(out, rc)


def _elevated(action: str) -> str:
    """Run the action through an elevated PowerShell (UAC prompt); used when the service ACL was not granted."""
    import ctypes
    cmd = {"start": "Start-Service", "stop": "Stop-Service -Force", "restart": "Restart-Service -Force"}[action]
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe",
                                             f"-NoProfile -WindowStyle Hidden -Command {cmd} {SERVICE}", None, 0)
    if rc <= 32:
        return f"{action}: administrator rights were refused or unavailable"
    return (f"{action}: no direct permission, asked Windows for administrator rights "
            "(run `qis service grant` as admin once to skip the prompt)")


def _wait_state(want: str, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if service_state() == want:
            return True
        time.sleep(1)
    return False


def control_service(action: str) -> str:
    """start / stop / restart the service as the current user; falls back to UAC on access denied. Blocking."""
    if not IS_WIN:
        return "service control is Windows only here (use systemctl)"
    if action == "restart":
        if service_state() not in ("STOPPED", "NOT_INSTALLED"):
            rc, out = _sc("stop", SERVICE)
            if rc == SC_ACCESS_DENIED:
                return _elevated("restart")
            if not _wait_state("STOPPED", 90):
                return "restart: the service did not stop within 90 s"
        action_sc = "start"
    else:
        action_sc = action
    rc, out = _sc(action_sc, SERVICE)
    if rc == 0:
        return f"{action}: requested"
    if rc == SC_ACCESS_DENIED:
        return _elevated(action)
    known = {1056: "already running", 1062: "not running", SC_NOT_INSTALLED: "service not installed"}
    lines = out.strip().splitlines()
    return f"{action}: {known.get(rc) or (lines[-1].strip() if lines else f'error {rc}')}"


# ------------------------------------------------------------------ local system load
class SystemSampler:
    """CPU % (delta of GetSystemTimes between calls) and RAM, without psutil. Empty dict off Windows."""

    def __init__(self):
        self._last: tuple[int, int] | None = None

    def sample(self) -> dict:
        if not IS_WIN:
            return {}
        import ctypes
        from ctypes import wintypes
        out: dict = {}
        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            def ft(f):
                return (f.dwHighDateTime << 32) | f.dwLowDateTime
            busy_total = (ft(kernel) + ft(user), ft(idle))  # kernel time includes idle time
            if self._last:
                total = busy_total[0] - self._last[0]
                if total > 0:
                    out["cpu_pct"] = round(100 * (1 - (busy_total[1] - self._last[1]) / total), 1)
            self._last = busy_total

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            out["ram_total"], out["ram_used"] = st.ullTotalPhys, st.ullTotalPhys - st.ullAvailPhys
        return out


# ------------------------------------------------------------------ polling
@dataclass
class Snapshot:
    service: str = "UNKNOWN"
    health: dict | None = None     # /healthz body, None = gateway unreachable
    ready: dict | None = None      # /readyz body (200 or 503)
    status: dict | None = None     # /admin/api/status
    keys: list | None = None       # /admin/api/keys "data"
    system: dict = field(default_factory=dict)
    error: str = ""                # transport error (never contains the key)
    auth_error: str = ""
    ts: float = 0.0


def _short(e: Exception) -> str:
    msg = str(e) or type(e).__name__
    return msg if len(msg) <= 120 else msg[:117] + "..."


def fetch(client: httpx.Client, base: str, key: str | None, with_keys: bool = True) -> Snapshot:
    snap = Snapshot(service=service_state(), ts=time.time())
    try:
        snap.health = client.get(base + "/healthz", timeout=3).json()
        snap.ready = client.get(base + "/readyz", timeout=3).json()
    except (httpx.HTTPError, ValueError) as e:
        snap.health = snap.ready = None
        snap.error = _short(e)
        return snap
    if not key:
        return snap
    headers = {"Authorization": f"Bearer {key}"}
    try:
        r = client.get(base + "/admin/api/status", headers=headers, timeout=5)
        if r.status_code in (401, 403):
            snap.auth_error = f"the tray's admin key was rejected ({r.status_code}): run `qis tray --init-key` as admin"
            return snap
        r.raise_for_status()
        snap.status = r.json()
        if with_keys:
            r = client.get(base + "/admin/api/keys", headers=headers, timeout=5)
            r.raise_for_status()
            snap.keys = r.json().get("data", [])
    except (httpx.HTTPError, ValueError) as e:
        snap.error = _short(e)
    return snap


# ------------------------------------------------------------------ view model (pure)
def fmt_bytes(n) -> str:
    return f"{(n or 0) / 1024 ** 3:.1f} GB"


def fmt_dur(s) -> str:
    s = int(s or 0)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def overall(snap: Snapshot) -> str:
    if snap.health is None:
        if snap.service == "NOT_INSTALLED":
            return "not_installed"
        # service up but the gateway not answering yet = still booting (WinSW restarts it on crashes)
        return "starting" if snap.service in ("RUNNING", "START_PENDING") else "down"
    return "ready" if (snap.ready or {}).get("ready") else "starting"


def _progress(job: dict) -> str:
    p = job.get("progress") or {}
    if p.get("pct") is not None:
        txt = f"{p['pct'] * 100:.0f}%"
        if p.get("steps"):
            txt += f"  step {p.get('step', 0)}/{p['steps']}"
        return txt
    return p.get("label") or p.get("op") or job.get("status", "")


def clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 3] + "..."


def worker_view(snap: Snapshot) -> dict | None:
    """The hub connection as the operator should see it, or None when worker mode is off. Pure.

    Reads `worker` from /healthz (works without an admin key) and from /admin/api/status (richer, preferred)."""
    h = (snap.health or {}).get("worker") or {}
    s = (snap.status or {}).get("worker") or {}
    if not (s.get("enabled") or h.get("enabled")):
        return None
    link = s.get("link") or h.get("link") or "connecting"
    if link not in WORKER_COLOURS:
        link = "connecting"
    host = s.get("hub") or h.get("hub") or ""
    running = s.get("running") or []
    left = len(running) + int(s.get("unacked") or 0)
    err = s.get("last_error") or ""
    first = running[0] if running else None
    pct = f"{first['pct'] * 100:.0f}%" if first and first.get("pct") is not None else ""
    if link == "connected":
        if running:
            headline = f"Worker: connected · {len(running)} running" + (f" ({pct})" if len(running) == 1 and pct else "")
        else:
            headline = f"Worker: connected to {host}" if host else "Worker: connected"
    elif link == "connecting":
        retry = s.get("retry_in_s")
        headline = f"Connecting to hub… retry in {retry} s" if retry else "Connecting to hub…"
    elif link == "disconnected":
        headline = f"Hub unreachable: {err}" if err else "Hub unreachable"
    elif link == "auth_failed":
        headline = "Join token rejected: run qis worker join"
    elif link == "outdated":
        headline = "Hub needs a newer QIS: run qis update"
    elif link == "draining":
        headline = f"Draining: {left} job{'s' if left != 1 else ''} left" if left else "Drained: no jobs left"
    else:
        headline = "Paused by hub"

    if link in ("connecting", "disconnected") and (s.get("replaced_count") or 0) >= 2:
        headline = "worker_id is in use elsewhere: is this data folder copied from another node?"
    headline = clip(headline, 100)
    lines = [f"QIS: {clip(headline, 56)}"]
    if host:
        lines.append(f"Hub: {host}")
    if first:
        lines.append(f"Job: {first.get('job_id') or first.get('attempt_id', '')} {first.get('phase', '')} {pct}".rstrip())
    base = NL.join(lines)
    if err and link != "connected" and err not in lines[0]:
        room = TOOLTIP_MAX - len(base) - len(NL + "Error: ")
        if room > 12:
            base += NL + "Error: " + (err if len(err) <= room else err[:room - 3] + "...")
    tooltip = base[:TOOLTIP_MAX] if len(base) > TOOLTIP_MAX else base

    now = snap.ts or time.time()
    since = s.get("connected_since")
    age = s.get("last_ack_age_s")
    hub_rows = {
        "State": link + (f" (retry in {s['retry_in_s']} s)" if link in ("connecting", "disconnected") and s.get("retry_in_s") else ""),
        "Hub": s.get("hub_url") or host or "-",
        "Worker ID": s.get("worker_id") or "-",
        "Connected for": fmt_dur(now - since) if since else "-",
        "Last heartbeat": f"{age:.0f}s ago" if age is not None else "-",
        "Jobs this session": f"{s.get('session_done', 0)} done, {s.get('session_failed', 0)} failed" if s else "-",
        "Last error": err or "-",
    }
    return {"link": link, "headline": headline, "colour": WORKER_COLOURS[link], "tooltip": tooltip, "rows": hub_rows,
            "draining": bool(s.get("draining")) or link == "draining", "paused": link == "paused",
            "hub_http": hub_http_url(s["hub_url"]) if s.get("hub_url") else ""}


def view_model(snap: Snapshot) -> dict:
    """Everything the window and the tray tooltip show, as display strings."""
    state = overall(snap)
    headline, colour = STATES[state]
    worker = worker_view(snap)
    st = snap.status or {}
    ready = snap.ready or {}
    q = st.get("queue") or {}
    vm: dict = {"state": state, "headline": headline, "colour": colour}
    if worker and state == "ready":     # engine is up: the hub link decides what the operator needs to see
        vm.update(state=worker["link"], headline=worker["headline"], colour=worker["colour"])

    if st:
        vm["subtitle"] = f"{st.get('name', '')}  v{st.get('version', '')}  up {fmt_dur(st.get('uptime_s'))}".strip()
    elif snap.health:
        vm["subtitle"] = f"v{snap.health.get('version', '')}"
    else:
        vm["subtitle"] = ""

    svc = snap.service.replace("_", " ").lower() if snap.service != "UNKNOWN" else "unknown"
    if snap.health is None:
        gateway = f"unreachable ({snap.error})" if snap.error else "unreachable"
    else:
        gateway = "ok"
    if ready:
        engine = f"{ready.get('comfy', '?')}, {'warm' if ready.get('warm') else 'not warm'}"
        if ready.get("missing_nodes"):
            engine += f", missing nodes: {', '.join(ready['missing_nodes'])}"
    else:
        engine = "-"
    vm["service"] = {"Service": svc, "Gateway": gateway, "Engine": engine}

    if q:
        vm["queue"] = {
            "Running": f"{q.get('running', 0)} GPU, {q.get('running_cpu', 0)} CPU",
            "Queued": f"{q.get('queued', 0)} GPU, {q.get('queued_cpu', 0)} CPU  (max pending {q.get('max_pending', '-')})",
            "ETA": fmt_dur(q.get("eta_s")) if q.get("eta_s") else "-",
            "Paused": "yes" if q.get("paused") else "no",
        }
    else:
        vm["queue"] = {"Running": "-", "Queued": "-", "ETA": "-", "Paused": "-"}

    g = st.get("gpu") or {}
    total = g.get("memory_total") or g.get("vram_total") or 0
    used = g.get("memory_used")
    if used is None and g.get("vram_total"):
        used = g["vram_total"] - g.get("vram_free", 0)
    vm["gpu"] = {
        "GPU": g.get("name") or "-",
        "Utilisation": f"{g['util_gpu']} %" if g.get("util_gpu") is not None else "-",
        "VRAM": f"{fmt_bytes(used)} / {fmt_bytes(total)}" if total else "-",
        "Temperature": f"{g['temperature']} °C" if g.get("temperature") is not None else "-",
        "Power": f"{g['power_w']:.0f} W" if g.get("power_w") is not None else "-",
    }

    sysm = snap.system or {}
    vm["system"] = {
        "CPU": f"{sysm['cpu_pct']:.0f} %" if sysm.get("cpu_pct") is not None else "-",
        "RAM": f"{fmt_bytes(sysm['ram_used'])} / {fmt_bytes(sysm['ram_total'])}" if sysm.get("ram_total") else "-",
    }

    d = st.get("last_24h") or {}
    vm["last_24h"] = {
        "Done": str(d.get("done", "-")) if d else "-",
        "Failed": str(d.get("failed", "-")) if d else "-",
        "Images": str(d.get("images", "-")) if d else "-",
        "GPU time": fmt_dur(d.get("gpu_s")) if d else "-",
    }

    vm["jobs"] = [(j.get("id", ""), j.get("preset") or j.get("kind") or "", _progress(j),
                   fmt_dur(j["eta_s"]) if j.get("eta_s") is not None else "-")
                  for j in st.get("running_jobs") or []]

    rows = [k for k in (snap.keys or []) if not k.get("revoked_at") or k.get("jobs_24h")]
    rows.sort(key=lambda k: (-(k.get("jobs_24h") or 0), k.get("name", "")))
    vm["keys"] = [(k.get("name", ""), f"{k.get('prefix', '')}...", str(k.get("jobs_24h") or 0),
                   str(k.get("failed_24h") or 0), "revoked" if k.get("revoked_at") else "active") for k in rows]

    vm["note"] = snap.auth_error or ("" if snap.status or snap.health is None else
                                     "no admin key: queue / GPU / usage hidden (run `qis tray --init-key` as admin)")
    vm["worker"] = ({k: worker[k] for k in ("link", "draining", "paused", "hub_http")} | {"enabled": True}
                    if worker else {"enabled": False, "link": "disabled", "draining": False, "paused": False, "hub_http": ""})
    vm["hub"] = worker["rows"] if worker else {}
    if worker and state == "ready":
        vm["tooltip"] = worker["tooltip"]
    else:
        busy = f" - {q.get('running', 0)} running, {q.get('queued', 0)} queued" if q else ""
        vm["tooltip"] = f"QIS: {headline}{busy}"[:TOOLTIP_MAX]
    return vm


# ------------------------------------------------------------------ autostart (HKCU Run)
def autostart_command(root: Path, url: str | None) -> str:
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    cmd = f'"{pyw if pyw.exists() else exe}" -m qis.cli tray --root "{root}"'
    return cmd + (f' --url "{url}"' if url else "")


def autostart_enabled() -> bool:
    if not IS_WIN:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
        return True
    except OSError:
        return False


def set_autostart(on: bool, command: str) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, command)
        else:
            with contextlib.suppress(FileNotFoundError):
                winreg.DeleteValue(k, RUN_VALUE)


# ------------------------------------------------------------------ GUI
def icon_image(colour: str, size: int = 64):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, size - 4, size - 4), fill=colour, outline=(255, 255, 255, 230), width=4)
    return img


class TrayApp:
    def __init__(self, root: Path, base: str, cfg: Config, url_arg: str | None):
        self.root_dir, self.base, self.cfg, self.url_arg = root, base.rstrip("/"), cfg, url_arg
        self.key = read_key(key_path(root))
        self.snap = Snapshot()
        self.vm = view_model(self.snap)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.ui_calls: queue.Queue = queue.Queue()  # callables to run on the Tk thread
        self.message = ""
        self.icon = None
        self.tk = None
        self.win = None

    # ------------------------------------------------ poller thread
    def poll_loop(self) -> None:
        sampler = SystemSampler()
        n, last_keys, icon_colour = 0, None, None
        with httpx.Client(trust_env=False) as client:
            while not self.stop.is_set():
                try:
                    snap = fetch(client, self.base, self.key, with_keys=n % KEYS_EVERY == 0)
                    with contextlib.suppress(Exception):
                        snap.system = sampler.sample()
                    if snap.keys is None and snap.status is not None:
                        snap.keys = last_keys
                    last_keys = snap.keys
                    vm = view_model(snap)
                except Exception as e:  # an unexpected payload must not kill the poller
                    snap = Snapshot(service="UNKNOWN", error=f"tray error: {_short(e)}", ts=time.time())
                    vm = view_model(snap)
                with self.lock:
                    self.snap, self.vm = snap, vm
                if self.icon is not None:
                    with contextlib.suppress(Exception):
                        if vm["colour"] != icon_colour:
                            self.icon.icon = icon_image(vm["colour"])
                            icon_colour = vm["colour"]
                        self.icon.title = vm["tooltip"]
                n += 1
                self.stop.wait(POLL_S)

    # ------------------------------------------------ actions (any thread)
    def service(self, action: str) -> None:
        self.say(f"{action}: working...")
        threading.Thread(target=lambda: self.say(control_service(action)), daemon=True).start()

    def say(self, msg: str) -> None:
        self.message = f"{time.strftime('%H:%M:%S')}  {msg}"

    def open_dashboard(self) -> None:
        webbrowser.open(self.base + "/admin/")

    def open_hub(self) -> None:
        url = self.vm.get("worker", {}).get("hub_http")
        if url:
            webbrowser.open(url)
        else:
            self.say("hub address unknown (the gateway has not reported worker status yet)")

    def worker_action(self, action: str) -> None:
        """Drain / Resume through the local gateway's admin API (needs the tray key)."""
        if not self.key:
            self.say(f"{action}: no admin key (run `qis tray --init-key` as admin)")
            return
        self.say(f"{action}: working...")

        def run():
            try:
                with httpx.Client(trust_env=False) as c:
                    r = c.post(f"{self.base}/admin/api/worker/{action}", headers={"Authorization": f"Bearer {self.key}"}, timeout=10)
                self.say(f"{action}: ok" if r.status_code == 200 else f"{action}: HTTP {r.status_code} {r.text[:80]}")
            except httpx.HTTPError as e:
                self.say(f"{action}: {_short(e)}")
        threading.Thread(target=run, daemon=True).start()

    def open_logs(self) -> None:
        logs = self.cfg.data_dir / "logs"
        if IS_WIN and logs.exists():
            os.startfile(logs)
        else:
            self.say(f"no log folder at {logs}")

    def toggle_autostart(self) -> None:
        try:
            set_autostart(not autostart_enabled(), autostart_command(self.root_dir, self.url_arg))
        except OSError as e:
            self.say(f"Start with Windows: {e}")

    def show_window(self) -> None:
        self.ui_calls.put(self._show)

    def quit(self) -> None:
        self.stop.set()
        if self.icon is not None:
            with contextlib.suppress(Exception):
                self.icon.stop()
        self.ui_calls.put(lambda: self.tk.quit())

    # ------------------------------------------------ tray
    def build_icon(self):
        import pystray
        def svc() -> str:
            return self.snap.service

        def worker() -> dict:
            return self.vm.get("worker", {})
        item = pystray.MenuItem
        menu = pystray.Menu(
            item("Open status window", lambda: self.show_window(), default=True),
            pystray.Menu.SEPARATOR,
            item("Start service", lambda: self.service("start"), enabled=lambda i: svc() in ("STOPPED", "UNKNOWN")),
            item("Stop service", lambda: self.service("stop"),
                 enabled=lambda i: svc() in ("RUNNING", "START_PENDING", "UNKNOWN")),
            item("Restart service", lambda: self.service("restart"), enabled=lambda i: svc() != "NOT_INSTALLED"),
            pystray.Menu.SEPARATOR,
            item("Open dashboard", lambda: self.open_dashboard()),
            item("Drain (finish current jobs)", lambda: self.worker_action("drain"),
                 visible=lambda i: worker().get("enabled", False), enabled=lambda i: not worker().get("draining")),
            item("Resume", lambda: self.worker_action("resume"),
                 visible=lambda i: worker().get("enabled", False), enabled=lambda i: bool(worker().get("draining"))),
            item("Open hub dashboard", lambda: self.open_hub(),
                 visible=lambda i: worker().get("enabled", False), enabled=lambda i: bool(worker().get("hub_http"))),
            item("Open logs folder", lambda: self.open_logs()),
            item("Start with Windows", lambda: self.toggle_autostart(), checked=lambda i: autostart_enabled(),
                 visible=IS_WIN),
            pystray.Menu.SEPARATOR,
            item("Quit", lambda: self.quit()),
        )
        return pystray.Icon("qis", icon_image(self.vm["colour"]), self.vm["tooltip"], menu)

    # ------------------------------------------------ Tk (main thread only)
    def _show(self) -> None:
        if self.win is not None:
            self.win.deiconify()
            self.win.lift()
            return
        self.win = StatusWindow(self)

    def _pump(self) -> None:
        while True:
            try:
                fn = self.ui_calls.get_nowait()
            except queue.Empty:
                break
            fn()
        if self.win is not None:
            with self.lock:
                vm = self.vm
            self.win.render(vm, self.snap.ts, self.message)
        if not self.stop.is_set():
            self.tk.after(500, self._pump)

    def offer_key(self) -> None:
        """No (valid) key yet: on an installed node offer to create one here; never blocks when there is no DB."""
        if self.key or not (self.root_dir / "config" / "config.toml").exists():
            return
        from tkinter import messagebox
        if not messagebox.askyesno("Qwen Image Service", "The tray needs an admin API key to show the queue, GPU and "
                                   f"usage. Create one now (stored in {key_path(self.root_dir)})?"):
            return
        try:
            self.key = ensure_key(self.root_dir, self.cfg)
            self.say("admin key created")
        except Exception as e:
            messagebox.showwarning("Qwen Image Service", f"Could not create the key: {_short(e)}\n\nRun once from an "
                                   "administrator prompt:\n  qis tray --init-key")

    def run(self, show: bool) -> None:
        import tkinter as tk
        self.tk = tk.Tk()
        self.tk.withdraw()
        self.offer_key()
        threading.Thread(target=self.poll_loop, name="qis-tray-poll", daemon=True).start()
        self.icon = self.build_icon()
        self.icon.run_detached()
        if show:
            self.show_window()
        self.tk.after(100, self._pump)
        try:
            self.tk.mainloop()
        finally:
            self.stop.set()
            with contextlib.suppress(Exception):
                self.icon.stop()


class StatusWindow:
    SECTIONS = (("service", "Service"), ("hub", "Hub"), ("queue", "Queue"), ("gpu", "GPU"), ("system", "System"),
                ("last_24h", "Last 24 h"))

    def __init__(self, app: TrayApp):
        import tkinter as tk
        from tkinter import ttk
        self.app = app
        w = self.top = tk.Toplevel(app.tk)
        w.title("Qwen Image Service")
        w.geometry("760x720")
        w.minsize(620, 560)
        w.protocol("WM_DELETE_WINDOW", w.withdraw)  # closing hides; Quit is in the tray menu
        self.vars: dict[tuple[str, str], tk.StringVar] = {}
        pad = {"padx": 8, "pady": 4}

        head = ttk.Frame(w)
        head.pack(fill="x", **pad)
        self.dot = tk.Canvas(head, width=22, height=22, highlightthickness=0)
        self.dot_id = self.dot.create_oval(3, 3, 19, 19, fill="#8a8a8a", outline="")
        self.dot.pack(side="left")
        self.headline = tk.StringVar()
        self.subtitle = tk.StringVar()
        ttk.Label(head, textvariable=self.headline, font=("Segoe UI", 14, "bold")).pack(side="left", padx=8)
        ttk.Label(head, textvariable=self.subtitle, foreground="#666").pack(side="left")

        bar = ttk.Frame(w)
        bar.pack(fill="x", **pad)
        self.worker_buttons = []
        for label, fn, worker_only in (("Start", lambda: app.service("start"), False),
                                       ("Stop", lambda: app.service("stop"), False),
                                       ("Restart", lambda: app.service("restart"), False),
                                       ("Dashboard", app.open_dashboard, False), ("Logs", app.open_logs, False),
                                       ("Drain", lambda: app.worker_action("drain"), True),
                                       ("Resume", lambda: app.worker_action("resume"), True),
                                       ("Hub", app.open_hub, True)):
            b = ttk.Button(bar, text=label, command=fn)
            b.pack(side="left", padx=(0, 6))
            if worker_only:
                self.worker_buttons.append(b)

        grid = ttk.Frame(w)
        grid.pack(fill="x", **pad)
        vm = app.vm
        self.boxes: dict[str, tuple] = {}
        for i, (name, title) in enumerate(self.SECTIONS):
            box = ttk.LabelFrame(grid, text=title)
            box.grid(row=i // 2, column=i % 2, sticky="nsew", padx=4, pady=4)
            self.boxes[name] = (box, i)
            for r, label in enumerate(HUB_ROWS if name == "hub" else vm[name]):
                ttk.Label(box, text=label + ":", foreground="#555").grid(row=r, column=0, sticky="w", padx=6)
                v = self.vars[(name, label)] = tk.StringVar()
                ttk.Label(box, textvariable=v).grid(row=r, column=1, sticky="w", padx=6)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        self.jobs = self._table(w, "Running jobs", ("id", "preset", "progress", "eta"), (190, 150, 190, 80), 4)
        self.keys = self._table(w, "API keys (last 24 h)", ("name", "prefix", "jobs", "failed", "state"),
                                (200, 150, 80, 80, 80), 6)

        self.note = tk.StringVar()
        self.status = tk.StringVar()
        ttk.Label(w, textvariable=self.note, foreground="#b45309", wraplength=720).pack(fill="x", **pad)
        ttk.Label(w, textvariable=self.status, foreground="#666").pack(side="bottom", fill="x", **pad)

    def _table(self, parent, title, cols, widths, height):
        from tkinter import ttk
        box = ttk.LabelFrame(parent, text=title)
        box.pack(fill="both", expand=True, padx=8, pady=4)
        t = ttk.Treeview(box, columns=cols, show="headings", height=height)
        for c, wd in zip(cols, widths, strict=True):
            t.heading(c, text=c)
            t.column(c, width=wd, anchor="w")
        t.pack(fill="both", expand=True)
        return t

    @staticmethod
    def _fill(tree, rows) -> None:
        old = tree.get_children()
        if [tuple(tree.item(i, "values")) for i in old] == [tuple(map(str, r)) for r in rows]:
            return  # unchanged: keep selection / scroll position
        tree.delete(*old)
        for r in rows:
            tree.insert("", "end", values=r)

    def render(self, vm: dict, ts: float, message: str) -> None:
        if not self.top.winfo_viewable():
            return
        self.dot.itemconfigure(self.dot_id, fill=vm["colour"])
        self.headline.set(vm["headline"])
        self.subtitle.set(vm["subtitle"])
        for (name, label), v in self.vars.items():
            v.set(vm[name].get(label, "-"))
        enabled = vm["worker"]["enabled"]
        box, i = self.boxes["hub"]
        if enabled:
            box.grid(row=i // 2, column=i % 2, sticky="nsew", padx=4, pady=4)
        else:
            box.grid_remove()
        for b in self.worker_buttons:
            b.state(["!disabled"] if enabled else ["disabled"])
        self._fill(self.jobs, vm["jobs"])
        self._fill(self.keys, vm["keys"])
        self.note.set(vm["note"])
        updated = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "-"
        self.status.set(f"updated {updated}    {message}")


# ------------------------------------------------------------------ CLI
def cmd_tray(a) -> None:
    from .setup import default_root
    root = Path(a.root).resolve() if a.root else default_root()
    cfg = load_config(root / "config" / "config.toml")
    if a.init_key:
        try:
            ensure_key(root, cfg)
        except Exception as e:
            sys.exit(f"could not create the tray key: {e}")
        print(f"tray key ready: {key_path(root)}")
        return
    try:
        import tkinter  # noqa: F401

        import pystray  # noqa: F401
    except ImportError as e:
        sys.exit(f"qis tray needs the 'tray' extra ({e.name} is missing): "
                 f"uv pip install --python \"{sys.executable}\" pystray   (or: pip install qwen-image-service[tray])")
    base = a.url or f"http://127.0.0.1:{cfg.server.port}"
    TrayApp(root, base, cfg, a.url).run(show=not a.hidden)
