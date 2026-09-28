"""Diffusion upscale ("hires fix") for an approved draft: resize, then re-denoise lightly (via the service).

Keeps the composition of the draft and re-creates fine texture at the new size, which an ESRGAN upscaler smooths
away (e.g. knitted yarn turns plastic). Works on RGBA too: the Qwen-Image 2.1 VAE encodes and decodes alpha.

  python tools/refine.py model.png --size 896x1152 --prompt-file prompt.txt [--nodes "0.75, 0.5, 0.25"]

--nodes are raw Viggle sigma nodes; the node applies the resolution shift, so the real noise level is higher than the
number (at ~1.8 MP: raw 0.25 -> ~0.41, raw 0.5 -> ~0.68). "0.25, 0.12" keeps approved content and small details
(default); "0.5, 0.25" redraws more texture but can change small objects; "0.75, 0.5, 0.25" invents new detail.
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
    ap.add_argument("--size", required=True, help="WxH to refine at (multiples of 32)")
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--nodes", default="0.25, 0.12", help="turbo engine: raw Viggle sigma nodes")
    ap.add_argument("--engine", default="turbo", choices=["turbo", "base", "base_q8"],
                    help="turbo = Viggle GGUF, few steps; base / base_q8 = original model with KSampler denoise")
    ap.add_argument("--denoise", type=float, default=0.45, help="base engines: how much of the image is redrawn")
    ap.add_argument("--steps", type=int, default=24, help="base engines: full-schedule steps (denoise uses a part)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", help="output path (default: <image>_refined.png)")
    a = ap.parse_args()
    w, h = (int(x) for x in a.size.lower().split("x"))
    src = Path(a.image)
    out = Path(a.out) if a.out else src.with_name(f"{src.stem}_refined.png")
    prompt = Path(a.prompt_file).read_text(encoding="utf-8").strip()
    c = Client()
    t = time.time()
    job = c.run_job({"inputs": {"src": c.upload(src)}, "ops": [{
        "op": "refine", "image": "$inputs.src", "prompt": prompt, "width": w, "height": h, "engine": a.engine,
        "nodes": a.nodes, "denoise": a.denoise, "steps": a.steps, "seed": a.seed}]})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(c.download(job["result"]["outputs"][0]["images"][0]["url"]))
    how = f"nodes=[{a.nodes}]" if a.engine == "turbo" else f"{a.engine} denoise={a.denoise} steps={a.steps}"
    print(f"refined {w}x{h} {how} in {time.time() - t:.1f}s -> {out}")


if __name__ == "__main__":
    main()
