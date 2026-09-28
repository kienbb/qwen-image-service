"""Place a transparent (RGBA) model into a UI frame rendered on a black background.

The UI stays on top of the model: every pixel of the frame that is not (near) black is treated as UI,
so banners and badges overlap the model the way they do in the real game screenshots.

  python tools/compose_screenshot.py ui.png model.png out.png [--top 0.26] [--bottom 0.84] [--max-width 0.9]
"""
import argparse

import numpy as np
from PIL import Image


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


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("ui")
    ap.add_argument("model")
    ap.add_argument("out")
    ap.add_argument("--top", type=float, default=0.26, help="top of the model band, fraction of height")
    ap.add_argument("--bottom", type=float, default=0.84, help="bottom of the model band, fraction of height")
    ap.add_argument("--max-width", type=float, default=0.9, help="max model width, fraction of width")
    a = ap.parse_args()
    compose(Image.open(a.ui), Image.open(a.model), a.top, a.bottom, a.max_width).save(a.out)
    print("saved", a.out)
