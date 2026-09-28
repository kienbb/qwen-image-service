"""Turn a high-res AI render into clean retro pixel art.

Pipeline: crop to the subject -> downscale to a pixel grid -> reduce to a limited palette
-> hard alpha / background knock-out -> (optional) nearest-neighbour upscale for preview.
Pure Pillow + numpy so it runs both inside ComfyUI (custom node) and standalone (CLI).
"""
from __future__ import annotations

from collections import deque

import numpy as np
from PIL import Image

# Fixed retro palettes (hex, no '#').
PALETTES: dict[str, list[str]] = {
    "pico8": [
        "000000", "1d2b53", "7e2553", "008751", "ab5236", "5f574f", "c2c3c7", "fff1e8",
        "ff004d", "ffa300", "ffec27", "00e436", "29adff", "83769c", "ff77a8", "ffccaa",
    ],
    "sweetie16": [
        "1a1c2c", "5d275d", "b13e53", "ef7d57", "ffcd75", "a7f070", "38b764", "257179",
        "29366f", "3b5dc9", "41a6f6", "73eff7", "f4f4f4", "94b0c2", "566c86", "333c57",
    ],
    "db32": [
        "000000", "222034", "45283c", "663931", "8f563b", "df7126", "d9a066", "eec39a",
        "fbf236", "99e550", "6abe30", "37946e", "4b692f", "524b24", "323c39", "3f3f74",
        "306082", "5b6ee1", "639bff", "5fcde4", "cbdbfc", "ffffff", "9badb7", "847e87",
        "696a6a", "595652", "76428a", "ac3232", "d95763", "d77bba", "8f974a", "8a6f30",
    ],
}
PALETTE_CHOICES = ["adaptive"] + list(PALETTES)


def _palette_image(hexes: list[str]) -> Image.Image:
    flat: list[int] = []
    for h in hexes:
        flat += [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    flat += flat[:3] * (256 - len(hexes))  # pad with first colour
    pal = Image.new("P", (1, 1))
    pal.putpalette(flat)
    return pal


def _border(a: np.ndarray) -> np.ndarray:
    return np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])


def _has_alpha(a: np.ndarray) -> bool:
    # Qwen-Image 2.1's VAE always decodes RGBA; opaque renders still carry alpha ~210-255, so only count real holes
    return (a < 128).mean() > 0.01


def _subject_mask(rgba: np.ndarray, tol: float = 32.0) -> np.ndarray:
    """True where the pixel belongs to the subject: opaque, or far from the border (background) colour."""
    if _has_alpha(rgba[..., 3]):
        return rgba[..., 3] >= 128
    rgb = rgba[..., :3].astype(np.float32)
    bg = np.median(_border(rgb), axis=0)
    return np.linalg.norm(rgb - bg, axis=-1) > tol


