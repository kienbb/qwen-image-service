"""ComfyUI API workflow builders (pure functions, no I/O).

Extracted from gen.py and tools/*.py; tests/golden checks they reproduce the legacy JSON exactly.
Image inputs are names ComfyUI's LoadImage understands (files already uploaded to its input folder).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from importlib import resources

from .constants import EDIT_HINT, MODES, RGBA_WRAP, STYLES, SUBJECT_HINT, ModelSet

Workflow = dict


class WorkflowError(ValueError):
    """Parameters that cannot be turned into a workflow (reported to the client as 400)."""


def build_prompt(subject: str, style: str, transparent: bool, edit: bool) -> str:
    """Wrap a short subject into the asset prompt: style prefix + framing hint (+ RGBA phrase)."""
    subject = subject.strip().rstrip(".")
    subject = subject[:1].upper() + subject[1:]
    if edit:  # instruction first, the references already carry the look
        text = f"{subject}. {EDIT_HINT}"
    else:
        hint = SUBJECT_HINT if transparent else SUBJECT_HINT + ", plain flat white background"
        text = ". ".join(p for p in (STYLES[style], subject, hint) if p) + "."
    return RGBA_WRAP.format(text) if transparent else text


def wrap_transparent(prompt: str) -> str:
    return RGBA_WRAP.format(prompt)


def dims(size: int, ratio: str) -> tuple[int, int]:
    """Width/height with about size*size pixels at the W:H ratio, multiples of 32."""
    rw, rh = (float(x) for x in ratio.split(":"))
    area = size * size
    return max(32, round(math.sqrt(area * rw / rh) / 32) * 32), max(32, round(math.sqrt(area * rh / rw) / 32) * 32)


def round32(v: float) -> int:
    return max(32, round(v / 32) * 32)


@dataclass
class GenParams:
    mode: str = "turbo"
    steps: int = 0                 # 0 = mode default
    size: int = 768                # render resolution: about size*size pixels
    aspect: str = "1:1"            # W:H (text -> image only)
    width: int | None = None       # explicit render size (overrides size/aspect)
    height: int | None = None
    negative: str = ""
    cfg: float = 1.0               # base modes only
    clip_device: str = "default"   # default = streamed through the GPU; cpu = RAM only
    # optional pixelize tail inside ComfyUI (legacy CLI); the service pixelizes on its CPU pool instead
    grid: int = 0
    colors: int = 24
    palette: str = "adaptive"
    dither: bool = False
    preview: int = 256
    crop: bool = True
    remove_bg: bool = True

    def resolved_steps(self) -> int:
        spec = MODES[self.mode]
        steps = self.steps or spec["steps"]
        if "sigmas" in spec and steps not in spec["sigmas"]:
            raise WorkflowError(f"mode {self.mode} supports steps {sorted(spec['sigmas'])}")
        return steps

    def render_size(self) -> tuple[int, int]:
        if self.width and self.height:
            return round32(self.width), round32(self.height)
        return dims(self.size, self.aspect)

    def resolution(self) -> int:
        if self.width and self.height:
            return round(math.sqrt(round32(self.width) * round32(self.height)))
        return self.size


def generate(p: GenParams, prompt: str, seed: int, refs: list[str], models: ModelSet | None = None,
             prefix: str = "asset") -> Workflow:
    """Text -> image, or edit / multi-reference when refs are given. Raw image is saved by node 'save_raw'."""
    models = models or ModelSet()
    if p.mode not in MODES:
        raise WorkflowError(f"unknown mode {p.mode!r}")
    mode = MODES[p.mode]
    if mode["unet"] not in models.unets:
        raise WorkflowError(f"mode {p.mode!r} is not installed on this node")
    steps = p.resolved_steps()
    wf: Workflow = {
        "unet": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": models.unets[mode["unet"]]}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": models.clip, "type": "qwen_image", "device": p.clip_device}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": models.vae}},
        "enc": {"class_type": "TextEncodeQwenImage21", "inputs": {
            "clip": ["clip", 0], "prompt": prompt, "negative_prompt": p.negative, "resolution": p.resolution()}},
    }
    wf["cache"] = {"class_type": "QwenImage21Cache", "inputs": {"model": ["unet", 0], "device": "auto", "dtype": "default"}}
    model = ["cache", 0]

    for i, ref in enumerate(refs, 1):
        wf[f"ref{i}"] = {"class_type": "LoadImage", "inputs": {"image": ref}}
        wf["enc"]["inputs"][f"images.image_{i}"] = [f"ref{i}", 0]
    if refs:  # edit: output size follows image 1 at ~size x size pixels
        wf["enc"]["inputs"]["vae"] = ["vae", 0]
        latent = ["enc", 2]
    else:
        w, h = p.render_size()
        wf["empty"] = {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}}
        latent = ["empty", 0]

    if "sigmas" not in mode:
        wf["sample"] = {"class_type": "KSampler", "inputs": {
            "model": model, "positive": ["enc", 0], "negative": ["enc", 1], "latent_image": latent,
            "seed": seed, "steps": steps, "cfg": p.cfg, "sampler_name": "euler",
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
    wf["save_raw"] = {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": f"{prefix}/raw"}}
    if p.grid:
        wf["pixel"] = {"class_type": "PixelArtRetro", "inputs": {
            "image": ["decode", 0], "grid": p.grid, "colors": p.colors, "palette": p.palette, "downscale": "box",
            "dither": p.dither, "alpha_threshold": 128, "preview_size": p.preview,
            "crop": p.crop, "remove_bg": p.remove_bg}}
        wf["save_sprite"] = {"class_type": "SaveImage", "inputs": {"images": ["pixel", 0], "filename_prefix": f"{prefix}/sprite_{p.grid}px"}}
        wf["save_preview"] = {"class_type": "SaveImage", "inputs": {"images": ["pixel", 1], "filename_prefix": f"{prefix}/preview"}}
    return wf


def refine(image: str, prompt: str, width: int, height: int, *, engine: str = "turbo", nodes: str = "0.25, 0.12",
           denoise: float = 0.45, steps: int = 24, seed: int = 1, models: ModelSet | None = None,
           prefix: str = "refine/image") -> Workflow:
    """Hires-fix: resize, then re-denoise lightly (turbo: raw Viggle sigma nodes; base engines: KSampler denoise)."""
    models = models or ModelSet()
    if engine not in MODES or MODES[engine]["unet"] not in models.unets:
        raise WorkflowError(f"refine engine {engine!r} is not installed on this node")
    try:
        [float(x) for x in nodes.split(",")]
    except ValueError as e:
        raise WorkflowError(f"nodes must be comma separated numbers: {nodes!r}") from e
    wf: Workflow = {
        "load": {"class_type": "LoadImage", "inputs": {"image": image}},
        "rgba": {"class_type": "JoinImageWithAlpha", "inputs": {"image": ["load", 0], "alpha": ["load", 1]}},
        "scale": {"class_type": "ImageScale", "inputs": {"image": ["rgba", 0], "upscale_method": "lanczos",
                                                           "width": width, "height": height, "crop": "disabled"}},
        "unet": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": models.unets[MODES[engine]["unet"]]}},
        "cache": {"class_type": "QwenImage21Cache", "inputs": {"model": ["unet", 0], "device": "auto", "dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": models.clip, "type": "qwen_image", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": models.vae}},
        "enc": {"class_type": "TextEncodeQwenImage21", "inputs": {"clip": ["clip", 0], "prompt": prompt,
                                                                   "negative_prompt": "", "resolution": max(width, height)}},
        "latent": {"class_type": "VAEEncode", "inputs": {"pixels": ["scale", 0], "vae": ["vae", 0]}},
        "sigmas": {"class_type": "ViggleTurboSigmas", "inputs": {"latent": ["latent", 0], "nodes": nodes}},
        "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "guider": {"class_type": "BasicGuider", "inputs": {"model": ["cache", 0], "conditioning": ["enc", 0]}},
        "sampler": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "sample": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["noise", 0], "guider": ["guider", 0], "sampler": ["sampler", 0],
            "sigmas": ["sigmas", 0], "latent_image": ["latent", 0]}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": prefix}},
    }
    if engine != "turbo":
        for k in ("sigmas", "noise", "guider", "sampler"):
            del wf[k]
        wf["sample"] = {"class_type": "KSampler", "inputs": {
            "model": ["cache", 0], "positive": ["enc", 0], "negative": ["enc", 1], "latent_image": ["latent", 0],
            "seed": seed, "steps": steps, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
            "denoise": denoise}}
    return wf


def upscale(image: str, model_file: str, width: int, height: int, prefix: str = "upscale/image") -> Workflow:
    """ESRGAN-family model at its native factor, then a Lanczos resize to the exact target size."""
    return {
        "load": {"class_type": "LoadImage", "inputs": {"image": image}},
        "model": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": model_file}},
        "up": {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["model", 0], "image": ["load", 0]}},
        "fit": {"class_type": "ImageScale", "inputs": {"image": ["up", 0], "upscale_method": "lanczos",
                                                         "width": width, "height": height, "crop": "disabled"}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["fit", 0], "filename_prefix": prefix}},
    }


def enhancer_system_prompt(edit: bool) -> str:
    name = "pe_edit_system.txt" if edit else "pe_t2i_system.txt"
    return resources.files("qis.data").joinpath("prompts", name).read_text(encoding="utf-8")


def enhance(request: str, refs: list[str], clip_device: str = "default", models: ModelSet | None = None) -> Workflow:
    """Official Qwen-Image 2.1 rewriter run on the Qwen3-VL text encoder (as in Viggle's workflows).

    The text comes back in outputs['show']['text'][0]; parse it with parse_enhanced()."""
    models = models or ModelSet()
    system = enhancer_system_prompt(bool(refs))
    vision = "<|vision_start|><|image_pad|><|vision_end|>" * len(refs)
    user = f"{request}\n(Write the description in English.)" if refs else request
    wf: Workflow = {
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": models.clip, "type": "qwen_image", "device": clip_device}},
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
    return wf


def parse_enhanced(text: str) -> tuple[str, str] | None:
    """(rewritten prompt, W:H ratio or '') from the rewriter output; None if it did not return the JSON."""
    try:
        res = json.loads(text[text.index("{"):text.rindex("}") + 1])
        return res["rewritten_prompt"].strip(), res.get("wh_ratio") or ""
    except (ValueError, KeyError, AttributeError):
        return None


def workflow_models(wf: Workflow) -> set[str]:
    """Model files a workflow loads (used for model affinity in the scheduler)."""
    keys = {"UnetLoaderGGUF": "unet_name", "UpscaleModelLoader": "model_name"}
    return {n["inputs"][keys[n["class_type"]]] for n in wf.values() if n["class_type"] in keys}
