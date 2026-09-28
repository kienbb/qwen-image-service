"""Generate / edit retro pixel-art game assets through the Qwen Image Service API.

Start the service first (`qis serve`, or the installed service), set QIS_API_KEY (`qis key create --name cli`), then:
  text -> image      python gen.py "a knight with a blue cape and a sword"
  small icon         python gen.py "health potion, red liquid in a glass flask" --grid 32
  image edit         python gen.py "make his armor gold and his cape red" --ref output/knight/raw_0.png
  multi-reference    python gen.py "the character from image 1 holding the weapon from image 2" --ref hero.png --ref axe.png
  batch              python gen.py "forest slime monster" --count 4 --size 768
  prompt enhancer    python gen.py "a goblin archer" --enhance   (official Qwen rewriter on the local text encoder, cached)
  UI workflow        python gen.py --dump-workflow workflow_api.json   (drag the file into the ComfyUI web UI)

Outputs land in ./output/<name>/: raw render, sprite at native grid size, nearest-upscaled preview.
Stdlib only - runs on any Python 3.9+ (the service does the work).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from qis.client import ApiError, Client  # noqa: E402

MODES = ["turbo", "base", "base_q8"]
STYLES = ["16bit", "32bit", "none"]
PALETTES = ["adaptive", "pico8", "sweetie16", "db32"]


def build_spec(a: argparse.Namespace, refs: list[str], aspect_given: bool) -> dict:
    gen = {"op": "generate", "prompt": a.subject, "mode": a.mode, "steps": a.steps, "size": a.size, "n": a.count,
           "negative": a.negative, "cfg": a.cfg, "template": "raw" if a.raw_prompt else "asset", "style": a.style,
           "transparent": a.transparent, "enhance": a.enhance, "clip_device": a.clip_device}
    if a.seed >= 0:
        gen["seed"] = a.seed
    if aspect_given or not a.enhance:
        gen["aspect"] = a.aspect
    if refs:
        gen["refs"] = [f"$inputs.ref{i}" for i in range(len(refs))]
    ops = [gen]
    if a.grid:
        ops.append({"op": "pixelize", "image": "$prev", "grid": a.grid, "colors": a.colors, "palette": a.palette,
                    "dither": a.dither, "crop": not a.no_crop, "remove_bg": not a.keep_bg, "preview_size": a.preview})
    return {"ops": ops, "inputs": {f"ref{i}": r for i, r in enumerate(refs)},
            "outputs": ["$step[0]", "$step[1]"] if a.grid else ["$step[0]"]}


def dump_workflow(a: argparse.Namespace) -> None:
    from qis.engine import workflows as W
    subject = a.subject or "a knight with a blue cape and a sword"
    refs = [Path(r).name for r in a.ref]
    p = W.GenParams(mode=a.mode, steps=a.steps, size=a.size, aspect=a.aspect, negative=a.negative, cfg=a.cfg,
                    clip_device=a.clip_device, grid=a.grid, colors=a.colors, palette=a.palette, dither=a.dither,
                    preview=a.preview, crop=not a.no_crop, remove_bg=not a.keep_bg)
    wf = W.generate(p, W.build_prompt(subject, a.style, a.transparent, bool(refs)), 42, refs, prefix="asset")
    Path(a.dump_workflow).write_text(json.dumps(wf, indent=2), encoding="utf-8")
    print("wrote", a.dump_workflow)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("subject", nargs="?", help="what to draw, or the edit instruction when --ref is given")
    ap.add_argument("--ref", action="append", default=[], metavar="IMG",
                    help="reference image (repeat up to 3x); turns on image edit / multi-reference mode")
    ap.add_argument("--mode", default="turbo", choices=MODES,
                    help="turbo = Viggle baked GGUF, 5 steps (default); base = original model, 10 steps")
    ap.add_argument("--style", default="16bit", choices=STYLES)
    ap.add_argument("--transparent", action="store_true", help="ask Qwen-Image 2.1 for a native RGBA transparent render")
    ap.add_argument("--raw-prompt", action="store_true", help="use the subject text as the full prompt (no wrapping)")
    ap.add_argument("--size", type=int, default=768, help="render resolution (about size x size pixels)")
    ap.add_argument("--aspect", default="1:1", help="W:H of the render, e.g. 2:3 for a standing character (text -> image only)")
    ap.add_argument("--enhance", action="store_true",
                    help="rewrite the prompt with the official Qwen rewriter first (cached by the service); "
                         "also picks the aspect ratio unless --aspect is given")
    ap.add_argument("--steps", type=int, default=0, help="turbo: 4-8 (default 5); base: any (default 10)")
    ap.add_argument("--cfg", type=float, default=1.0, help="base mode only; keep 1.0 unless using --negative")
    ap.add_argument("--negative", default="")
    ap.add_argument("--seed", type=int, default=-1)
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--grid", type=int, default=64, help="sprite size in art pixels (longest side); 0 = skip pixelize")
    ap.add_argument("--colors", type=int, default=24)
    ap.add_argument("--palette", default="adaptive", choices=PALETTES)
    ap.add_argument("--dither", action="store_true")
    ap.add_argument("--no-crop", action="store_true", help="do not crop the sprite to the subject")
    ap.add_argument("--keep-bg", action="store_true", help="keep the background (default: knock it out to transparent)")
    ap.add_argument("--preview", type=int, default=256, help="upscaled preview size, 0 = none")
    ap.add_argument("--name", default=None, help="output subfolder (default: timestamp)")
    ap.add_argument("--clip-device", default="default", choices=["default", "cpu"])
    ap.add_argument("--url", help="service URL (default $QIS_URL or http://127.0.0.1:8080)")
    ap.add_argument("--api-key", help="API key (default $QIS_API_KEY)")
    ap.add_argument("--dump-workflow", metavar="FILE", help="write the ComfyUI API workflow JSON and exit")
    aspect_given = any(x == "--aspect" or x.startswith("--aspect=") for x in sys.argv)
    a = ap.parse_args()

    if a.dump_workflow:
        return dump_workflow(a)
    if not a.subject:
        ap.error("subject is required")
    if len(a.ref) > 3:
        ap.error("at most 3 reference images")

    c = Client(a.url, a.api_key)
    refs = [c.upload(r) for r in a.ref]
    name = a.name or time.strftime("%Y%m%d_%H%M%S")
    out = ROOT / "output" / name
    out.mkdir(parents=True, exist_ok=True)
    t = time.time()
    try:
        job = c.run_job(build_spec(a, refs, aspect_given))
    except ApiError as e:
        sys.exit(str(e))
    outputs = job["result"]["outputs"]
    gen_data = outputs[0]["data"]
    print(f"mode={a.mode} size={a.size} refs={len(refs)} job={job['id']} {time.time() - t:.1f}s\nprompt: {gen_data['prompt']}")
    saved = []
    for i, img in enumerate(outputs[0]["images"]):
        (out / f"raw_{i}.png").write_bytes(c.download(img["url"]))
        saved.append(f"raw_{i}.png")
    if a.grid:
        for i, img in enumerate(outputs[1]["images"]):
            (out / f"sprite_{a.grid}px_{i}.png").write_bytes(c.download(img["url"]))
            saved.append(f"sprite_{a.grid}px_{i}.png")
        for i, img in enumerate(outputs[1]["extra"].get("preview", [])):
            (out / f"preview_{i}.png").write_bytes(c.download(img["url"]))
            saved.append(f"preview_{i}.png")
    print(f"seeds={gen_data['seeds']} -> output/{name}/ {saved}")


if __name__ == "__main__":
    main()
