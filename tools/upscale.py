"""Upscale an approved draft to its final size with an ESRGAN-family model via the local ComfyUI.

  python tools/upscale.py draft.png --size 1080x1920 [--model RealESRGAN_x2plus.pth] [--out final.png]

The model upscales by its native factor (x2/x4), then a Lanczos resize lands on the exact target size.
Real-ESRGAN (BSD-3) is used by default because its licence allows commercial use.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gen  # noqa: E402  (ComfyUI API helpers)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--size", required=True, help="final WxH, e.g. 1080x1920")
    ap.add_argument("--model", default="RealESRGAN_x2plus.pth", help="file in ComfyUI/models/upscale_models")
    ap.add_argument("--out", help="output path (default: <image>_<W>x<H>.png next to the input)")
    a = ap.parse_args()

    w, h = (int(x) for x in a.size.lower().split("x"))
    src = Path(a.image)
    out = Path(a.out) if a.out else src.with_name(f"{src.stem}_{w}x{h}.png")
    prefix = f"upscale/{src.stem}_{Path(a.model).stem}"
    wf = {
        "load": {"class_type": "LoadImage", "inputs": {"image": gen.upload(str(src))}},
        "model": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": a.model}},
        "up": {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["model", 0], "image": ["load", 0]}},
        "fit": {"class_type": "ImageScale", "inputs": {"image": ["up", 0], "upscale_method": "lanczos",
                                                         "width": w, "height": h, "crop": "disabled"}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["fit", 0], "filename_prefix": prefix}},
    }
    t = time.time()
    res = gen.run(wf)
    img = res["outputs"]["save"]["images"][0]
    saved = gen.ROOT / "output" / img["subfolder"] / img["filename"]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(saved.read_bytes())
    print(f"{a.model}: {time.time() - t:.1f}s -> {out}")


if __name__ == "__main__":
    main()
