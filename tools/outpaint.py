"""Resize an image to a new aspect ratio without redrawing it: scale uniformly, then outpaint only the new area.

Runs the service's outpaint op: hint canvas -> Qwen-Image 2.1 edit at draft size -> Real-ESRGAN to the final size ->
light refine pass -> the ORIGINAL pixels pasted back with a feathered seam (the source is preserved, only scaled).

  python tools/outpaint.py src.png --size 1080x1920 --prompt "..." [--anchor-y 0.44] [--seed 1] --out out.png

--anchor-y places the centre of the original image (fraction of the final height); keep focal points out of
areas a UI will cover (safezone).
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qis.client import Client  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--size", required=True, help="final WxH")
    ap.add_argument("--prompt", required=True, help="what the extended area should contain")
    ap.add_argument("--anchor-y", type=float, default=0.5)
    ap.add_argument("--draft", type=int, default=768, help="draft area side (draft has about draft*draft pixels)")
    ap.add_argument("--steps", type=int, default=6)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--feather", type=int, default=24, help="seam width in final pixels")
    ap.add_argument("--scene", default="", help="short description of the whole image, used by the refine pass")
    ap.add_argument("--refine-nodes", default="0.5, 0.25", help="Viggle sigma nodes for the final refine; empty = ESRGAN only")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    w, h = (int(x) for x in a.size.lower().split("x"))
    c = Client()
    t = time.time()
    job = c.run_job({"inputs": {"src": c.upload(a.image)}, "ops": [{
        "op": "outpaint", "image": "$inputs.src", "width": w, "height": h, "prompt": a.prompt, "anchor_y": a.anchor_y,
        "draft": a.draft, "steps": a.steps, "seed": a.seed, "feather": a.feather, "scene": a.scene,
        "refine_nodes": a.refine_nodes}]})
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(c.download(job["result"]["outputs"][0]["images"][0]["url"]))
    print(f"outpaint {w}x{h} in {time.time() - t:.1f}s -> {out}")


if __name__ == "__main__":
    main()
