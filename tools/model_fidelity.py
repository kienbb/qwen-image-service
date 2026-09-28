"""How faithfully does an output keep a reference model? Finds the model in the output, then scores it.

The reference is an RGBA cut-out of the model (e.g. from the extraction step). A coarse-to-fine search over scale and
offset finds where it sits in the output by normalised cross-correlation on greyscale (robust to relighting), then:
  struct  NCC of the aligned model vs the output patch (1.0 = identical structure; relighting barely changes it)
  hue     mean absolute hue shift in degrees over saturated model pixels (catches recolouring such as mint -> green)
The placement can be written as JSON for pasting the model back.

  python tools/model_fidelity.py model_rgba.png output.png [output2.png ...] [--json placement.json]
"""
import argparse
import json

import numpy as np
from PIL import Image


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


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("outputs", nargs="+")
    ap.add_argument("--json", help="write the placement of the first output here")
    a = ap.parse_args()
    model = Image.open(a.model).convert("RGBA")
    model = model.crop(model.getchannel("A").point(lambda v: 255 if v >= 128 else 0).getbbox())
    for i, p in enumerate(a.outputs):
        out = Image.open(p)
        s, x, y, c = locate(out, model)
        h = hue_shift(out, model, s, x, y)
        print(f"{p}: struct={c:.3f} hue={h:.1f}deg scale={s:.2f} at=({x},{y}) size={round(model.width * s)}x{round(model.height * s)}")
        if a.json and i == 0:
            json.dump({"scale": s, "x": x, "y": y, "struct": c, "hue": h}, open(a.json, "w"))
