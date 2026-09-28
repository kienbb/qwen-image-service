"""Generate / edit retro pixel-art game assets with Qwen-Image 2.1 (GGUF) via a local ComfyUI.

Start ComfyUI first (start_comfyui.bat), then e.g.:
  text -> image      python gen.py "a knight with a blue cape and a sword"
  small icon         python gen.py "health potion, red liquid in a glass flask" --grid 32
  image edit         python gen.py "make his armor gold and his cape red" --ref output/knight/raw_00001_.png
  multi-reference    python gen.py "the character from image 1 holding the weapon from image 2" --ref hero.png --ref axe.png
  batch              python gen.py "forest slime monster" --count 4 --size 768
  prompt enhancer    python gen.py "a goblin archer" --enhance   (official Qwen rewriter on the local text encoder, cached)
  UI workflow        python gen.py --dump-workflow workflow_api.json   (drag the file into the ComfyUI web UI)

Outputs land in ./output/<name>/: raw render, sprite at native grid size, nearest-upscaled preview.
Stdlib only - runs on any Python 3.9+.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import mimetypes
import random
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SERVER = "http://127.0.0.1:8188"
ROOT = Path(__file__).parent
PE_CACHE = ROOT / "output" / "pe_cache.json"

UNETS = {"base": "qwen-image-2.1-Q4_K_M.gguf", "base_q8": "qwen-image-2.1-Q8_0.gguf",
         "viggle": "qwen-image-2.1-viggle-Q4_K_M.gguf"}  # base + Viggle turbo v0.2.1 baked in (tools/merge_lora.py)
VIGGLE_SIGMAS = {
    4: "1.0, 0.75, 0.5, 0.25",
    5: "1.0, 0.875, 0.75, 0.5, 0.25",
    6: "1.0, 0.9375, 0.875, 0.75, 0.5, 0.25",
    7: "1.0, 0.9583, 0.9167, 0.875, 0.75, 0.5, 0.25",
    8: "1.0, 0.9375, 0.875, 0.75, 0.625, 0.5, 0.25, 0.125"}
CLIP = "qwen3vl_8b_int8_convrot.safetensors"
VAE = "qwen_image_2.1_vae_bf16.safetensors"

# sampling modes: Viggle-baked student (custom sigmas, no CFG) or the original model (KSampler)
MODES = {
    # raw sigma nodes; ViggleTurboSigmas applies the resolution shift. Extra steps only split the high-noise end
    "turbo": {"unet": "viggle", "steps": 5, "sigmas": VIGGLE_SIGMAS},
    "base": {"unet": "base", "steps": 10},
    "base_q8": {"unet": "base_q8", "steps": 20},  # 8-bit weights: cleaner surfaces, 7.6 GB so partly offloaded on 6 GB
}

STYLES = {
    "16bit": ("16-bit retro pixel art in the style of a SNES / Sega Genesis game sprite, crisp chunky pixels, "
              "bold dark outlines, limited palette, flat cel shading with 2-3 tone ramps, no gradients, no blur, "
              "no anti-aliasing"),
    "32bit": ("32-bit era pixel art in the style of a GBA / PlayStation 1 / Sega Saturn 2D game, detailed "
              "hand-placed pixels, clean outlines, rich but limited palette, careful dithering-free shading, "
              "no blur"),
    "none": "",
}
SUBJECT_HINT = "Single game asset, centered, full object visible"
EDIT_HINT = "Keep the same art style, proportions and background."
# Qwen-Image 2.1 native transparency (see official Comfy-Org template note)
RGBA_WRAP = ("This is an RGBA format image with transparency. {} "
             "The image has an alpha channel and a transparent background.")


def build_prompt(subject: str, style: str, transparent: bool, edit: bool) -> str:
    subject = subject.strip().rstrip(".")
    subject = subject[:1].upper() + subject[1:]
    if edit:  # instruction first, the references already carry the look
        text = f"{subject}. {EDIT_HINT}"
    else:
        hint = SUBJECT_HINT if transparent else SUBJECT_HINT + ", plain flat white background"
        text = ". ".join(p for p in (STYLES[style], subject, hint) if p) + "."
    return RGBA_WRAP.format(text) if transparent else text


def dims(size: int, ratio: str) -> tuple[int, int]:
    """Width/height with about size*size pixels at the W:H ratio, multiples of 32."""
    rw, rh = (float(x) for x in ratio.split(":"))
    area = size * size
    return max(32, round(math.sqrt(area * rw / rh) / 32) * 32), max(32, round(math.sqrt(area * rh / rw) / 32) * 32)


def enhance(request: str, refs: list[str], ref_paths: list[str], clip_device: str) -> tuple[str, str]:
    """Rewrite the prompt with the official Qwen-Image 2.1 rewriter system prompt, run on the Qwen3-VL text encoder
    (as in Viggle's ComfyUI workflows). Returns (prompt, wh_ratio); results are cached per request + reference files."""
    key = hashlib.sha1(json.dumps([request] + [hashlib.sha1(Path(r).read_bytes()).hexdigest() for r in ref_paths]).encode()).hexdigest()
    cache = json.loads(PE_CACHE.read_text(encoding="utf-8")) if PE_CACHE.exists() else {}
    if key in cache:
        return cache[key]["prompt"], cache[key]["ratio"]

    system = (ROOT / "prompts" / ("pe_edit_system.txt" if refs else "pe_t2i_system.txt")).read_text(encoding="utf-8")
    vision = "<|vision_start|><|image_pad|><|vision_end|>" * len(refs)
    user = f"{request}\n(Write the description in English.)" if refs else request
    wf = {
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP, "type": "qwen_image", "device": clip_device}},
        # a prompt that starts with <|im_start|> is used as the full chat, so the system turn is ours
        "gen": {"class_type": "TextGenerate", "inputs": {
            "clip": ["clip", 0], "max_length": 512, "sampling_mode": "off", "thinking": True,
            "use_default_template": True, "mtp": "auto",
            "prompt": f"<|im_start|>system\n{system}<|im_end|>\n<|im_start|>user\n{vision}{user}<|im_end|>\n<|im_start|>assistant\n"}},
        "show": {"class_type": "PreviewAny", "inputs": {"source": ["gen", 0]}},
    }
    if refs:  # the rewriter sees the references, resized to image 1
        wf["batch"] = {"class_type": "BatchImagesNode", "inputs": {}}
        for i, ref in enumerate(refs):
            wf[f"pe_ref{i}"] = {"class_type": "LoadImage", "inputs": {"image": ref}}
            wf["batch"]["inputs"][f"images.image{i}"] = [f"pe_ref{i}", 0]
        wf["gen"]["inputs"]["image"] = ["batch", 0]

    t = time.time()
    text = run(wf)["outputs"]["show"]["text"][0]
    try:
        res = json.loads(text[text.index("{"):text.rindex("}") + 1])
        prompt, ratio = res["rewritten_prompt"].strip(), res.get("wh_ratio") or ""
    except (ValueError, KeyError):
        print(f"enhancer output did not parse, using the prompt as written:\n{text[:500]}")
        return request, ""
    print(f"enhanced in {time.time() - t:.0f}s (ratio {ratio or 'follows image 1'})")
    cache[key] = {"request": request, "prompt": prompt, "ratio": ratio}
    PE_CACHE.parent.mkdir(exist_ok=True)
    PE_CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")
    return prompt, ratio


