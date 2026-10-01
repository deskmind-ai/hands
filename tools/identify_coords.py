"""Deployment acceptance for a local vision model: what does it actually emit?

Two questions have to be answered with evidence before a single capability
number is worth recording, and both are cheap:

1. **What output format does this checkpoint speak?** UI-TARS was trained to emit
   `Action: click(start_box='(x,y)')`, Holo emits something else, and forcing an
   unfamiliar JSON schema onto either produces garbage that looks exactly like a
   weak model. So this prints the raw completion before parsing anything.

2. **Which coordinate convention are those numbers in?** Absolute pixels of the
   image it was shown, 0-1000 normalised, or 0-1 normalised. Guessing wrong is
   worth tens of points of apparent grounding accuracy, and the failure looks
   like bad aim rather than a configuration error.

The probe shows a synthetic screen with one obvious target whose rectangle is
known, asks for a click, then scores every convention by whether the mapped
point lands inside that rectangle.

    .venv/bin/python tools/identify_coords.py --model mlx-community/UI-TARS-1.5-7B-4bit
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.geometry import (CoordSpace, ImageTransform, Point, Rect, ScreenGeometry,
                            Size, to_logical)

CONVENTIONS = {
    "model_image (absolute pixels)": CoordSpace.MODEL_IMAGE,
    "norm_1000 (0-1000)": CoordSpace.NORM_1000,
    "norm_unit (0-1)": CoordSpace.NORM_UNIT,
}

#: Where the target sits in the rendered screen, in image pixels.
TARGET = Rect(980, 300, 240, 60)
SCREEN = Size(1512, 982)


def render() -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", SCREEN.as_tuple(), (242, 242, 246))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 26)
        big = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 30)
    except OSError:
        font = big = ImageFont.load_default()
    # Distractors, so "click the button" is not simply "click the only thing".
    for i in range(10):
        y = 120 + i * 62
        d.rectangle([80, y, 820, y + 48], fill=(252, 252, 253), outline=(214, 214, 222), width=2)
        d.text((104, y + 12), f"document-{i:02d}.txt", fill=(40, 40, 50), font=font)
    d.rectangle([TARGET.x, TARGET.y, TARGET.x + TARGET.w, TARGET.y + TARGET.h],
                fill=(208, 226, 252), outline=(52, 96, 190), width=4)
    d.text((TARGET.x + 46, TARGET.y + 14), "Rename", fill=(16, 24, 60), font=big)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


#: Number pairs, in the several shapes these checkpoints use.
PATTERNS = [
    r"<\|box_start\|>\s*\(?\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)",
    r"start_box\s*=\s*'?\(?\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)",
    r'"?(?:x|point)"?\s*[:=]\s*\[?\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)',
    r"\(\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\)",
    r"\[\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\]",
]


def extract(text: str) -> Point | None:
    for pat in PATTERNS:
        m = re.search(pat, text)
        if m:
            return Point(float(m.group(1)), float(m.group(2)))
    return None


PROMPTS = {
    "ui-tars": ("You are a GUI agent. Look at the screenshot and click the Rename button.\n"
                "Output your action."),
    "plain": "Click the Rename button. Give the pixel coordinates of the click.",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="mlx-community/UI-TARS-1.5-7B-4bit")
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--long-edge", type=int, default=1512)
    args = ap.parse_args()

    try:
        from mlx_vlm import generate, load
        from mlx_vlm.prompt_utils import apply_chat_template
        from PIL import Image
    except ImportError as exc:
        print(f"needs the venv: .venv/bin/python tools/identify_coords.py  ({exc})")
        return 1

    png = render()
    tx = ImageTransform.fit(SCREEN, args.long_edge)
    img = Image.open(io.BytesIO(png)).convert("RGB")
    if tx.target.as_tuple() != SCREEN.as_tuple():
        img = img.resize(tx.target.as_tuple(), Image.LANCZOS)
    print(f"screen {SCREEN.as_tuple()} -> model image {tx.target.as_tuple()}")
    print(f"target rect in image pixels: x={TARGET.x} y={TARGET.y} "
          f"w={TARGET.w} h={TARGET.h}  centre={TARGET.center.rounded()}\n")

    print(f"loading {args.model} ...")
    model, processor = load(args.model)
    config = getattr(model, "config", None)

    geometry = ScreenGeometry(SCREEN, SCREEN)   # 1:1 here; the real driver differs
    target_logical = Rect(TARGET.x, TARGET.y, TARGET.w, TARGET.h)

    for label, prompt in PROMPTS.items():
        print(f"\n══ prompt style: {label}")
        formatted = apply_chat_template(processor, config, prompt, num_images=1)
        out = generate(model, processor, formatted, [img],
                       max_tokens=args.max_tokens, verbose=False)
        text = out if isinstance(out, str) else getattr(out, "text", str(out))
        print("  raw output:")
        for line in text.strip().splitlines()[:8]:
            print(f"    {line[:150]}")

        pt = extract(text)
        if pt is None:
            print("  no coordinate pair found in the output")
            continue
        print(f"  extracted numbers: ({pt.x:g}, {pt.y:g})")
        for name, space in CONVENTIONS.items():
            try:
                mapped = to_logical(pt, space, geometry, tx)
            except Exception as exc:  # noqa: BLE001
                print(f"    {name:32} unmappable ({exc})")
                continue
            hit = target_logical.contains(mapped)
            print(f"    {name:32} -> ({mapped.x:7.1f}, {mapped.y:7.1f})  "
                  f"{'HIT' if hit else 'miss'}")

    print("\nA convention that hits is evidence; a convention that misses on one "
          "probe is not proof it is wrong. Run this against several targets "
          "before freezing the setting.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
