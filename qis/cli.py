"""`qis` management CLI and the `qis-gateway` server entry point."""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import logging.handlers
import secrets
import sys
import time
from pathlib import Path

from . import __version__
from .config import Config, load_config


def _setup_logging(cfg: Config, verbose: bool = False) -> None:
    logs = cfg.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(logs / "gateway.log", maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.handlers[:] = [fh, sh]
    for noisy in ("httpx", "httpcore", "websockets", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_serve(a, cfg: Config | None = None) -> None:
    import uvicorn

    from .app import create_app
    cfg = cfg or load_config(a.config)
    if a.host:
        cfg.server.host = a.host
    if a.port:
        cfg.server.port = a.port
    if a.attach:
        cfg.comfy.manage = False
    _setup_logging(cfg, a.verbose)
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_config=None, proxy_headers=False,
                timeout_keep_alive=30)


def serve_main() -> None:
    ap = argparse.ArgumentParser("qis-gateway")
    _serve_args(ap)
    cmd_serve(ap.parse_args())


def _serve_args(ap):
    ap.add_argument("--config", help="config.toml (default: $QIS_CONFIG, ./config.toml, ./config/config.toml)")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--attach", action="store_true", help="use an already running ComfyUI instead of starting one")
    ap.add_argument("-v", "--verbose", action="store_true")


def _db(a):
    from .auth import ApiKeys, Users
    from .db import DB
    cfg = load_config(a.config)
    db = DB(cfg.data_dir / "qis.db")
    return cfg, db, ApiKeys(db, cfg.secret()), Users(db)


def cmd_key(a) -> None:
    cfg, db, keys, _ = _db(a)
    if a.action == "create":
        scopes = a.scopes.split(",") if a.scopes else None
        key, row = keys.create(a.name, scopes=scopes, priority=a.priority, max_pending=a.max_pending, rpm=a.rpm)
        db.audit("cli", "key.create", row["id"], {"name": a.name})
        if a.json:
            print(json.dumps({"key": key, **row}))
        else:
            print(f"created {row['id']} ({row['name']}) scopes={','.join(row['scopes'])}")
            print(f"\n  {key}\n\nThe key is shown only once. Use it as: Authorization: Bearer {key[:14]}...")
    elif a.action == "list":
        for k in keys.list():
            state = "revoked" if k["revoked_at"] else "active"
            print(f"{k['id']}  {k['prefix']}...  {k['name']:<24} {state:<8} {','.join(k['scopes'])}")
    elif a.action == "revoke":
        print("revoked" if keys.revoke(a.id) else "not found / already revoked")
        db.audit("cli", "key.revoke", a.id)


def cmd_admin(a) -> None:
    cfg, db, _, users = _db(a)
    if a.action == "create":
        pw = a.password
        generated = False
        if not pw:
            if sys.stdin.isatty() and not a.random:
                pw = getpass.getpass("password (min 10 chars): ")
            else:
                pw, generated = secrets.token_urlsafe(12), True
        u = users.create(a.username, pw, email=a.email, role=a.role, must_change_pw=generated)
        db.audit("cli", "user.create", u["id"], {"username": a.username, "role": a.role})
        print(f"created user {u['username']} ({u['role']})")
        if generated:
            print(f"password: {pw}   (must be changed at first login)")
    elif a.action == "reset-password":
        row = users.find(a.username)
        if not row:
            sys.exit(f"no user {a.username}")
        pw = a.password or secrets.token_urlsafe(12)
        users.set_password(row["id"], pw, must_change=not a.password)
        users.unlock(row["id"])
        db.audit("cli", "user.reset_password", row["id"])
        print(f"password for {row['username']}: {pw}" + ("" if a.password else "   (must be changed at next login)"))
    elif a.action == "list":
        for u in users.list():
            print(f"{u['id']}  {u['username']:<20} {u.get('email') or '':<30} {u['role']:<9} {'disabled' if u['disabled'] else ''}")


def cmd_hardware(a) -> None:
    from .engine import hardware
    hw = hardware.detect(a.path or ".")
    vram = hw.gpus[0].vram_gb if hw.gpus else 0
    tier = hardware.choose_tier(vram)
    out = {**hw.to_dict(), "tier": tier, "torch_index": hardware.torch_index(hw.gpus[0].compute_cap if hw.gpus else None)}
    print(json.dumps(out, indent=1))


def cmd_status(a) -> None:
    import httpx
    cfg = load_config(a.config)
    base = f"http://127.0.0.1:{cfg.server.port}"
    try:
        h = httpx.get(base + "/healthz", timeout=5).json()
        r = httpx.get(base + "/readyz", timeout=5)
        print(json.dumps({"gateway": h, "ready": r.json()}, indent=1))
        sys.exit(0 if r.status_code == 200 else 3)
    except httpx.HTTPError as e:
        print(f"gateway not reachable at {base}: {e}")
        sys.exit(2)


def cmd_settings(a) -> None:
    from .db import DB
    from .settings import Settings
    cfg = load_config(a.config)
    s = Settings(DB(cfg.data_dir / "qis.db"))
    if a.action == "get":
        data = s.current.model_dump()
        print(json.dumps(data.get(a.group) if a.group else data, indent=1))
    else:
        group, key = a.key.split(".", 1)
        try:
            value = json.loads(a.value)
        except ValueError:
            value = a.value
        s.update(group, {key: value}, actor="cli")
        print(f"{a.key} = {json.dumps(value)} (a running gateway picks this up after restart or via the dashboard)")


def cmd_db(a) -> None:
    from .db import DB
    cfg = load_config(a.config)
    db = DB(cfg.data_dir / "qis.db")
    if a.action == "upgrade":
        print(f"schema version {db.migrate()}")
    else:
        dest = Path(a.out) if a.out else cfg.data_dir / "backups" / f"qis-{time.strftime('%Y%m%d-%H%M%S')}.db"
        dest.parent.mkdir(parents=True, exist_ok=True)
        db.backup(dest)
        print(f"backup written to {dest}")


def cmd_doctor(a) -> None:
    from .doctor import run_doctor
    sys.exit(run_doctor(load_config(a.config), as_json=a.json))


def cmd_benchmark(a) -> None:
    from .bench import run_benchmark
    run_benchmark(load_config(a.config), a.url, a.key, a.images, apply=a.apply)


def cmd_tray(a) -> None:
    from .tray import cmd_tray as run
    run(a)


# ------------------------------------------------------------------ qis worker ... (docs/MESH.md section 8.1)
def _worker_ctx(a) -> tuple[Path, Path, Config]:
    """(install root, config.toml, config) for the worker commands: --config wins, else <root>/config/config.toml."""
    if a.config:
        cfg_path = Path(a.config)
        root = cfg_path.resolve().parent.parent
    else:
        from .setup import default_root
        root = Path(a.root).resolve() if a.root else default_root()
        cfg_path = root / "config" / "config.toml"
    return root, cfg_path, load_config(cfg_path)


def _restart(root: Path, no_restart: bool) -> None:
    if no_restart:
        print("not restarting (--no-restart): restart the service for the change to take effect")
        return
    from .setup import service_cmd, service_installed
    try:
        if not service_installed(root):
            print("the qis service is not installed: start the node with `qis serve` (or `qis worker run`)")
            return
        rc = service_cmd(root, "restart")
        print("service restarted" if rc == 0 else f"service restart failed (exit {rc}): restart it manually")
    except (OSError, ValueError) as e:
        print(f"could not restart the service ({e}): restart it manually")


def _admin_headers(root: Path) -> dict:
    """Authorization for the local admin API: the tray's admin key, created on first use."""
    from .tray import ensure_key, key_path, read_key
    key = read_key(key_path(root))
    if not key:
        try:
            key = ensure_key(root)
        except Exception as e:
            sys.exit(f"no admin key for the local gateway ({e}); run `qis tray --init-key` as administrator")
    return {"Authorization": f"Bearer {key}"}


def _local_call(a, method: str, path: str, *, timeout: float = 15, params: dict | None = None):
    import httpx
    root, _, cfg = _worker_ctx(a)
    base = f"http://127.0.0.1:{cfg.server.port}"
    try:
        r = httpx.request(method, base + path, headers=_admin_headers(root), params=params, timeout=timeout)
    except httpx.HTTPError as e:
        sys.exit(f"gateway not reachable at {base}: {e}")
    if r.status_code >= 400:
        try:
            msg = r.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            msg = r.text[:200]
        sys.exit(f"{path}: HTTP {r.status_code}: {msg}")
    return r.json()


def _print_worker(w: dict) -> None:
    if not w.get("enabled"):
        print("worker mode is off ([worker] enabled = false)")
        return
    print(f"link      : {w.get('link')}  ({w.get('state', '')})")
    print(f"hub       : {w.get('hub_url')}")
    print(f"worker id : {w.get('worker_id')}  name: {w.get('name')}")
    if w.get("retry_in_s") is not None:
        print(f"retry in  : {w['retry_in_s']} s")
    if w.get("last_error"):
        print(f"last error: {w['last_error']}")
    for r in w.get("running") or []:
        print(f"running   : {r.get('job_id')} {r.get('phase')} {round((r.get('pct') or 0) * 100)}%")
    print(f"session   : {w.get('session_done', 0)} done, {w.get('session_failed', 0)} failed")


def cmd_worker(a) -> None:
    import os
    cmd = a.wcmd
    if cmd == "join":
        from .mesh.join import join
        root, cfg_path, _ = _worker_ctx(a)
        token = a.token or os.environ.get("QIS_JOIN_TOKEN") or (getpass.getpass("join token: ") if sys.stdin.isatty() else "")
        try:
            url = join(cfg_path, a.hub_url, token, a.name)
        except ValueError as e:
            sys.exit(str(e))
        print(f"worker mode on: {url}")
        print(f"token stored in {cfg_path.parent / 'worker.token'}")
        _restart(root, a.no_restart)
    elif cmd == "leave":
        from .mesh.join import leave
        root, cfg_path, cfg = _worker_ctx(a)
        if cfg.worker.enabled:
            try:  # let a running gateway finish its jobs and say bye(leave) first
                _local_call(a, "POST", "/admin/api/worker/leave", timeout=a.timeout + 15, params={"timeout": a.timeout})
                print("drained and left the hub")
            except SystemExit as e:
                print(f"could not drain through the gateway ({e}); turning worker mode off anyway")
        leave(cfg_path)
        print("worker mode off")
        _restart(root, a.no_restart)
    elif cmd == "status":
        w = _local_call(a, "GET", "/admin/api/status").get("worker") or {}
        if a.json:
            print(json.dumps(w, indent=1))
        else:
            _print_worker(w)
        sys.exit(0 if w.get("link") in ("connected", "draining", "paused") else 3)
    elif cmd == "drain":
        w = _local_call(a, "POST", "/admin/api/worker/drain", timeout=(a.timeout + 15) if a.wait else 15,
                        params={"wait": "true" if a.wait else "false", "timeout": a.timeout})
        _print_worker(w)
    elif cmd == "resume":
        _print_worker(_local_call(a, "POST", "/admin/api/worker/resume"))
    elif cmd == "run":
        cfg = load_config(a.config)
        cfg.worker.enabled = True
        if a.hub:
            cfg.worker.hub_url = a.hub
        elif os.environ.get("QIS_HUB_URL"):
            cfg.worker.hub_url = os.environ["QIS_HUB_URL"]
        if a.token:
            cfg.worker.token = a.token
        if a.name:
            cfg.worker.name = a.name
        cmd_serve(a, cfg)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser("qis", description=f"Qwen Image Service {__version__}")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the gateway (starts ComfyUI unless --attach)")
    _serve_args(p)
    p.set_defaults(fn=cmd_serve)

    def common(p):
        p.add_argument("--config")
        return p

    p = common(sub.add_parser("key", help="API keys"))
    p.add_argument("action", choices=["create", "list", "revoke"])
    p.add_argument("id", nargs="?")
    p.add_argument("--name", default="default")
    p.add_argument("--scopes", help="comma separated (default: all but admin:*)")
    p.add_argument("--priority", default="interactive", choices=["interactive", "batch"])
    p.add_argument("--max-pending", type=int)
    p.add_argument("--rpm", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_key)

    p = common(sub.add_parser("admin", help="dashboard users"))
    p.add_argument("action", choices=["create", "reset-password", "list"])
    p.add_argument("--username", default="admin")
    p.add_argument("--email")
    p.add_argument("--role", default="admin", choices=["admin", "operator", "viewer"])
    p.add_argument("--password")
    p.add_argument("--random", action="store_true", help="generate a password (must be changed at first login)")
    p.set_defaults(fn=cmd_admin)

    p = sub.add_parser("hardware", help="detect GPU / RAM / disk and the recommended tier")
    p.add_argument("--path")
    p.set_defaults(fn=cmd_hardware)

    p = common(sub.add_parser("status", help="gateway health"))
    p.set_defaults(fn=cmd_status)

    p = common(sub.add_parser("settings", help="read / change runtime settings"))
    p.add_argument("action", choices=["get", "set"])
    p.add_argument("key", nargs="?", help="group.key for set, group for get")
    p.add_argument("value", nargs="?")
    p.set_defaults(fn=lambda a: cmd_settings(argparse.Namespace(**{**vars(a), "group": a.key if a.action == "get" else None})))

    p = common(sub.add_parser("db", help="database maintenance"))
    p.add_argument("action", choices=["upgrade", "backup"])
    p.add_argument("--out")
    p.set_defaults(fn=cmd_db)

    p = common(sub.add_parser("doctor", help="check driver, CUDA, ComfyUI, nodes, models, disk"))
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_doctor)

    p = common(sub.add_parser("benchmark", help="measure s/step on a running gateway and set queue defaults"))
    p.add_argument("--url", default=None)
    p.add_argument("--key", required=False)
    p.add_argument("--images", type=int, default=3)
    p.add_argument("--apply", action="store_true", help="write perf + max_pending into the settings")
    p.set_defaults(fn=cmd_benchmark)

    p = sub.add_parser("tray", help="notification-area monitor: status window, start / stop / restart the service")
    p.add_argument("--root", help="install root (default: derived from the venv)")
    p.add_argument("--url", help="gateway URL (default: http://127.0.0.1:<port from config>)")
    p.add_argument("--hidden", action="store_true", help="start in the tray without opening the status window")
    p.add_argument("--init-key", action="store_true", help="create the tray's admin:* key (config/tray.key) and exit")
    p.set_defaults(fn=cmd_tray)

    p = sub.add_parser("worker", help="worker mode: join / leave a QIS hub, status, drain, resume, run")
    wsub = p.add_subparsers(dest="wcmd", required=True)

    def wcommon(p):
        p.add_argument("--config", help="config.toml (default: <root>/config/config.toml)")
        p.add_argument("--root", help="install root (default: derived from the venv)")
        p.set_defaults(fn=cmd_worker)
        return p

    w = wcommon(wsub.add_parser("join", help="connect this node to a hub: writes [worker] + config/worker.token, restarts"))
    w.add_argument("hub_url", help="https://qis.runnow.cc (or wss://host/worker/ws, ws://host:8101/worker/ws)")
    w.add_argument("--token", help="join token (default: $QIS_JOIN_TOKEN, else prompt)")
    w.add_argument("--name", help="worker name shown on the hub (default: hostname)")
    w.add_argument("--no-restart", action="store_true")
    w = wcommon(wsub.add_parser("leave", help="drain, tell the hub bye, turn worker mode off, restart"))
    w.add_argument("--timeout", type=float, default=120, help="seconds to wait for running jobs")
    w.add_argument("--no-restart", action="store_true")
    w = wcommon(wsub.add_parser("status", help="hub connection state (exit 0 when connected)"))
    w.add_argument("--json", action="store_true")
    w = wcommon(wsub.add_parser("drain", help="stop taking jobs, finish the running ones, disconnect"))
    w.add_argument("--wait", action="store_true", help="block until drained")
    w.add_argument("--timeout", type=float, default=3600)
    wcommon(wsub.add_parser("resume", help="reconnect / take jobs again after a drain"))
    w = wcommon(wsub.add_parser("run", help="run the gateway in the foreground in worker mode (dev, fake hub)"))
    w.add_argument("--hub", help="hub URL (default: $QIS_HUB_URL, else [worker].hub_url)")
    w.add_argument("--token", help="join token (default: $QIS_JOIN_TOKEN, else config/worker.token)")
    w.add_argument("--name")
    w.add_argument("--host")
    w.add_argument("--port", type=int)
    w.add_argument("--attach", action="store_true", help="use an already running ComfyUI")
    w.add_argument("-v", "--verbose", action="store_true")

    try:
        from .setup import register_cli
        register_cli(sub)
    except ImportError:
        pass

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