def build_workflow(a: argparse.Namespace, prompt: str, seed: int, name: str, refs: list[str]) -> dict:
    mode = MODES[a.mode]
    wf = {
        "unet": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": UNETS[mode["unet"]]}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP, "type": "qwen_image", "device": a.clip_device}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "enc": {"class_type": "TextEncodeQwenImage21", "inputs": {
            "clip": ["clip", 0], "prompt": prompt, "negative_prompt": a.negative, "resolution": a.size}},
    }
    model = ["unet", 0]
    steps = a.steps or mode["steps"]
    if "sigmas" in mode and steps not in mode["sigmas"]:
        sys.exit(f"--mode {a.mode} supports --steps {sorted(mode['sigmas'])}")
    wf["cache"] = {"class_type": "QwenImage21Cache", "inputs": {"model": model, "device": "auto", "dtype": "default"}}
    model = ["cache", 0]

    for i, ref in enumerate(refs, 1):
        wf[f"ref{i}"] = {"class_type": "LoadImage", "inputs": {"image": ref}}
        wf["enc"]["inputs"][f"images.image_{i}"] = [f"ref{i}", 0]
    if refs:  # edit: output size follows image 1 at ~size x size pixels
        wf["enc"]["inputs"]["vae"] = ["vae", 0]
        latent = ["enc", 2]
    else:
        w, h = dims(a.size, a.aspect)
        wf["empty"] = {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}}
        latent = ["empty", 0]

    if a.mode in ("base", "base_q8"):
        wf["sample"] = {"class_type": "KSampler", "inputs": {
            "model": model, "positive": ["enc", 0], "negative": ["enc", 1], "latent_image": latent,
            "seed": seed, "steps": steps, "cfg": a.cfg, "sampler_name": "euler",
            "scheduler": "simple", "denoise": 1.0}}
    else:
        wf["sigmas"] = {"class_type": "ViggleTurboSigmas", "inputs": {"latent": latent, "nodes": mode["sigmas"][steps]}}
        wf["noise"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}}
        wf["guider"] = {"class_type": "BasicGuider", "inputs": {"model": model, "conditioning": ["enc", 0]}}
        wf["sampler"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}}
        wf["sample"] = {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["noise", 0], "guider": ["guider", 0], "sampler": ["sampler", 0],
            "sigmas": ["sigmas", 0], "latent_image": latent}}

    wf["decode"] = {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}}
    wf["save_raw"] = {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": f"{name}/raw"}}
    if a.grid:
        wf["pixel"] = {"class_type": "PixelArtRetro", "inputs": {
            "image": ["decode", 0], "grid": a.grid, "colors": a.colors, "palette": a.palette, "downscale": "box",
            "dither": a.dither, "alpha_threshold": 128, "preview_size": a.preview,
            "crop": not a.no_crop, "remove_bg": not a.keep_bg}}
        wf["save_sprite"] = {"class_type": "SaveImage", "inputs": {"images": ["pixel", 0], "filename_prefix": f"{name}/sprite_{a.grid}px"}}
        wf["save_preview"] = {"class_type": "SaveImage", "inputs": {"images": ["pixel", 1], "filename_prefix": f"{name}/preview"}}
    return wf


def api(path: str, data: dict | None = None) -> dict:
    req = urllib.request.Request(SERVER + path, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def upload(path: str) -> str:
    """Upload a reference image into ComfyUI's input folder; returns the name LoadImage expects."""
    p = Path(path)
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{p.name}\"\r\n"
            f"Content-Type: {mimetypes.guess_type(p.name)[0] or 'image/png'}\r\n\r\n").encode()
    body += p.read_bytes() + f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(SERVER + "/upload/image", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.load(r)
    return f"{res['subfolder']}/{res['name']}" if res.get("subfolder") else res["name"]


def run(workflow: dict) -> dict:
    try:
        pid = api("/prompt", {"prompt": workflow})["prompt_id"]
    except urllib.error.HTTPError as e:
        sys.exit(f"ComfyUI rejected the workflow: {e.read().decode(errors='replace')}")
    while True:
        hist = api(f"/history/{pid}")
        if pid in hist:
            status = hist[pid].get("status", {})
            if status.get("status_str") == "error":
                msgs = [m for m in status.get("messages", []) if m[0] == "execution_error"]
                sys.exit(f"Generation failed: {json.dumps(msgs, indent=1)[:2000]}")
            return hist[pid]
        time.sleep(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("subject", nargs="?", help="what to draw, or the edit instruction when --ref is given")
    ap.add_argument("--ref", action="append", default=[], metavar="IMG",
                    help="reference image (repeat up to 3x); turns on image edit / multi-reference mode")
    ap.add_argument("--mode", default="turbo", choices=list(MODES),
                    help="turbo = Viggle baked GGUF, 5 steps (default); base = original model, 10 steps")
    ap.add_argument("--style", default="16bit", choices=list(STYLES))
    ap.add_argument("--transparent", action="store_true", help="ask Qwen-Image 2.1 for a native RGBA transparent render")
    ap.add_argument("--raw-prompt", action="store_true", help="use the subject text as the full prompt (no wrapping)")
    ap.add_argument("--size", type=int, default=768, help="render resolution (about size x size pixels); 1024 = model sweet spot but no visible gain after pixelizing")
    ap.add_argument("--aspect", default="1:1", help="W:H of the render, e.g. 2:3 for a standing character (text -> image only)")
    ap.add_argument("--enhance", action="store_true",
                    help="rewrite the prompt with the official Qwen rewriter first (~2-3 min per new prompt on this GPU, "
                         "cached in output/pe_cache.json); also picks the aspect ratio unless --aspect is given")
    ap.add_argument("--steps", type=int, default=0, help="turbo: 4-8 (default 5); base: any (default 10)")
    ap.add_argument("--cfg", type=float, default=1.0, help="base mode only; keep 1.0 unless using --negative")
    ap.add_argument("--negative", default="")
    ap.add_argument("--seed", type=int, default=-1)
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--grid", type=int, default=64, help="sprite size in art pixels (longest side); 0 = skip pixelize")
    ap.add_argument("--colors", type=int, default=24)
    ap.add_argument("--palette", default="adaptive", choices=["adaptive", "pico8", "sweetie16", "db32"])
    ap.add_argument("--dither", action="store_true")
    ap.add_argument("--no-crop", action="store_true", help="do not crop the sprite to the subject")
    ap.add_argument("--keep-bg", action="store_true", help="keep the background (default: knock it out to transparent)")
    ap.add_argument("--preview", type=int, default=256, help="upscaled preview size, 0 = none")
    ap.add_argument("--name", default=None, help="output subfolder (default: timestamp)")
    ap.add_argument("--clip-device", default="default", choices=["default", "cpu"],
                    help="default = ComfyUI streams the text encoder through the GPU (~2x faster per new prompt); cpu = RAM only")
    ap.add_argument("--dump-workflow", metavar="FILE", help="write the API workflow JSON and exit")
    aspect_given = any(x == "--aspect" or x.startswith("--aspect=") for x in sys.argv)
    a = ap.parse_args()

    edit = bool(a.ref)
    if a.dump_workflow:
        subject = a.subject or "a knight with a blue cape and a sword"
        refs = [Path(r).name for r in a.ref]
        wf = build_workflow(a, build_prompt(subject, a.style, a.transparent, edit), 42, "asset", refs)
        Path(a.dump_workflow).write_text(json.dumps(wf, indent=2), encoding="utf-8")
        print("wrote", a.dump_workflow)
        return
    if not a.subject:
        ap.error("subject is required")
    if len(a.ref) > 3:
        print("warning: Viggle turbo was trained with up to 3 references")

    refs = [upload(r) for r in a.ref]
    if a.enhance:
        # enhance the plain request; the RGBA wrapper is a fixed phrase the rewriter must not paraphrase
        request = a.subject if a.raw_prompt else build_prompt(a.subject, a.style, False, edit)
        prompt, ratio = enhance(request, refs, a.ref, a.clip_device)
        if a.transparent:
            prompt = RGBA_WRAP.format(prompt)
        if ratio and not aspect_given and not edit:
            a.aspect = ratio
    else:
        prompt = a.subject if a.raw_prompt else build_prompt(a.subject, a.style, a.transparent, edit)
    name = a.name or time.strftime("%Y%m%d_%H%M%S")
    print(f"mode={a.mode} size={a.size} aspect={'image 1' if edit else a.aspect} refs={len(refs)}\nprompt: {prompt}")
    for i in range(a.count):
        seed = a.seed + i if a.seed >= 0 else random.randrange(2**48)
        t = time.time()
        out = run(build_workflow(a, prompt, seed, name, refs))
        files = [img["filename"] for node in out["outputs"].values() for img in node.get("images", [])]
        print(f"[{i + 1}/{a.count}] seed={seed} {time.time() - t:.1f}s -> output/{name}/ {files}")


if __name__ == "__main__":
    main()
