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


def cmd_serve(a) -> None:
    import uvicorn

    from .app import create_app
    cfg = load_config(a.config)
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

    try:
        from .setup import register_cli
        register_cli(sub)
    except ImportError:
        pass

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
