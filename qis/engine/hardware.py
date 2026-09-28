"""Hardware detection (nvidia-smi / NVML, RAM, disk) and the VRAM tier -> model profile choice (DESIGN §12.3)."""
from __future__ import annotations

import contextlib
import os
import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


def hostname() -> str:
    return socket.gethostname()


@dataclass
class Gpu:
    index: int
    name: str
    vram_mb: int
    driver: str
    compute_cap: str

    @property
    def vram_gb(self) -> float:
        return self.vram_mb / 1024


@dataclass
class Hardware:
    os: str
    arch: str
    cpu_cores: int
    ram_gb: float
    disk_free_gb: float
    gpus: list[Gpu] = field(default_factory=list)
    nvidia_smi: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["gpus"] = [asdict(g) | {"vram_gb": round(g.vram_gb, 1)} for g in self.gpus]
        return d


def _nvidia_smi() -> str | None:
    exe = shutil.which("nvidia-smi")
    if not exe and sys.platform == "win32":
        cand = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
        exe = str(cand) if cand.exists() else None
    return exe


def detect_gpus() -> list[Gpu]:
    exe = _nvidia_smi()
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "--query-gpu=index,name,memory.total,driver_version,compute_cap",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=20, check=True).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 5:
            with contextlib.suppress(ValueError):
                gpus.append(Gpu(int(parts[0]), parts[1], int(float(parts[2])), parts[3], parts[4]))
    return gpus


def ram_gb() -> float:
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(st)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return st.ullTotalPhys / 1024 ** 3
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except (OSError, ValueError, AttributeError):
        return 0.0


def detect(path: str | Path = ".") -> Hardware:
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    gpus = detect_gpus()
    hw = Hardware(os=platform.system(), arch=platform.machine(), cpu_cores=os.cpu_count() or 1,
                  ram_gb=round(ram_gb(), 1), disk_free_gb=round(shutil.disk_usage(p).free / 1024 ** 3, 1),
                  gpus=gpus, nvidia_smi=_nvidia_smi() is not None)
    if not gpus:
        hw.warnings.append("no NVIDIA GPU found (nvidia-smi missing or no device): only the tools-only profile is possible")
    else:
        g = gpus[0]
        with contextlib.suppress(ValueError):
            if float(g.compute_cap) < 6.0:
                hw.warnings.append(f"{g.name} (compute {g.compute_cap}) is too old for current PyTorch builds")
        major = int(g.driver.split(".")[0]) if g.driver.split(".")[0].isdigit() else 0
        if major and major < 570:
            hw.warnings.append(f"driver {g.driver} is older than 570: CUDA 12.8 wheels need a newer driver")
    if hw.ram_gb and hw.ram_gb < 30:
        hw.warnings.append(f"{hw.ram_gb:.0f} GB RAM: the 9.4 GB text encoder streams from RAM; 32 GB+ is recommended")
    return hw


# ------------------------------------------------------------------ tiers
TIERS = [  # (tier, min VRAM GB, description)
    ("T0", 0, "< 6 GB hoặc không có GPU NVIDIA: chỉ các tool CPU (pixelize, upscale, compose)"),
    ("T1", 5.5, "6-8 GB: turbo Q4_K_M, encoder int8 stream, Real-ESRGAN"),
    ("T2", 9.5, "10-12 GB: turbo Q8_0 + base Q4_K_M"),
    ("T3", 15, "16 GB: turbo Q8_0 + base Q8_0"),
    ("T4", 22, "24 GB: Q8_0, encoder nằm hẳn trong VRAM"),
    ("T5", 30, ">= 32 GB: turbo/base BF16"),
]


def choose_tier(vram_gb: float | None) -> str:
    if not vram_gb:
        return "T0"
    tier = "T0"
    for name, min_gb, _ in TIERS:
        if vram_gb >= min_gb:
            tier = name
    return tier


def torch_index(compute_cap: str | None) -> str:
    """PyTorch wheel index for the GPU: cu128 for Turing+ (and required by Blackwell), cu126 for Pascal."""
    try:
        cc = float(compute_cap or 0)
    except ValueError:
        cc = 0
    return "cu126" if 0 < cc < 7.5 else "cu128"


def nvml_sample(device: int = 0) -> dict:
    """Live GPU numbers for the dashboard; empty dict when NVML is unavailable."""
    try:
        import pynvml
    except ImportError:
        return {}
    try:
        pynvml.nvmlInit()
    except Exception:
        return {}
    try:
        h = pynvml.nvmlDeviceGetHandleByIndex(device)
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        util = pynvml.nvmlDeviceGetUtilizationRates(h)
        name = pynvml.nvmlDeviceGetName(h)
        out = {"name": name.decode() if isinstance(name, bytes) else name, "memory_total": mem.total, "memory_used": mem.used,
               "util_gpu": util.gpu, "util_mem": util.memory}
        with contextlib.suppress(Exception):
            out["temperature"] = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
        with contextlib.suppress(Exception):
            out["power_w"] = round(pynvml.nvmlDeviceGetPowerUsage(h) / 1000, 1)
        with contextlib.suppress(Exception):
            drv = pynvml.nvmlSystemGetDriverVersion()
            out["driver"] = drv.decode() if isinstance(drv, bytes) else drv
        return out
    finally:
        with contextlib.suppress(Exception):
            pynvml.nvmlShutdown()
