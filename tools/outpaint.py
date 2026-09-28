"""Resize an image to a new aspect ratio without redrawing it: scale uniformly, then outpaint only the new area.

Pipeline (draft -> final, like the rest of the tool):
  1. canvas at draft size: the original scaled to fit, the empty area pre-filled with blurred edge colour as a hint
  2. Qwen-Image 2.1 edit (1 reference = the canvas) paints the empty area as a continuation of the scene
  3. final size: the draft is upscaled (Real-ESRGAN via tools/upscale.py), a light Qwen img2img pass at final size
     (tools/refine.py) redraws the soft extension, then the ORIGINAL pixels are pasted back over their band with a
     feathered seam, so the source image is preserved exactly (only scaled)

  python tools/outpaint.py src.png --size 1080x1920 --prompt "..." [--anchor-y 0.44] [--seed 1] [--out out.png]

--anchor-y places the centre of the original image (fraction of the final height); keep focal points out of
areas a UI will cover (safezone).
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import gen  # noqa: E402

PY = sys.executable


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--size", required=True, help="final WxH")
    ap.add_argument("--prompt", required=True, help="what the extended area should contain")
    ap.add_argument("--anchor-y", type=float, default=0.5)
    ap.add_argument("--draft", type=int, default=768, help="draft area side (draft has about draft*draft pixels)")
    ap.add_argument("--steps", type=int, default=6)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--feather", type=int, default=24, help="seam width in final pixels")
    ap.add_argument("--scene", default="", help="short description of the whole image, used by the refine pass")
    ap.add_argument("--refine-nodes", default="0.5, 0.25",
                    help="Viggle sigma nodes for the final-size refine pass; empty string = ESRGAN only")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    W, H = (int(x) for x in a.size.lower().split("x"))
    out = Path(a.out)
    work = out.with_suffix("")
    work.mkdir(parents=True, exist_ok=True)
    src = Image.open(a.image).convert("RGB")
    dw, dh = gen.dims(a.draft, f"{W}:{H}")

    t = time.time()
    hint_canvas(src, dw, dh, a.anchor_y).save(work / "1_canvas.png")
    prompt = ("Image 1 shows a scene in its middle; the blurry areas are empty and must be painted. "
              f"Extend the scene naturally into the blurry areas: {a.prompt} "
              "Keep the existing scene exactly unchanged, same art style, colours, lighting and perspective, "
              "seamless continuation with no borders or frames.")
    ns = argparse.Namespace(mode="turbo", steps=a.steps, size=a.draft, aspect="1:1", negative="", cfg=1.0, grid=0,
                            colors=24, palette="adaptive", dither=False, preview=0, no_crop=True, keep_bg=True,
                            clip_device="default")
    res = gen.run(gen.build_workflow(ns, prompt, a.seed, f"outpaint/{work.name}", [gen.upload(str(work / "1_canvas.png"))]))
    img = res["outputs"]["save_raw"]["images"][0]
    draft = Image.open(gen.ROOT / "output" / img["subfolder"] / img["filename"]).convert("RGB")
    draft.save(work / "2_draft.png")
    paste_back(draft, src, a.anchor_y, max(4, a.feather * dw // W)).save(work / "3_draft_pasted.png")
    t_draft = time.time() - t

    t = time.time()
    subprocess.run([PY, str(ROOT / "tools" / "upscale.py"), str(work / "2_draft.png"), "--size", f"{W}x{H}",
                    "--model", "RealESRGAN_x4plus.pth", "--out", str(work / "4_upscaled.png")], check=True)
    # the sharp original goes in first so the refine pass matches its detail level across the seam
    upscaled = paste_back(Image.open(work / "4_upscaled.png").convert("RGB"), src, a.anchor_y, a.feather)
    upscaled.save(work / "5_upscaled_pasted.png")
    if a.refine_nodes:
        # the upscaled extension is soft (painted at draft size); a light img2img pass at final size redraws its detail
        rw, rh = (round(v / 32) * 32 for v in (W, H))
        (work / "refine_prompt.txt").write_text(f"{a.scene} {a.prompt}".strip(), encoding="utf-8")
        subprocess.run([PY, str(ROOT / "tools" / "refine.py"), str(work / "5_upscaled_pasted.png"), "--size", f"{rw}x{rh}",
                        "--prompt-file", str(work / "refine_prompt.txt"), "--nodes", a.refine_nodes,
                        "--seed", str(a.seed), "--out", str(work / "6_refined.png")], check=True)
        upscaled = Image.open(work / "6_refined.png").convert("RGB").resize((W, H), Image.LANCZOS)
    final = paste_back(upscaled, src, a.anchor_y, a.feather)
    final.save(out)
    print(f"draft {dw}x{dh} {t_draft:.1f}s | final {W}x{H} {time.time() - t:.1f}s -> {out}")


if __name__ == "__main__":
    main()
