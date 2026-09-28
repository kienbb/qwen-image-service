"""engine.workflows must reproduce the workflows of the legacy CLI byte for byte (tests/golden/legacy)."""
import json
from pathlib import Path

import pytest

from qis.engine import workflows as W

LEGACY = Path(__file__).parent / "golden" / "legacy"

GEN_CASES = {
    "t2i_turbo_default": (dict(), "a knight", "16bit", False, [], 42),
    "t2i_turbo_steps8_aspect23": (dict(steps=8, aspect="2:3"), "a standing orc", "32bit", False, [], 7),
    "t2i_turbo_transparent_grid32": (dict(grid=32, palette="pico8", dither=True), "golden key", "16bit", True, [], 1),
    "t2i_turbo_nogrid_1024": (dict(grid=0, size=1024), "a castle", "none", False, [], 5),
    "t2i_base_cfg_negative": (dict(mode="base", cfg=3.5, negative="blurry"), "a dragon", "16bit", False, [], 9),
    "t2i_base_q8": (dict(mode="base_q8", remove_bg=False, crop=False), "a potion", "16bit", False, [], 11),
    "t2i_clip_cpu_steps4": (dict(steps=4, clip_device="cpu", colors=16), "slime", "16bit", False, [], 3),
    "edit_1ref": (dict(), "make his armor gold", "16bit", False, ["knight.png"], 42),
    "edit_3ref_base": (dict(mode="base"), "combine", "16bit", False, ["a.png", "b.png", "c.png"], 8),
    "edit_1ref_transparent": (dict(grid=0), "add a shield", "16bit", True, ["k.png"], 2),
}


def legacy(name):
    return json.loads((LEGACY / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", GEN_CASES)
def test_generate_matches_gen_py(name):
    kw, subject, style, transparent, refs, seed = GEN_CASES[name]
    kw = {"grid": 64, **kw}
    ref = legacy(f"gen_{name}")
    prompt = W.build_prompt(subject, style, transparent, bool(refs))
    assert prompt == ref["prompt"]
    assert W.generate(W.GenParams(**kw), prompt, seed, refs, prefix="asset") == ref["workflow"]


@pytest.mark.parametrize("name,call", [
    ("refine_turbo", lambda: W.refine("img.png", "a cozy room", 896, 1152, prefix="refine/img")),
    ("refine_turbo_nodes", lambda: W.refine("img.png", "a cozy room", 1024, 1024, nodes="0.5, 0.25", seed=4, prefix="refine/img")),
    ("refine_base", lambda: W.refine("img.png", "a cozy room", 768, 768, engine="base", denoise=0.3, steps=20, prefix="refine/img")),
    ("refine_base_q8", lambda: W.refine("img.png", "a cozy room", 768, 1024, engine="base_q8", prefix="refine/img")),
    ("upscale_x2", lambda: W.upscale("img.png", "RealESRGAN_x2plus.pth", 1080, 1920, prefix="upscale/img_RealESRGAN_x2plus")),
    ("upscale_x4", lambda: W.upscale("img.png", "RealESRGAN_x4plus.pth", 2048, 2048, prefix="upscale/img_RealESRGAN_x4plus")),
])
def test_tools_match_legacy(name, call):
    assert call() == legacy(f"tool_{name}")["workflow"]


def test_turbo_rejects_unsupported_steps():
    with pytest.raises(W.WorkflowError):
        W.generate(W.GenParams(steps=3), "x", 1, [])


def test_explicit_size_rounds_to_32():
    wf = W.generate(W.GenParams(width=1000, height=1500), "x", 1, [])
    assert (wf["empty"]["inputs"]["width"], wf["empty"]["inputs"]["height"]) == (992, 1504)
    assert wf["enc"]["inputs"]["resolution"] == round((992 * 1504) ** 0.5)


def test_enhance_parse():
    assert W.parse_enhanced('noise {"rewritten_prompt": " a b ", "wh_ratio": "2:3"} tail') == ("a b", "2:3")
    assert W.parse_enhanced("no json") is None


def test_enhance_workflow_with_refs():
    wf = W.enhance("make it red", ["a.png", "b.png"])
    assert wf["gen"]["inputs"]["image"] == ["batch", 0]
    assert wf["gen"]["inputs"]["prompt"].count("<|image_pad|>") == 2
    assert "Write the description in English" in wf["gen"]["inputs"]["prompt"]


def test_profile_config_disables_missing_roles(tmp_path):
    from qis.config import load_config
    from qis.models import Manifest, profile_config, write_config
    man = Manifest.load()
    cfg_path = tmp_path / "config.toml"
    write_config(cfg_path, profile_config(man, man.profile_models("T1", "fast"), "T1"))
    cfg = load_config(cfg_path)
    ms = cfg.models.model_set()
    assert set(ms.unets) == {"turbo"} and ms.unets["turbo"] == "qwen_image_2.1_turbo_Q4_K_M.gguf"
    assert set(ms.upscalers) == {"realesrgan-x2", "realesrgan-x4"}
    assert man.profile_models("T1", "quality")[0:1] != []
