"""Offline replay.

The cheapest signal in the whole system. A recorded trajectory holds every
observation the model saw; re-running an adapter over those frozen observations
tests a new prompt, a new output parser or a new coordinate convention in
seconds, with no desktop, no reset and -- for the parsing and grounding cases --
no new model call at all.

What replay can answer: would the new parser have accepted this output, does the
new coordinate mapping land inside the intended element. What it cannot answer:
anything about a trajectory that would have diverged. Replay narrows the set of
changes that need a live run; it never replaces one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass
class ReplayFrame:
    obs_id: str
    elements: list[dict]
    screenshot: Path | None
    action: dict | None
    ok: bool | None
    detail: str


def read_trace(run_dir: Path) -> Iterator[dict]:
    p = Path(run_dir) / "trace.jsonl"
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def frames(run_dir: Path) -> list[ReplayFrame]:
    """Pair each observation with the action that followed it."""
    run_dir = Path(run_dir)
    obs: dict[str, dict] = {}
    order: list[str] = []
    out: list[ReplayFrame] = []
    for rec in read_trace(run_dir):
        if rec["t"] == "obs":
            obs[rec["id"]] = rec
            order.append(rec["id"])
        elif rec["t"] == "step" and rec.get("obs") in obs:
            o = obs[rec["obs"]]
            shot = run_dir / "obs" / o["screenshot"] if o.get("screenshot") else None
            out.append(ReplayFrame(
                obs_id=o["id"], elements=o.get("elements", []),
                screenshot=shot if shot and shot.exists() else None,
                action=rec.get("action"), ok=rec.get("ok"), detail=rec.get("detail", "")))
    return out


def actions(run_dir: Path) -> list[dict]:
    return [f.action for f in frames(run_dir) if f.action]


def grounding_pairs(run_dir: Path) -> list[dict]:
    """Harvest a click-target dataset from a real run.

    Every successful click on a known element is a labelled grounding example.
    Accumulated across runs this becomes a millisecond-level eval for exactly
    the failure class that dominates small local models.
    """
    pairs = []
    for f in frames(run_dir):
        a = f.action or {}
        if a.get("kind") not in ("click", "double_click", "right_click") or not f.ok:
            continue
        eid = (a.get("binding") or {}).get("element_id")
        el = next((e for e in f.elements if e["id"] == eid), None) if eid else None
        if el and el.get("rect") and f.screenshot:
            pairs.append({"screenshot": str(f.screenshot), "element_id": eid,
                          "role": el["role"], "label": el.get("label", ""),
                          "rect_logical": el["rect"]})
    return pairs
