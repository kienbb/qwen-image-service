"""Model defaults, sampling schedules and prompt fragments for Qwen-Image 2.1 (moved from gen.py)."""
from __future__ import annotations

from dataclasses import dataclass, field

# Viggle turbo v0.2.1 raw sigma nodes; ViggleTurboSigmas applies the resolution shift.
# Extra steps only split the high-noise end.
VIGGLE_SIGMAS = {
    4: "1.0, 0.75, 0.5, 0.25",
    5: "1.0, 0.875, 0.75, 0.5, 0.25",
    6: "1.0, 0.9375, 0.875, 0.75, 0.5, 0.25",
    7: "1.0, 0.9583, 0.9167, 0.875, 0.75, 0.5, 0.25",
    8: "1.0, 0.9375, 0.875, 0.75, 0.625, 0.5, 0.25, 0.125"}

# sampling modes: Viggle-baked student (custom sigmas, no CFG) or the original model (KSampler)
MODES = {
    "turbo": {"unet": "turbo", "steps": 5, "sigmas": VIGGLE_SIGMAS},
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


@dataclass
class ModelSet:
    """File names ComfyUI loads; the active profile decides them (installer / dashboard)."""
    unets: dict[str, str] = field(default_factory=lambda: {
        "turbo": "qwen-image-2.1-viggle-Q4_K_M.gguf",  # base + Viggle turbo v0.2.1 baked in (tools/merge_lora.py)
        "base": "qwen-image-2.1-Q4_K_M.gguf",
        "base_q8": "qwen-image-2.1-Q8_0.gguf",
    })
    clip: str = "qwen3vl_8b_int8_convrot.safetensors"
    vae: str = "qwen_image_2.1_vae_bf16.safetensors"
    upscalers: dict[str, str] = field(default_factory=lambda: {
        "realesrgan-x2": "RealESRGAN_x2plus.pth",
        "realesrgan-x4": "RealESRGAN_x4plus.pth",
    })

    def modes(self) -> list[str]:
        return [m for m, spec in MODES.items() if spec["unet"] in self.unets]
