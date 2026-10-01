"""Names for unnamed controls, from our local grounder.

Electron apps (chat clients first) expose their toolbar icons to the accessibility tree as a bare "按钮": a planner that
reads text cannot tell send from emoji. The screenshot can. For each such app a small vocabulary of controls it is
known to have is grounded on the current screenshot by a local grounding model (any server on
127.0.0.1 that answers the /ground request below; the screenshot is passed by path and never leaves the machine), and a button that exactly one
description lands on gets that description's name. A button two descriptions land on gets both ("image / expand")
-- the planner sees the doubt instead of a confident wrong name.

Grounding the vocabulary costs ~5 s per description, so results are cached by the layout of the unnamed buttons: the
same toolbar in the same place is named once per run. The capture is sent at the window's point size. Neighbouring
icons are not always told apart (two captures of one toolbar put the upload icon on different buttons), which is why a
doubly named button keeps both names.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

from .apps import APPS

GROUNDER_URL = os.environ.get("HANDS_GROUNDER_URL", "http://127.0.0.1:8010/ground")
UNNAMED = {"按钮", "button", "Button", ""}

#: What each app's unnamed buttons can be, from the local apps file (`grounding:`). The description is the
#: grounding query; the key is the name shown. Empty unless configured.
VOCAB: dict[str, dict[str, str]] = APPS.grounding

_cache: dict[tuple, dict[str, str]] = {}


def _layout(els) -> tuple:
    return tuple(sorted((round(e.rect.x), round(e.rect.y), round(e.rect.w), round(e.rect.h))
                        for e in els))


def name_unnamed(app: str, els: list, shot: Path, width: float, height: float, pad: float = 3.0) -> int:
    """Give unnamed buttons of `els` (window-local rects, window `width` x `height` points) names grounded on `shot`.
    Returns how many were named; 0 when the app has no vocabulary or the grounder is not running."""
    vocab = VOCAB.get(app.lower())
    unnamed = [e for e in els if (e.role or "").lower() == "button" and (e.label or "").strip() in UNNAMED
               and e.rect and e.rect.w > 0]
    if not vocab or not unnamed or not shot.exists():
        return 0
    key = (app.lower(), round(width), round(height), _layout(unnamed))
    if key not in _cache:
        try:
            req = urllib.request.Request(GROUNDER_URL, data=json.dumps({"image": str(shot.resolve()),
                                                                        "size": [round(width), round(height)],
                                                                        "queries": vocab}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as r:
                points = json.load(r)["points"]
        except (OSError, ValueError, KeyError):
            return 0
        names: dict[str, list[str]] = {}
        for name, pt in points.items():
            if not pt:
                continue
            x, y = pt[0] * width, pt[1] * height
            hits = [e for e in unnamed if e.rect.x - pad <= x <= e.rect.x + e.rect.w + pad
                    and e.rect.y - pad <= y <= e.rect.y + e.rect.h + pad]
            if len(hits) == 1:                  # overlapping duplicates (same rect twice) count as one below
                names.setdefault(_rkey(hits[0]), []).append(name)
            elif hits and len({_rkey(h) for h in hits}) == 1:
                names.setdefault(_rkey(hits[0]), []).append(name)
        _cache[key] = {k: " / ".join(v) for k, v in names.items()}
    named = 0
    for e in unnamed:
        n = _cache[key].get(_rkey(e))
        if n:
            e.label = n
            named += 1
    return named


def _rkey(e) -> str:
    return f"{round(e.rect.x)},{round(e.rect.y)},{round(e.rect.w)},{round(e.rect.h)}"
