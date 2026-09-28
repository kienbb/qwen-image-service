"""`qis setup` and friends: the Python half of the installer (the .ps1/.sh bootstrap installs uv + this package,
then hands over here). Also: service registration (WinSW / systemd), cloudflared, update with rollback, uninstall.

Layout under the install root (DESIGN §12.6):
  app/ (this repo)  .venv/  comfyui/  models/  config/config.toml  data/  bin/ (uv, WinSW, cloudflared)
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import zipfile
from importlib import resources
from pathlib import Path

import httpx

from .engine import hardware
from .models import DownloadError, Manifest, download, models_root, profile_config, write_config

IS_WIN = sys.platform == "win32"
EST_S = {"T0": None, "T1": 30, "T2": 20, "T3": 14, "T4": 8, "T5": 5}  # rough s/image, turbo 768 px (T1 measured)
SERVICE = "qis"


def lock() -> dict:
    return json.loads(resources.files("qis.data").joinpath("comfy.lock.json").read_text(encoding="utf-8"))


def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    say(f"\n==> {msg}")


def ask(prompt: str, default: str = "", yes: bool = False) -> str:
    if yes or not sys.stdin or not sys.stdin.isatty():
        return default
    try:
        v = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        return default
    return v or default


def gb(n: int) -> str:
    return f"{n / 1024 ** 3:.1f} GB"


def default_root() -> Path:
    # installed layout: <root>/.venv/(Scripts|bin)/python
    exe = Path(sys.executable).resolve()
    if exe.parent.parent.name == ".venv":
        return exe.parent.parent.parent
    return Path("C:/qis") if IS_WIN else Path("/opt/qis")


def uv_exe(root: Path) -> str | None:
    for cand in (root / "bin" / ("uv.exe" if IS_WIN else "uv"), shutil.which("uv")):
        if cand and Path(cand).exists():
            return str(cand)
    return None


def pip_install(root: Path, args: list[str]) -> None:
    uv = uv_exe(root)
    cmd = [uv, "pip", "install", "--python", sys.executable, *args] if uv else [sys.executable, "-m", "pip", "install", *args]
    say("   $ " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def fetch(url: str, dest: Path, label: str = "") -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(60, read=300)) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done = 0
        last = 0.0
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1024 * 1024):
                f.write(chunk)
                done += len(chunk)
                if time.monotonic() - last > 2:
                    last = time.monotonic()
                    say(f"   {label or dest.name}: {done / 1e6:.0f}" + (f"/{total / 1e6:.0f}" if total else "") + " MB")
    tmp.replace(dest)
    return dest


# ------------------------------------------------------------------ ComfyUI
def install_comfy(root: Path, force: bool = False) -> Path:
    lk = lock()
    comfy = root / "comfyui"
    marker = comfy / ".qis-commit"
    if comfy.exists() and marker.exists() and marker.read_text().strip() == lk["comfyui"]["commit"] and not force:
        say(f"   ComfyUI {lk['comfyui']['version']} already installed")
    else:
        tmp = root / "tmp"
        z = fetch(lk["comfyui"]["url"], tmp / "comfyui.zip", "ComfyUI")
        extract = tmp / "comfyui_x"
        shutil.rmtree(extract, ignore_errors=True)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(extract)
        src = next(extract.iterdir())
        if comfy.exists():  # keep user custom_nodes / models / user folders across an upgrade
            keep = {n: comfy / n for n in ("custom_nodes", "models", "user") if (comfy / n).exists()}
            old = root / f"comfyui.old.{int(time.time())}"
            comfy.rename(old)
            src.rename(comfy)
            for n in keep:
                shutil.rmtree(comfy / n, ignore_errors=True)
                (old / n).rename(comfy / n)
            shutil.rmtree(old, ignore_errors=True)
        else:
            src.rename(comfy)
        marker.write_text(lk["comfyui"]["commit"])
        shutil.rmtree(tmp, ignore_errors=True)
    for node in lk["custom_nodes"]:
        dest = comfy / "custom_nodes" / node["name"]
        mk = dest / ".qis-commit"
        if dest.exists() and mk.exists() and mk.read_text().strip() == node["commit"] and not force:
            continue
        tmp = root / "tmp"
        z = fetch(node["url"], tmp / f"{node['name']}.zip", node["name"])
        extract = tmp / f"{node['name']}_x"
        shutil.rmtree(extract, ignore_errors=True)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(extract)
        shutil.rmtree(dest, ignore_errors=True)
        next(extract.iterdir()).rename(dest)
        mk.write_text(node["commit"])
        shutil.rmtree(tmp, ignore_errors=True)
    from .engine.comfy.supervisor import install_custom_nodes
    install_custom_nodes(comfy)
    return comfy


def install_deps(root: Path, comfy: Path, index: str) -> None:
    pins = lock().get("torch", {}).get(index, ["torch", "torchvision", "torchaudio"])
    pip_install(root, [*pins, "--index-url", f"https://download.pytorch.org/whl/{index}"])
    reqs = []
    for f in [comfy / "requirements.txt", comfy / "custom_nodes" / "ComfyUI-GGUF" / "requirements.txt"]:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            name = line.split("=")[0].split(">")[0].split("<")[0].split("~")[0].strip().lower()
            if line and name not in ("torch", "torchvision", "torchaudio"):
                reqs.append(line)
    req = root / "tmp_requirements.txt"
    req.write_text("\n".join(dict.fromkeys(reqs)) + "\n", encoding="utf-8")
    try:
        pip_install(root, ["-r", str(req)])
    finally:
        req.unlink(missing_ok=True)


def check_torch() -> tuple[bool, str]:
    code = "import torch,json;print(json.dumps([torch.__version__, torch.cuda.is_available(), torch.version.cuda, " \
           "torch.cuda.get_device_name(0) if torch.cuda.is_available() else '']))"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180)
    if r.returncode:
        return False, r.stderr.strip().splitlines()[-1] if r.stderr else "torch import failed"
    v, ok, cuda, name = json.loads(r.stdout.strip().splitlines()[-1])
    return ok, f"torch {v} cuda {cuda} {name}"


# ------------------------------------------------------------------ profile choice
def choose_profile(man: Manifest, hw: hardware.Hardware, a) -> tuple[str, list[str]]:
    vram = hw.gpus[0].vram_gb if hw.gpus else 0
    rec = hardware.choose_tier(vram)
    say(f"   GPU: {hw.gpus[0].name if hw.gpus else 'none'} ({vram:.1f} GB VRAM), RAM {hw.ram_gb:.0f} GB, "
        f"free disk {hw.disk_free_gb:.0f} GB -> recommended tier {rec}")
    for w in hw.warnings:
        say(f"   ! {w}")
    tier = a.tier or rec
    variant = a.variant
    if not a.yes and not a.tier:
        say("\n   Tiers:")
        for t, p in man.profiles.items():
            ids = man.profile_models(t)
            est = f"~{EST_S[t]} s/image" if EST_S.get(t) else "no image generation"
            mark = "  <- recommended" if t == rec else ""
            say(f"     {t}: {p['title']:<52} {gb(man.total_bytes(ids)):>8}  {est}{mark}")
        tier = ask("   Tier", rec).upper()
        if tier not in man.profiles:
            sys.exit(f"unknown tier {tier}")
        variants = ["recommended"] + [v for v in ("quality", "fast") if v in man.profiles[tier]]
        say("   Variants: recommended = balanced; quality = larger turbo quant; fast = smaller / fewer models; custom = pick")
        variant = ask(f"   Variant ({'/'.join(variants + ['custom'])})", "recommended")
    if tier not in man.profiles:
        sys.exit(f"unknown tier {tier}")
    if variant == "custom":
        ids = []
        for m in man.models.values():
            default = "y" if m.id in man.profile_models(tier) else "n"
            if ask(f"   {m.id:<16} {m.role:<9} {gb(m.bytes):>8} {'(non-commercial)' if not m.commercial_ok else ''}",
                   default, a.yes).lower().startswith("y"):
                ids.append(m.id)
    else:
        ids = man.profile_models(tier, variant if variant in man.profiles[tier] else "recommended")
    if tier != "T0" and vram and vram < 5.5 and not a.force:
        sys.exit(f"{vram:.1f} GB VRAM is below 6 GB: use --tier T0 (tools only) or --force")
    need = man.total_bytes(ids)
    say(f"   -> tier {tier}, models: {', '.join(ids)} ({gb(need)})")
    say("   License: Qwen-Image 2.1 models are under the Qwen Research License (NON-COMMERCIAL use only).")
    return tier, ids


# ------------------------------------------------------------------ services
def service_install(root: Path, cfg_path: Path, user: str | None = None) -> None:
    venv_qis = root / ".venv" / ("Scripts/qis.exe" if IS_WIN else "bin/qis")
    if IS_WIN:
        exe = root / "bin" / "qis-service.exe"
        if not exe.exists():
            fetch(lock()["winsw"], exe, "WinSW")
        data = root / "data" / "logs"
        data.mkdir(parents=True, exist_ok=True)
        (root / "bin" / "qis-service.xml").write_text(f"""<service>
  <id>{SERVICE}</id>
  <name>Qwen Image Service</name>
  <description>Qwen-Image 2.1 node: OpenAI-compatible image API, queue and dashboard</description>
  <executable>{venv_qis}</executable>
  <arguments>serve --config "{cfg_path}"</arguments>
  <workingdirectory>{root}</workingdirectory>
  <env name="PYTHONUNBUFFERED" value="1"/>
  <env name="PYTHONIOENCODING" value="utf-8"/>
  <startmode>Automatic</startmode>
  <delayedAutoStart>true</delayedAutoStart>
  <onfailure action="restart" delay="10 sec"/>
  <onfailure action="restart" delay="30 sec"/>
  <onfailure action="restart" delay="60 sec"/>
  <resetfailure>1 hour</resetfailure>
  <stoptimeout>45 sec</stoptimeout>
  <logpath>{data}</logpath>
  <log mode="roll-by-size"><sizeThreshold>10240</sizeThreshold><keepFiles>5</keepFiles></log>
