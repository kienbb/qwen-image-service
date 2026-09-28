"""Place a transparent (RGBA) model into a UI frame rendered on a black background (runs locally, CPU only).

The UI stays on top of the model: every pixel of the frame that is not (near) black is treated as UI,
so banners and badges overlap the model the way they do in the real game screenshots.

  python tools/compose_screenshot.py ui.png model.png out.png [--top 0.26] [--bottom 0.84] [--max-width 0.9]
"""
import argparse
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qis.engine.imaging import compose  # noqa: E402

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
