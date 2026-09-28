"""Upscale an approved draft to its final size (Real-ESRGAN via the service, then Lanczos to the exact size).

  python tools/upscale.py draft.png --size 1080x1920 [--model realesrgan-x2|realesrgan-x4|nearest] [--out final.png]

Real-ESRGAN (BSD-3) is used by default because its licence allows commercial use. Needs QIS_API_KEY.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qis.client import Client  # noqa: E402

LEGACY = {"RealESRGAN_x2plus.pth": "realesrgan-x2", "RealESRGAN_x4plus.pth": "realesrgan-x4"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--size", required=True, help="final WxH, e.g. 1080x1920")
    ap.add_argument("--model", default="realesrgan-x2", help="upscaler key, or nearest / lanczos (no model)")
    ap.add_argument("--out", help="output path (default: <image>_<W>x<H>.png next to the input)")
    a = ap.parse_args()
    w, h = (int(x) for x in a.size.lower().split("x"))
    src = Path(a.image)
    out = Path(a.out) if a.out else src.with_name(f"{src.stem}_{w}x{h}.png")
    model = LEGACY.get(a.model, a.model)
    op = {"op": "upscale", "image": "$inputs.src", "width": w, "height": h}
    op.update({"method": model} if model in ("nearest", "lanczos") else {"method": "esrgan", "model": model})
    c = Client()
    t = time.time()
    job = c.run_job({"inputs": {"src": c.upload(src)}, "ops": [op]})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(c.download(job["result"]["outputs"][0]["images"][0]["url"]))
    print(f"{model}: {time.time() - t:.1f}s -> {out}")


if __name__ == "__main__":
    main()