</service>
""", encoding="utf-8")
        st = subprocess.run([str(exe), "status"], capture_output=True, text=True)
        if "NonExistent" in st.stdout or st.returncode:
            subprocess.run([str(exe), "install"], check=True)
        else:
            subprocess.run([str(exe), "stop"], capture_output=True)
            subprocess.run([str(exe), "refresh"], capture_output=True)
        subprocess.run([str(exe), "start"], check=True)
    else:
        user = user or os.environ.get("SUDO_USER") or "root"
        unit = f"""[Unit]
Description=Qwen Image Service (Qwen-Image 2.1 node)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
WorkingDirectory={root}
ExecStart={venv_qis} serve --config {cfg_path}
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=10
KillMode=control-group
TimeoutStopSec=45
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""
        if user != "root":  # the service writes data/ and models/, and ComfyUI writes its user/ folder
            subprocess.run(["chown", "-R", f"{user}:{user}", str(root)], check=True)
        Path(f"/etc/systemd/system/{SERVICE}.service").write_text(unit, encoding="utf-8")
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "--now", SERVICE], check=True)
        subprocess.run(["systemctl", "restart", SERVICE], check=True)


def service_cmd(root: Path, action: str) -> int:
    if IS_WIN:
        exe = root / "bin" / "qis-service.exe"
        if not exe.exists():
            say("service is not installed")
            return 1
        return subprocess.run([str(exe), action]).returncode
    mapping = {"status": ["systemctl", "status", SERVICE, "--no-pager"], "uninstall": ["systemctl", "disable", "--now", SERVICE]}
    return subprocess.run(mapping.get(action, ["systemctl", action, SERVICE])).returncode


