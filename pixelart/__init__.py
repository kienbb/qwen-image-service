"""ComfyUI custom node: Pixel Art (Retro) — wraps pixelize.pixelize()."""
import numpy as np
import torch
from PIL import Image

from .pixelize import PALETTE_CHOICES, pixelize


def _to_pil(t: torch.Tensor) -> Image.Image:
    arr = (t.cpu().numpy().clip(0, 1) * 255).astype(np.uint8)
    return Image.fromarray(arr, "RGBA" if arr.shape[-1] == 4 else "RGB")


def _to_tensor(img: Image.Image) -> torch.Tensor:
    return torch.from_numpy(np.array(img.convert("RGBA")).astype(np.float32) / 255.0)


class PixelArtRetro:
    CATEGORY = "image/pixelart"
    RETURN_TYPES = ("IMAGE", "IMAGE")
    RETURN_NAMES = ("sprite", "preview")
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "grid": ("INT", {"default": 64, "min": 8, "max": 256, "step": 8,
                                 "tooltip": "Longest side of the sprite in art pixels"}),
                "colors": ("INT", {"default": 24, "min": 2, "max": 256,
                                   "tooltip": "Palette size (adaptive palette only)"}),
                "palette": (PALETTE_CHOICES, {"default": "adaptive"}),
                "downscale": (["box", "nearest"], {"default": "box"}),
                "dither": ("BOOLEAN", {"default": False}),
                "alpha_threshold": ("INT", {"default": 128, "min": 1, "max": 255}),
                "preview_size": ("INT", {"default": 256, "min": 0, "max": 1024, "step": 8,
                                         "tooltip": "Nearest-neighbour upscale of the sprite; 0 = none"}),
                "crop": ("BOOLEAN", {"default": True, "tooltip": "Crop to the subject and centre it on a square canvas"}),
                "remove_bg": ("BOOLEAN", {"default": False, "tooltip": "Make a flat background transparent (renders without alpha)"}),
            },
            "optional": {
                "mask": ("MASK", {"tooltip": "Optional alpha mask (1 = keep) if the image has no alpha"}),
            },
        }

    def run(self, image, grid, colors, palette, downscale, dither, alpha_threshold, preview_size, crop=True, remove_bg=False, mask=None):
        sprites, previews = [], []
        for i, t in enumerate(image):
            pil = _to_pil(t)
            if mask is not None:
                m = mask[min(i, mask.shape[0] - 1)].cpu().numpy()
                a = Image.fromarray((m.clip(0, 1) * 255).astype(np.uint8)).resize(pil.size)
                pil = pil.convert("RGBA")
                pil.putalpha(a)
            s, p = pixelize(pil, grid, colors, palette, downscale, dither, alpha_threshold, preview_size, crop, remove_bg)
            sprites.append(_to_tensor(s))
            previews.append(_to_tensor(p))
        return (torch.stack(sprites), torch.stack(previews))


NODE_CLASS_MAPPINGS = {"PixelArtRetro": PixelArtRetro}
NODE_DISPLAY_NAME_MAPPINGS = {"PixelArtRetro": "Pixel Art (Retro 16/32-bit)"}
