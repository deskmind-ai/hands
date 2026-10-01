"""Draw each step of a run onto the screenshot it was decided on: a box around the element acted on, and a banner with
the step, what it did and what came of it. For reading a trace at a glance (cua draws its clicks on its screenshots
the same way); the files go to <run>/annotated/step-NN.png, beside an index.md.

    python tools/annotate_trace.py runs/do-20261001-181504

A run keeps its screenshots under obs/ unless it was made with --no-screenshots. Elements' rects are in the
window's points and the screenshots are stored at that size, so a rect is drawn as it is; a step on a synthetic
control (save, close, open a folder's file: done by script) has nothing on screen to box, and gets the banner only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

RED = (220, 38, 38)


def _font(size: int):
    for name in ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def annotate(run: Path) -> list[Path]:
    """The annotated images written for `run`, one per step that has a screenshot."""
    obs, steps = {}, []
    for line in (run / "trace.jsonl").read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec.get("t") == "obs":
            obs[rec["id"]] = rec
        elif rec.get("t") == "step":
            steps.append(rec)
    out_dir = run / "annotated"
    out_dir.mkdir(exist_ok=True)
    written, index = [], []
    for st in steps:
        o = obs.get(st.get("obs") or "")
        shot = run / "obs" / (o or {}).get("screenshot", "") if o else None
        if not o or not shot or not shot.is_file():
            continue
        im = Image.open(shot).convert("RGB")
        logical = (o.get("geometry") or {}).get("logical") or list(im.size)
        sx, sy = im.size[0] / max(1, logical[0]), im.size[1] / max(1, logical[1])
        draw = ImageDraw.Draw(im)
        eid = ((st.get("action") or {}).get("binding") or {}).get("element_id")
        el = next((e for e in o.get("elements") or [] if e.get("id") == eid), None)
        if el and el.get("rect"):
            x, y, w, h = el["rect"]
            box = (x * sx, y * sy, (x + w) * sx, (y + h) * sy)
            draw.rectangle(box, outline=RED, width=max(2, round(3 * sx)))
        what = st.get("describe") or st.get("kind") or ""
        result = ("ok" if st.get("ok") else "refused") + (": " + str(st.get("detail"))[:90] if st.get("detail") else "")
        banner = f"step {st['n']}  {what}"[:110]
        f = _font(max(12, round(14 * sx)))
        pad = round(6 * sx)
        lines = [banner, result]
        height = sum(draw.textbbox((0, 0), t, font=f)[3] for t in lines) + pad * (len(lines) + 1)
        # Above the screenshot, never over it: the top of a window is often what the step was about.
        canvas = Image.new("RGB", (im.size[0], im.size[1] + height), (255, 255, 255))
        canvas.paste(im, (0, height))
        im, draw = canvas, ImageDraw.Draw(canvas)
        yy = pad
        for t, colour in zip(lines, ((20, 20, 20), RED if not st.get("ok") else (90, 90, 90))):
            draw.text((pad, yy), t, font=f, fill=colour)
            yy += draw.textbbox((0, 0), t, font=f)[3] + pad
        path = out_dir / f"step-{st['n']:02d}.png"
        im.save(path)
        written.append(path)
        index.append(f"- [step {st['n']}]({path.name}) {what}" + ("" if st.get("ok") else " -- refused"))
    (out_dir / "index.md").write_text(f"# {run.name}\n\n" + "\n".join(index) + "\n", encoding="utf-8")
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", type=Path)
    a = ap.parse_args(argv)
    if not (a.run / "trace.jsonl").is_file():
        ap.error(f"no trace.jsonl in {a.run}")
    paths = annotate(a.run)
    print(f"{len(paths)} step(s) annotated -> {a.run / 'annotated'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