def service_uninstall(root: Path) -> None:
    if IS_WIN:
        exe = root / "bin" / "qis-service.exe"
        if exe.exists():
            subprocess.run([str(exe), "stop"], capture_output=True)
            subprocess.run([str(exe), "uninstall"], capture_output=True)
    else:
        subprocess.run(["systemctl", "disable", "--now", SERVICE], capture_output=True)
        Path(f"/etc/systemd/system/{SERVICE}.service").unlink(missing_ok=True)
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)


def cloudflared_install(root: Path, token: str) -> None:
    lk = lock()["cloudflared"]
    if IS_WIN:
        exe = root / "bin" / "cloudflared.exe"
        if not exe.exists():
            fetch(lk["windows"], exe, "cloudflared")
    else:
        exe = Path("/usr/local/bin/cloudflared")
        if not exe.exists():
            fetch(lk["linux"], exe, "cloudflared")
            exe.chmod(0o755)
    subprocess.run([str(exe), "service", "uninstall"], capture_output=True)
    subprocess.run([str(exe), "service", "install", token], check=True)


# ------------------------------------------------------------------ setup
def wait_ready(port: int, timeout: float = 900) -> bool:
    end = time.time() + timeout
    last = ""
    while time.time() < end:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/readyz", timeout=5)
            if r.status_code == 200:
                return True
            st = r.json().get("comfy", "")
            if st != last:
                say(f"   ComfyUI: {st}")
                last = st
        except httpx.HTTPError:
            pass
        time.sleep(3)
    return False


