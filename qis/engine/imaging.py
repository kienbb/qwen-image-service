"""CPU image operations: pixelize, background knock-out, compose, model fidelity and outpaint helpers.

Moved from tools/*.py so the service and the CLI share one implementation. The *_bytes wrappers take and return
PNG bytes so they can run in a process pool.
"""
from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageFilter

from ..comfy_nodes.pixelart_retro.pixelize import PALETTE_CHOICES, _knock_out_background, pixelize

__all__ = ["PALETTE_CHOICES", "pixelize", "compose", "locate", "hue_shift", "place", "hint_canvas", "paste_back"]


def _open(data: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(data))
    im.load()
    return im


def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG", compress_level=4)
    return buf.getvalue()


def compose(ui: Image.Image, model: Image.Image, top: float, bottom: float, max_width: float) -> Image.Image:
    ui = ui.convert("RGB")
    W, H = ui.size
    model = model.convert("RGBA")
    model = model.crop(model.getchannel("A").point(lambda a: 255 if a >= 128 else 0).getbbox())

    # fit the model into the band between top and bottom, centred horizontally
    box_h, box_w = (bottom - top) * H, max_width * W
    k = min(box_h / model.height, box_w / model.width)
    model = model.resize((round(model.width * k), round(model.height * k)), Image.LANCZOS)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    layer.alpha_composite(model, ((W - model.width) // 2, round(top * H + (box_h - model.height) / 2)))

    # UI mask: soft ramp on luminance so anti-aliased UI edges blend instead of leaving black fringes
    lum = np.asarray(ui.convert("L"), dtype=np.float32)
    m = np.clip((lum - 10.0) / 20.0, 0.0, 1.0)[..., None]
    out = np.asarray(ui, dtype=np.float32) * m + np.asarray(layer.convert("RGB"), dtype=np.float32) * (1 - m)
    return Image.fromarray(out.round().astype(np.uint8))


def _ncc(a, b, m):
    a, b = a[m], b[m]
    a, b = a - a.mean(), b - b.mean()
    d = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / d) if d else 0.0


def _gray(img, size=None):
    g = img.convert("L")
    return np.asarray(g.resize(size, Image.BILINEAR) if size else g, dtype=np.float32)


def locate(out_img: Image.Image, model_img: Image.Image, down: int = 8):
    """(scale, x, y, ncc): best placement of the model, scale relative to the model cut-out's own size."""
    ow, oh = out_img.size
    small = _gray(out_img, (ow // down, oh // down))
    best = (-2.0, None)
    for scale in np.arange(0.5, 3.0, 0.05):
        mw, mh = round(model_img.width * scale), round(model_img.height * scale)
        if mw > ow or mh > oh:
            continue
        m = model_img.resize((max(1, mw // down), max(1, mh // down)), Image.BILINEAR)
        g, mask = _gray(m), np.asarray(m.getchannel("A")) > 200
        h, w = g.shape
        for y in range(0, small.shape[0] - h + 1):
            for x in range(0, small.shape[1] - w + 1):
                c = _ncc(g, small[y:y + h, x:x + w], mask)
                if c > best[0]:
                    best = (c, (scale, x * down, y * down))
    scale, x0, y0 = best[1]
    full = _gray(out_img)
    best_f = (-2.0, None)
    for s in np.arange(scale - 0.05, scale + 0.051, 0.01):
        m = model_img.resize((round(model_img.width * s), round(model_img.height * s)), Image.BILINEAR)
        g, mask = _gray(m), np.asarray(m.getchannel("A")) > 200
        h, w = g.shape
        for y in range(max(0, y0 - down), min(oh - h, y0 + down) + 1, 2):
            for x in range(max(0, x0 - down), min(ow - w, x0 + down) + 1, 2):
                c = _ncc(g, full[y:y + h, x:x + w], mask)
                if c > best_f[0]:
                    best_f = (c, (float(s), x, y))
    return (*best_f[1], best_f[0])


def hue_shift(out_img, model_img, scale, x, y):
    m = model_img.resize((round(model_img.width * scale), round(model_img.height * scale)), Image.BILINEAR)
    a = np.asarray(m.convert("HSV"), dtype=np.float32)
    o = np.asarray(out_img.convert("RGB").crop((x, y, x + m.width, y + m.height)).convert("HSV"), dtype=np.float32)
    sel = (np.asarray(m.getchannel("A")) > 200) & (a[..., 1] > 64) & (o[..., 1] > 64)
    if not sel.any():
        return 0.0
    d = np.abs(a[..., 0][sel] - o[..., 0][sel]) / 255
    return float(np.minimum(d, 1 - d).mean() * 360)


def place(src: Image.Image, W: int, H: int, anchor_y: float) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Scale src to fit W x H (no crop) and return it with its box (x0, y0, x1, y1) on the W x H canvas."""
    k = min(W / src.width, H / src.height)
    w, h = round(src.width * k), round(src.height * k)
    x0 = (W - w) // 2
    y0 = min(max(round(anchor_y * H - h / 2), 0), H - h)
    return src.resize((w, h), Image.LANCZOS), (x0, y0, x0 + w, y0 + h)


def hint_canvas(src: Image.Image, W: int, H: int, anchor_y: float) -> Image.Image:
    """Canvas with the scaled source and its edges stretched + blurred into the empty area."""
    img, (x0, y0, x1, y1) = place(src, W, H, anchor_y)
    canvas = img.resize((W, H), Image.BILINEAR).filter(ImageFilter.GaussianBlur(max(W, H) // 12))
    if y0 > 0:  # stretch the top rows / bottom rows so the colours continue the right way
        canvas.paste(img.crop((0, 0, img.width, 8)).resize((x1 - x0, y0)).filter(ImageFilter.GaussianBlur(24)), (x0, 0))
    if y1 < H:
        canvas.paste(img.crop((0, img.height - 8, img.width, img.height)).resize((x1 - x0, H - y1)).filter(ImageFilter.GaussianBlur(24)), (x0, y1))
    canvas.paste(img, (x0, y0))
    return canvas


def paste_back(base: Image.Image, src: Image.Image, anchor_y: float, feather: int) -> Image.Image:
    """Put the original pixels back over their band with a soft edge on the sides that border new content."""
    img, (x0, y0, x1, y1) = place(src, *base.size, anchor_y)
    mask = Image.new("L", img.size, 255)
    if feather:
        m = mask.load()
        for y in range(img.height):
            d = min(y if y0 > 0 else feather, img.height - 1 - y if y1 < base.height else feather)
            a = min(255, round(255 * d / feather))
            for x in range(img.width):
                dx = min(x if x0 > 0 else feather, img.width - 1 - x if x1 < base.width else feather)
                m[x, y] = min(a, round(255 * min(dx, feather) / feather))
    out = base.copy()
    out.paste(img, (x0, y0), mask)
    return out


# ---------- picklable wrappers (process pool) ----------
def pixelize_bytes(data: bytes, grid: int, colors: int, palette: str, downscale: str, dither: bool,
                   preview_size: int, crop: bool, remove_bg: bool) -> tuple[bytes, bytes]:
    sprite, preview = pixelize(_open(data), grid, colors, palette, downscale, dither, 128, preview_size, crop, remove_bg)
    return _png(sprite), _png(preview)


def remove_bg_bytes(data: bytes, tolerance: float = 40.0) -> bytes:
    im = _open(data).convert("RGBA")
    rgba = np.array(im)
    if (rgba[..., 3] < 128).mean() > 0.01:  # already transparent
        return _png(im)
    alpha = _knock_out_background(rgba[..., :3], tolerance)
    im.putalpha(Image.fromarray(alpha))
    return _png(im)


def compose_bytes(ui: bytes, model: bytes, top: float, bottom: float, max_width: float) -> bytes:
    return _png(compose(_open(ui), _open(model), top, bottom, max_width))


def fidelity_bytes(model: bytes, outputs: list[bytes]) -> list[dict]:
    m = _open(model).convert("RGBA")
    bbox = m.getchannel("A").point(lambda v: 255 if v >= 128 else 0).getbbox()
    if bbox:
        m = m.crop(bbox)
    res = []
    for data in outputs:
        out = _open(data)
        s, x, y, c = locate(out, m)
        res.append({"struct": round(c, 4), "hue": round(hue_shift(out, m, s, x, y), 2), "scale": round(s, 3), "x": x, "y": y,
                    "width": round(m.width * s), "height": round(m.height * s)})
    return res


def resize_bytes(data: bytes, width: int, height: int, method: str) -> bytes:
    im = _open(data)
    im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
    return _png(im.resize((width, height), Image.NEAREST if method == "nearest" else Image.LANCZOS))
