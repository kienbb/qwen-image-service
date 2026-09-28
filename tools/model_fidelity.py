"""How faithfully does an output keep a reference model? Finds the model in the output, then scores it (local, CPU).

The reference is an RGBA cut-out of the model. A coarse-to-fine search over scale and offset finds where it sits in
the output by normalised cross-correlation on greyscale (robust to relighting), then:
  struct  NCC of the aligned model vs the output patch (1.0 = identical structure; relighting barely changes it)
  hue     mean absolute hue shift in degrees over saturated model pixels (catches recolouring such as mint -> green)

  python tools/model_fidelity.py model_rgba.png output.png [output2.png ...] [--json placement.json]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qis.engine.imaging import fidelity_bytes  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("outputs", nargs="+")
    ap.add_argument("--json", help="write the placement of the first output here")
    a = ap.parse_args()
    scores = fidelity_bytes(Path(a.model).read_bytes(), [Path(p).read_bytes() for p in a.outputs])
    for p, s in zip(a.outputs, scores, strict=True):
        print(f"{p}: struct={s['struct']:.3f} hue={s['hue']:.1f}deg scale={s['scale']:.2f} at=({s['x']},{s['y']}) "
              f"size={s['width']}x{s['height']}")
    if a.json:
        Path(a.json).write_text(json.dumps(scores[0]), encoding="utf-8")