def cmd_setup(a) -> None:
    root = Path(a.root).resolve() if a.root else default_root()
    root.mkdir(parents=True, exist_ok=True)
    cfg_path = root / "config" / "config.toml"
    models_dir = Path(a.models_dir).resolve() if a.models_dir else root / "models"
    say(f"Qwen Image Service setup -> {root}")

    step("1/8 Hardware")
    hw = hardware.detect(models_dir if models_dir.exists() else root)
    man = Manifest.load()
    tier, ids = choose_profile(man, hw, a)
    need = man.total_bytes(ids)
    if hw.disk_free_gb * 1024 ** 3 < need * 1.1 + 15 * 1024 ** 3 and not a.skip_models and not a.force:
        sys.exit(f"not enough disk: need ~{gb(need)} for models + ~15 GB for PyTorch/ComfyUI, {hw.disk_free_gb:.0f} GB free")

    step("2/8 ComfyUI (pinned)")
    comfy = install_comfy(root)

    step("3/8 PyTorch + ComfyUI dependencies")
    if a.skip_deps:
        say("   skipped (--skip-deps)")
    else:
        gpu = hw.gpus[0] if hw.gpus else None
        if gpu and (problem := hardware.driver_problem(gpu.compute_cap, gpu.driver)) and not a.force:
            sys.exit(f"{problem}. Update the NVIDIA driver and rerun (or --force).")
        index = a.torch_index or hardware.torch_index(gpu.compute_cap if gpu else None, gpu.driver if gpu else None)
        say(f"   torch index: {index}")
        install_deps(root, comfy, index)
        ok, msg = check_torch()
        say(f"   {msg}")
        if not ok and tier != "T0" and not a.force:
            sys.exit("PyTorch cannot see the GPU: update the NVIDIA driver, or rerun with --torch-index cu126/cu128")

    step("4/8 Models")
    if a.skip_models:
        say("   skipped (--skip-models)")
    else:
        reuse = [Path(p) for p in (a.reuse_models or [])]
        for i, mid in enumerate(ids, 1):
            m = man.models[mid]
            say(f"   [{i}/{len(ids)}] {m.file} ({gb(m.bytes)}) {m.license}")
            last = [0.0]

            def progress(done, total, m=m, last=last):
                if time.monotonic() - last[0] > 5 or done == total:
                    last[0] = time.monotonic()
                    say(f"       {done / 1024 ** 3:.2f}/{total / 1024 ** 3:.2f} GB")
            try:
                download(m, models_dir, progress, a.hf_token, verify=not a.no_verify, reuse_dirs=reuse)
            except DownloadError as e:
                sys.exit(f"download failed: {e}\n(rerun setup to resume)")

    step("5/8 Configuration")
    updates = profile_config(man, ids, tier)
    port = a.port
    updates["server"] = {"host": "0.0.0.0" if a.lan else "127.0.0.1", "port": port, "public_url": a.public_url or ""}
    updates["paths"] = {"data": str(root / "data"), "comfyui": str(comfy), "models": str(models_dir)}
    updates["comfy"] = {**updates["comfy"], "manage": True, "python": sys.executable, "port": a.comfy_port,
                        "warmup": True}
    write_config(cfg_path, updates)
    say(f"   wrote {cfg_path}")
    from .auth import ApiKeys, Users
    from .config import load_config
    from .db import DB
    cfg = load_config(cfg_path)
    db = DB(cfg.data_dir / "qis.db")
    users, keys = Users(db), ApiKeys(db, cfg.secret())

    step("6/8 Admin account + API key")
    admin_pw = None
    if users.count() == 0:
        pw = a.admin_password or os.environ.get("QIS_ADMIN_PASSWORD")
        generated = False
        if not pw and not a.yes and sys.stdin and sys.stdin.isatty():
            import getpass
            pw = getpass.getpass("   dashboard password for 'admin' (min 10 chars, empty = generate): ")
        if not pw:
            pw, generated = secrets.token_urlsafe(12), True
        users.create(a.admin_user, pw, email=a.admin_email, role="admin", must_change_pw=generated)
        admin_pw = pw if generated else None
        say(f"   admin user: {a.admin_user}" + (f"  password: {pw}  (change it at first login)" if generated else ""))
    else:
        say("   users already exist, keeping them")
    api_key = None
    if not keys.list():
        api_key, _ = keys.create("default")
    bench_key, bench_row = keys.create("installer-benchmark", scopes=["admin:*"])

    step("7/8 Service")
    if a.no_service:
        say(f"   skipped: run `{root / '.venv' / ('Scripts' if IS_WIN else 'bin') / 'qis'} serve --config {cfg_path}`")
    else:
        service_install(root, cfg_path, a.service_user)
        say(f"   service '{SERVICE}' installed and started")
        if a.tunnel_token:
            cloudflared_install(root, a.tunnel_token)
            say(f"   cloudflared installed: point the tunnel's public hostname at http://127.0.0.1:{port}")

    step("8/8 Readiness + benchmark")
    if a.no_service or a.skip_benchmark or tier == "T0" or a.skip_models:
        say("   skipped")
    elif wait_ready(port):
        say("   node is ready, benchmarking (a few minutes)...")
        try:
            from .bench import run_benchmark
            run_benchmark(cfg, f"http://127.0.0.1:{port}", bench_key, 3, apply=True)
        except SystemExit as e:
            say(f"   benchmark failed: {e}")
    else:
        say("   the node did not become ready in time; check `qis doctor` and data/logs/")
    keys.revoke(bench_row["id"])

    exe = root / ".venv" / ("Scripts" if IS_WIN else "bin") / "qis"
    say("\n" + "=" * 70)
    say(f" Dashboard : http://127.0.0.1:{port}/admin/" + (f"   public: {a.public_url}/admin/" if a.public_url else ""))
    say(f" API       : http://127.0.0.1:{port}/v1   (OpenAI compatible; docs at /docs)")
    if admin_pw:
        say(f" Admin     : {a.admin_user} / {admin_pw}   (change at first login)")
    if api_key:
        say(f" API key   : {api_key}   (shown once)")
        say(f"   curl http://127.0.0.1:{port}/v1/images/generations -H 'Authorization: Bearer {api_key}' "
            "-H 'Content-Type: application/json' -d '{\"model\":\"pixelart-16bit-64\",\"prompt\":\"a knight\"}'")
    say(f" Manage    : {exe} status | doctor | logs | update | uninstall")
    if a.tunnel_token:
        say(" Cloudflare: in Zero Trust > Access add a Bypass policy for /v1/* and /healthz, and an Allow-email policy for /admin*")
    say(" License   : Qwen-Image 2.1 weights are NON-COMMERCIAL (Qwen Research License).")
    say("=" * 70)


