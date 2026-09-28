"""Capture ComfyUI workflows produced by the legacy CLI (gen.py, tools/refine.py, tools/upscale.py).

Ran once against the pre-refactor code (git 53a61d1, before gen.py became an API client); the JSON files in
legacy/ are the contract qis.engine.workflows must reproduce. Kept for reference: run it inside that old checkout.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import gen  # noqa: E402

OUT = Path(__file__).parent / "legacy"
OUT.mkdir(exist_ok=True)


def ns(**kw):
    base = dict(mode="turbo", steps=0, size=768, aspect="1:1", negative="", cfg=1.0, grid=64, colors=24,
                palette="adaptive", dither=False, preview=256, no_crop=False, keep_bg=False, clip_device="default")
    base.update(kw)
    return argparse.Namespace(**base)


CASES = {
    "t2i_turbo_default": (ns(), "a knight", "16bit", False, [], 42),
    "t2i_turbo_steps8_aspect23": (ns(steps=8, aspect="2:3"), "a standing orc", "32bit", False, [], 7),
    "t2i_turbo_transparent_grid32": (ns(grid=32, palette="pico8", dither=True), "golden key", "16bit", True, [], 1),
    "t2i_turbo_nogrid_1024": (ns(grid=0, size=1024), "a castle", "none", False, [], 5),
    "t2i_base_cfg_negative": (ns(mode="base", cfg=3.5, negative="blurry"), "a dragon", "16bit", False, [], 9),
    "t2i_base_q8": (ns(mode="base_q8", keep_bg=True, no_crop=True), "a potion", "16bit", False, [], 11),
    "t2i_clip_cpu_steps4": (ns(steps=4, clip_device="cpu", colors=16), "slime", "16bit", False, [], 3),
    "edit_1ref": (ns(), "make his armor gold", "16bit", False, ["knight.png"], 42),
    "edit_3ref_base": (ns(mode="base"), "combine", "16bit", False, ["a.png", "b.png", "c.png"], 8),
    "edit_1ref_transparent": (ns(grid=0), "add a shield", "16bit", True, ["k.png"], 2),
}

for name, (a, subject, style, transparent, refs, seed) in CASES.items():
    prompt = gen.build_prompt(subject, style, transparent, bool(refs))
    wf = gen.build_workflow(a, prompt, seed, "asset", refs)
    (OUT / f"gen_{name}.json").write_text(json.dumps({"prompt": prompt, "workflow": wf}, indent=1), encoding="utf-8")

# tools/*.py build their workflow inside main(): capture it by stubbing the ComfyUI calls
captured = {}
gen.upload = lambda p: Path(p).name
gen.run = lambda wf: (captured.setdefault("wf", wf), {"outputs": {"save": {"images": [{"subfolder": "", "filename": "x"}]}}})[1]
gen.ROOT = Path(__file__).parent / "_stub_root"
(gen.ROOT / "output").mkdir(parents=True, exist_ok=True)
(gen.ROOT / "output" / "x").write_bytes(b"")
sys.path.insert(0, str(ROOT / "tools"))
import refine  # noqa: E402
import upscale  # noqa: E402

pf = Path(__file__).parent / "_stub_root" / "prompt.txt"
pf.write_text("a cozy room", encoding="utf-8")
for name, argv, mod in [
    ("refine_turbo", ["refine.py", "img.png", "--size", "896x1152", "--prompt-file", str(pf)], refine),
    ("refine_turbo_nodes", ["refine.py", "img.png", "--size", "1024x1024", "--prompt-file", str(pf), "--nodes", "0.5, 0.25", "--seed", "4"], refine),
    ("refine_base", ["refine.py", "img.png", "--size", "768x768", "--prompt-file", str(pf), "--engine", "base", "--denoise", "0.3", "--steps", "20"], refine),
    ("refine_base_q8", ["refine.py", "img.png", "--size", "768x1024", "--prompt-file", str(pf), "--engine", "base_q8"], refine),
    ("upscale_x2", ["upscale.py", "img.png", "--size", "1080x1920", "--out", str(gen.ROOT / "o.png")], upscale),
    ("upscale_x4", ["upscale.py", "img.png", "--size", "2048x2048", "--model", "RealESRGAN_x4plus.pth", "--out", str(gen.ROOT / "o.png")], upscale),
]:
    captured.clear()
    sys.argv = argv
    mod.main()
    (OUT / f"tool_{name}.json").write_text(json.dumps({"argv": argv[1:], "workflow": captured["wf"]}, indent=1), encoding="utf-8")
print("captured", len(list(OUT.glob("*.json"))), "workflows ->", OUT)
