"""`qis doctor`: check everything a node needs, print a verdict per item, exit non-zero on failures."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

from .config import Config
from .engine import hardware
from .engine.comfy.supervisor import NODES_SRC
from .models import Manifest, installed, models_root

OK, WARN, FAIL = "ok", "warn", "FAIL"


def run_doctor(cfg: Config, as_json: bool = False) -> int:
    checks: list[tuple[str, str, str]] = []

    def add(name, status, detail=""):
        checks.append((name, status, detail))

    hw = hardware.detect(cfg.data_dir if cfg.data_dir.exists() else ".")
    if hw.gpus:
        g = hw.gpus[0]
        add("gpu", OK, f"{g.name} {g.vram_gb:.1f} GB, driver {g.driver}, compute {g.compute_cap}")
    else:
        add("gpu", FAIL if cfg.profile.tier != "T0" else WARN, "no NVIDIA GPU visible to nvidia-smi")
    for w in hw.warnings:
        add("hardware", WARN, w)
    add("ram", OK if hw.ram_gb >= 30 else WARN, f"{hw.ram_gb:.0f} GB")

    comfy = Path(cfg.paths.comfyui) if cfg.paths.comfyui else None
    if not cfg.comfy.manage:
        add("comfyui", OK, f"attach mode: {cfg.comfy_url}")
    elif not comfy or not (comfy / "main.py").exists():
        add("comfyui", FAIL, f"main.py not found in {comfy} (paths.comfyui)")
    else:
        marker = comfy / ".qis-commit"
        add("comfyui", OK, f"{comfy} ({marker.read_text().strip()[:10] if marker.exists() else 'unpinned'})")
        for n in ["ComfyUI-GGUF"] + [p.name for p in NODES_SRC.iterdir() if not p.name.startswith(("_", "."))]:
            present = (comfy / "custom_nodes" / n).exists()
            add(f"node {n}", OK if present else WARN, "" if present else "missing (installed on next start)")
        py = cfg.comfy.python or sys.executable
        code = ("import torch,json;print(json.dumps([torch.__version__, torch.cuda.is_available(), torch.version.cuda]))")
        try:
            r = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=180)
            v, cuda_ok, cuda = json.loads(r.stdout.strip().splitlines()[-1])
            add("torch", OK if cuda_ok else FAIL, f"{v} CUDA {cuda} available={cuda_ok} ({py})")
        except Exception as e:
            add("torch", FAIL, f"cannot import torch with {py}: {e}")

    man = Manifest.load()
    root = models_root(cfg)
    ms = cfg.models.model_set()
    wanted = set(ms.unets.values()) | {ms.clip, ms.vae} | set(ms.upscalers.values())
    for m in installed(cfg, man):
        if m["file"] not in wanted:
            continue
        if m["present"] and m.get("matches_manifest", True):
            add(f"model {m['file']}", OK, f"{m['bytes'] / 1024 ** 3:.1f} GB")
        elif m["present"]:
            add(f"model {m['file']}", WARN, f"differs from the manifest build ({m['size_on_disk']} bytes): usable, not verified")
        else:
            add(f"model {m['file']}", FAIL, f"missing in {root / m['dir']} (qis models download {m['id']})")
    if not any(m["file"] in wanted for m in installed(cfg, man)):
        add("models", WARN, f"no configured model found under {root}")

    free = shutil.disk_usage(cfg.data_dir if cfg.data_dir.exists() else Path.cwd()).free / 1024 ** 3
    add("disk", OK if free > 10 else WARN, f"{free:.0f} GB free for data")
    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        (cfg.data_dir / ".write-test").write_text("x")
        (cfg.data_dir / ".write-test").unlink()
        add("data dir", OK, str(cfg.data_dir))
    except OSError as e:
        add("data dir", FAIL, f"{cfg.data_dir} not writable: {e}")

    base = f"http://127.0.0.1:{cfg.server.port}"
    try:
        h = httpx.get(base + "/healthz", timeout=5).json()
        r = httpx.get(base + "/readyz", timeout=5)
        rd = r.json()
        add("gateway", OK, f"{base} v{h.get('version')}")
        add("engine", OK if r.status_code == 200 else WARN,
            f"comfy={rd.get('comfy')} warm={rd.get('warm')} missing_nodes={rd.get('missing_nodes')}")
    except httpx.HTTPError:
        add("gateway", WARN, f"not running on {base}")

    cf = shutil.which("cloudflared") or (Path(cfg.data_dir).parent / "bin" / "cloudflared.exe")
    if cf and Path(cf).exists():
        add("cloudflared", OK, str(cf))
    else:
        add("cloudflared", WARN, "not installed (optional: qis tunnel <token>)")

    if as_json:
        print(json.dumps([{"check": n, "status": s, "detail": d} for n, s, d in checks], indent=1))
    else:
        for n, s, d in checks:
            print(f"[{s:>4}] {n:<34} {d}")
        fails = sum(1 for c in checks if c[1] == FAIL)
        print(f"\n{fails} failure(s), {sum(1 for c in checks if c[1] == WARN)} warning(s)")
    return 1 if any(c[1] == FAIL for c in checks) else 0


__all__ = ["run_doctor"]
