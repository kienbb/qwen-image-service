"""Diffusion upscale ("hires fix") for an approved draft: resize, then re-denoise lightly with the turbo model.

Keeps the composition of the draft and re-creates fine texture at the new size, which an ESRGAN upscaler smooths
away (e.g. knitted yarn turns plastic). Works on RGBA too: the Qwen-Image 2.1 VAE encodes and decodes alpha.

  python tools/refine.py model.png --size 896x1152 --prompt-file prompt.txt [--nodes "0.75, 0.5, 0.25"]

--nodes are raw Viggle sigma nodes; the node applies the resolution shift, so the real noise level is higher than the
number (at ~1.8 MP: raw 0.25 -> ~0.41, raw 0.5 -> ~0.68). "0.25, 0.12" keeps approved content and small details
(default); "0.5, 0.25" redraws more texture but can change small objects (a wall clock turned into a fan);
"0.75, 0.5, 0.25" invents new detail.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gen  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--size", required=True, help="WxH to refine at (multiples of 32)")
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--nodes", default="0.25, 0.12", help="turbo engine: raw Viggle sigma nodes")
    ap.add_argument("--engine", default="turbo", choices=["turbo", "base", "base_q8"],
                    help="turbo = Viggle GGUF, few steps (fast, slightly grainy surfaces); base / base_q8 = original "
                         "model with KSampler denoise (slower, much cleaner smooth materials)")
    ap.add_argument("--denoise", type=float, default=0.45, help="base engines: how much of the image is redrawn")
    ap.add_argument("--steps", type=int, default=24, help="base engines: full-schedule steps (denoise uses a part)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", help="output path (default: <image>_refined.png)")
    a = ap.parse_args()

    w, h = (int(x) for x in a.size.lower().split("x"))
    src = Path(a.image)
    out = Path(a.out) if a.out else src.with_name(f"{src.stem}_refined.png")
    prompt = Path(a.prompt_file).read_text(encoding="utf-8").strip()
    wf = {
        "load": {"class_type": "LoadImage", "inputs": {"image": gen.upload(str(src))}},
        "rgba": {"class_type": "JoinImageWithAlpha", "inputs": {"image": ["load", 0], "alpha": ["load", 1]}},
        "scale": {"class_type": "ImageScale", "inputs": {"image": ["rgba", 0], "upscale_method": "lanczos",
                                                           "width": w, "height": h, "crop": "disabled"}},
        "unet": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": gen.UNETS["viggle" if a.engine == "turbo" else a.engine]}},
        "cache": {"class_type": "QwenImage21Cache", "inputs": {"model": ["unet", 0], "device": "auto", "dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": gen.CLIP, "type": "qwen_image", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": gen.VAE}},
        "enc": {"class_type": "TextEncodeQwenImage21", "inputs": {"clip": ["clip", 0], "prompt": prompt,
                                                                   "negative_prompt": "", "resolution": max(w, h)}},
        "latent": {"class_type": "VAEEncode", "inputs": {"pixels": ["scale", 0], "vae": ["vae", 0]}},
        "sigmas": {"class_type": "ViggleTurboSigmas", "inputs": {"latent": ["latent", 0], "nodes": a.nodes}},
        "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": a.seed}},
        "guider": {"class_type": "BasicGuider", "inputs": {"model": ["cache", 0], "conditioning": ["enc", 0]}},
        "sampler": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "sample": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["noise", 0], "guider": ["guider", 0], "sampler": ["sampler", 0],
            "sigmas": ["sigmas", 0], "latent_image": ["latent", 0]}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": f"refine/{src.stem}"}},
    }
    if a.engine != "turbo":
        for k in ("sigmas", "noise", "guider", "sampler"):
            del wf[k]
        wf["sample"] = {"class_type": "KSampler", "inputs": {
            "model": ["cache", 0], "positive": ["enc", 0], "negative": ["enc", 1], "latent_image": ["latent", 0],
            "seed": a.seed, "steps": a.steps, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
            "denoise": a.denoise}}
    t = time.time()
    res = gen.run(wf)
    img = res["outputs"]["save"]["images"][0]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes((gen.ROOT / "output" / img["subfolder"] / img["filename"]).read_bytes())
    how = f"nodes=[{a.nodes}]" if a.engine == "turbo" else f"{a.engine} denoise={a.denoise} steps={a.steps}"
    print(f"refined {w}x{h} {how} in {time.time() - t:.1f}s -> {out}")


if __name__ == "__main__":
    main()