# ------------------------------------------------------------------ update / uninstall / logs / service
def cmd_update(a) -> None:
    root = Path(a.root).resolve() if a.root else default_root()
    app = root / "app"
    if not (app / ".git").exists():
        sys.exit(f"{app} is not a git checkout; reinstall with the installer to update")
    git = shutil.which("git") or sys.exit("git not found")

    def run(*cmd, **kw):
        return subprocess.run(cmd, cwd=app, check=True, capture_output=True, text=True, **kw).stdout.strip()
    old = run(git, "rev-parse", "HEAD")
    run(git, "fetch", "--tags", "origin")
    target = a.ref or "origin/HEAD"
    if a.check:
        new = run(git, "rev-parse", target)
        say(f"current {old[:10]}  available {new[:10]}  {'up to date' if new == old else 'update available'}")
        return
    from .config import load_config
    from .db import DB
    cfg = load_config(root / "config" / "config.toml")
    backup = cfg.data_dir / "backups" / f"pre-update-{time.strftime('%Y%m%d-%H%M%S')}.db"
    backup.parent.mkdir(parents=True, exist_ok=True)
    DB(cfg.data_dir / "qis.db").backup(backup)
    say(f"backup: {backup}")

    def apply(ref: str) -> None:
        run(git, "checkout", "--force", ref)
        pip_install(root, ["-e", str(app)])
        install_comfy(root)
        DB(cfg.data_dir / "qis.db").migrate()
        service_cmd(root, "restart")
    try:
        say(f"updating {old[:10]} -> {target}")
        apply(target)
        if not wait_ready(cfg.server.port, 180):
            raise RuntimeError("node not ready 3 minutes after the update")
        say("update done")
    except Exception as e:
        say(f"update failed ({e}); rolling back to {old[:10]}")
        shutil.copy2(backup, cfg.data_dir / "qis.db")
        apply(old)
        sys.exit(1)