def crop_to_subject(img: Image.Image, margin: float = 0.06) -> Image.Image:
    """Crop to the subject's bounding box plus margin, then pad to a square canvas (background colour / transparent)."""
    rgba = np.array(img.convert("RGBA"))
    ys, xs = np.nonzero(_subject_mask(rgba))
    if len(xs) == 0:
        return img.convert("RGBA")
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    side = int(max(x1 - x0, y1 - y0) * (1 + 2 * margin))
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    has_alpha = _has_alpha(rgba[..., 3])
    fill = (0, 0, 0, 0) if has_alpha else tuple(int(v) for v in np.median(_border(rgba), axis=0))
    canvas = Image.new("RGBA", (side, side), fill)
    canvas.paste(img.convert("RGBA"), (side // 2 - cx, side // 2 - cy))
    return canvas


def _knock_out_background(rgb: np.ndarray, tol: float = 40.0) -> np.ndarray:
    """Flood-fill from the border over pixels close to the border colour; returns alpha (0 = background)."""
    h, w, _ = rgb.shape
    rgb = rgb.astype(np.float32)
    near = np.linalg.norm(rgb - np.median(_border(rgb), axis=0), axis=-1) <= tol
    alpha = np.full((h, w), 255, np.uint8)
    todo = deque([(y, x) for y in range(h) for x in (0, w - 1)] + [(y, x) for x in range(w) for y in (0, h - 1)])
    while todo:
        y, x = todo.pop()
        if not alpha[y, x] or not near[y, x]:
            continue
        alpha[y, x] = 0
        for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
            if 0 <= ny < h and 0 <= nx < w and alpha[ny, nx]:
                todo.append((ny, nx))
    return alpha


def pixelize(
    img: Image.Image,
    grid: int = 64,
    colors: int = 24,
    palette: str = "adaptive",
    downscale: str = "box",
    dither: bool = False,
    alpha_threshold: int = 128,
    output_size: int = 256,
    crop: bool = True,
    remove_bg: bool = False,
) -> tuple[Image.Image, Image.Image]:
    """Return (sprite at native grid size, nearest-upscaled preview).

    grid          longest side of the sprite in art pixels (e.g. 32/64/128/256)
    colors        palette size when palette == 'adaptive'
    palette       'adaptive' or a key of PALETTES
    output_size   longest side of the preview; 0 = no upscale (preview = sprite)
    crop          crop to the subject and centre it on a square canvas before downscaling
    remove_bg     make the flat background transparent (for renders without an alpha channel)
    """
    rgba = crop_to_subject(img) if crop else img.convert("RGBA")
    w, h = rgba.size
    k = grid / max(w, h)
    size = (max(1, round(w * k)), max(1, round(h * k)))
    # box = average of each cell: keeps outlines/shading, avoids aliasing noise
    small = rgba.resize(size, Image.NEAREST if downscale == "nearest" else Image.BOX)

    alpha = np.array(small.getchannel("A"))
    rgb = small.convert("RGB")
    if _has_alpha(alpha):
        alpha = np.where(alpha >= alpha_threshold, 255, 0).astype(np.uint8)
    elif remove_bg:
        alpha = _knock_out_background(np.array(rgb))
    else:
        alpha = np.full_like(alpha, 255)
    has_alpha = alpha.min() < 255

    dmode = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    if palette == "adaptive":
        # build the palette from subject pixels only so the background does not eat palette slots
        if has_alpha and alpha.any():
            opaque = np.array(rgb)[alpha > 0].reshape(1, -1, 3)
            pal_src = Image.fromarray(opaque, "RGB").quantize(colors, method=Image.Quantize.MEDIANCUT)
            q = rgb.quantize(palette=pal_src, dither=dmode)
        else:
            q = rgb.quantize(colors, method=Image.Quantize.MEDIANCUT, dither=dmode)
    else:
        q = rgb.quantize(palette=_palette_image(PALETTES[palette]), dither=dmode)

    out = q.convert("RGBA")
    if has_alpha:
        out.putalpha(Image.fromarray(alpha))

    if output_size and output_size != max(out.size):
        s = output_size / max(out.size)
        preview = out.resize((round(out.width * s), round(out.height * s)), Image.NEAREST)
    else:
        preview = out
    return out, preview


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    ap = argparse.ArgumentParser(description="Pixelize an existing image")
    ap.add_argument("input")
    ap.add_argument("--grid", type=int, default=64)
    ap.add_argument("--colors", type=int, default=24)
    ap.add_argument("--palette", default="adaptive", choices=PALETTE_CHOICES)
    ap.add_argument("--downscale", default="box", choices=["box", "nearest"])
    ap.add_argument("--dither", action="store_true")
    ap.add_argument("--no-crop", action="store_true")
    ap.add_argument("--remove-bg", action="store_true")
    ap.add_argument("--size", type=int, default=256, help="preview size, 0 = none")
    a = ap.parse_args()
    src = Path(a.input)
    sprite, preview = pixelize(Image.open(src), a.grid, a.colors, a.palette, a.downscale, a.dither, 128, a.size,
                               not a.no_crop, a.remove_bg)
    sprite.save(src.with_name(f"{src.stem}_px{a.grid}.png"))
    preview.save(src.with_name(f"{src.stem}_px{a.grid}_x{a.size}.png"))
    print("saved", src.with_name(f"{src.stem}_px{a.grid}.png"))