def cmd_uninstall(a) -> None:
    root = Path(a.root).resolve() if a.root else default_root()
    if not a.yes and ask(f"Remove the qis service from {root}? (y/N)", "n").lower() != "y":
        return
    service_uninstall(root)
    say("service removed")
    if a.purge:
        for sub in ("app", "comfyui", "bin", "config", "data") + (("models",) if a.purge_models else ()):
            shutil.rmtree(root / sub, ignore_errors=True)
        say(f"removed {root} (models {'removed' if a.purge_models else 'kept'}); delete .venv manually after exiting")


def cmd_logs(a) -> None:
    from .config import load_config
    cfg = load_config(a.config)
    path = cfg.data_dir / "logs" / ("comfyui.log" if a.comfy else "gateway.log")
    if not path.exists():
        sys.exit(f"no log at {path}")
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - a.lines * 300))
        for line in f.read().decode("utf-8", "replace").splitlines()[-a.lines:]:
            print(line)
        while a.follow:
            chunk = f.read()
            if chunk:
                print(chunk.decode("utf-8", "replace"), end="", flush=True)
            time.sleep(1)


def cmd_service(a) -> None:
    root = Path(a.root).resolve() if a.root else default_root()
    if a.action == "install":
        service_install(root, root / "config" / "config.toml", a.user)
    elif a.action == "uninstall":
        service_uninstall(root)
    else:
        sys.exit(service_cmd(root, a.action))


def cmd_tunnel(a) -> None:
    root = Path(a.root).resolve() if a.root else default_root()
    cloudflared_install(root, a.token)
    say("cloudflared service installed")


def cmd_models(a) -> None:
    from .config import load_config
    from .models import installed
    cfg = load_config(a.config)
    man = Manifest.load()
    if a.action == "list":
        for m in installed(cfg, man):
            state = ("ok" if m.get("matches_manifest", True) else "local") if m["present"] else ("partial" if m["partial"] else "-")
            print(f"{m['id']:<18} {m['role']:<9} {m['bytes'] / 1024 ** 3:6.1f} GB  {state:<8} {'active' if m['active'] else '':<7} {m['file']}")
    elif a.action == "download":
        for mid in a.ids:
            if mid not in man.models:
                sys.exit(f"unknown model {mid}")
            def show(d, t, mid=mid):
                print(f"\r{mid}: {d / 1024 ** 3:.2f}/{t / 1024 ** 3:.2f} GB", end="", flush=True)
            download(man.models[mid], models_root(cfg), show, a.hf_token)
            print()
    elif a.action == "profile":
        tier = a.ids[0] if a.ids else sys.exit("give a tier, e.g. qis models profile T2")
        ids = man.profile_models(tier)
        if not cfg.source:
            sys.exit("no config file")
        write_config(Path(cfg.source), profile_config(man, ids, tier))
        print(f"profile {tier}: {', '.join(ids)} (download missing ones with `qis models download <id>`; restart the service)")


def register_cli(sub) -> None:
    p = sub.add_parser("setup", help="install / reconfigure this node (called by the installer)")
    p.add_argument("--root", help="install root (default: derived from the venv, else C:\\qis or /opt/qis)")
    p.add_argument("--tier", choices=["T0", "T1", "T2", "T3", "T4", "T5"])
    p.add_argument("--variant", default="recommended", choices=["recommended", "quality", "fast", "custom"])
    p.add_argument("--models-dir")
    p.add_argument("--reuse-models", action="append", help="directory with already downloaded model files (repeatable)")
    p.add_argument("--hf-token")
    p.add_argument("--no-verify", action="store_true", help="skip sha256 checks (not recommended)")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--comfy-port", type=int, default=8188)
    p.add_argument("--lan", action="store_true", help="listen on 0.0.0.0 instead of 127.0.0.1")
    p.add_argument("--public-url", default="")
    p.add_argument("--admin-user", default="admin")
    p.add_argument("--admin-email")
    p.add_argument("--admin-password")
    p.add_argument("--tunnel-token", help="Cloudflare tunnel token: installs cloudflared as a service")
    p.add_argument("--torch-index", help="override the PyTorch wheel index (cu130 / cu126)")
    p.add_argument("--service-user")
    p.add_argument("--no-service", action="store_true")
    p.add_argument("--skip-deps", action="store_true")
    p.add_argument("--skip-models", action="store_true")
    p.add_argument("--skip-benchmark", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("-y", "--yes", action="store_true", help="non-interactive, accept defaults")
    p.set_defaults(fn=cmd_setup)

    p = sub.add_parser("update", help="update the app (git) with DB backup and automatic rollback")
    p.add_argument("--root")
    p.add_argument("--ref", help="tag / branch / commit (default: origin/HEAD)")
    p.add_argument("--check", action="store_true")
    p.set_defaults(fn=cmd_update)

    p = sub.add_parser("uninstall", help="remove the service (and optionally files)")
    p.add_argument("--root")
    p.add_argument("--purge", action="store_true")
    p.add_argument("--purge-models", action="store_true")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(fn=cmd_uninstall)

    p = sub.add_parser("logs", help="print (and follow) the gateway or ComfyUI log")
    p.add_argument("--config")
    p.add_argument("--comfy", action="store_true")
    p.add_argument("-n", "--lines", type=int, default=100)
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(fn=cmd_logs)

    p = sub.add_parser("service", help="manage the OS service")
    p.add_argument("action", choices=["install", "uninstall", "start", "stop", "restart", "status"])
    p.add_argument("--root")
    p.add_argument("--user")
    p.set_defaults(fn=cmd_service)

    p = sub.add_parser("tunnel", help="install cloudflared as a service with a tunnel token")
    p.add_argument("token")
    p.add_argument("--root")
    p.set_defaults(fn=cmd_tunnel)

    p = sub.add_parser("models", help="list / download models, switch profile")
    p.add_argument("action", choices=["list", "download", "profile"])
    p.add_argument("ids", nargs="*")
    p.add_argument("--config")
    p.add_argument("--hf-token")
    p.set_defaults(fn=cmd_models)
