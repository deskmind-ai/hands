"""How much does the accessibility tree actually give us, per application?

This project's advantage over pixel-level computer use rests on the structured
channel: the model picks an element id out of a list instead of estimating a
coordinate. That advantage is not a property of the harness, it is a property of
each application, and it varies enormously -- so it is worth knowing which
applications keep it and which take it away before deciding what to build.

Reports, per running application:

  elements        how many nodes the tree exposes at all
  actionable      how many claim to be clickable
  settable        how many accept text -- the single most useful signal there is
  labelled        how many carry a label a model could reason about
  content text    whether any *user-visible* text appears, as opposed to
                  internal view class names like "ClientView"

The last column is the one that matters. An Electron application can report
thirty nodes and still be, for our purposes, a blank rectangle: the content is
drawn, not exposed, so everything must come from pixels.

    .venv/bin/python tools/ax_survey.py [--apps com.apple.finder,com.example.chat]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: Names an app gives its own plumbing. Present in the tree, useless to a model.
INTERNAL = re.compile(
    r"(View|Widget|Delegate|Container|Layer|Group|Wrapper|Root|Client|Contents)$")


def peekaboo(*args: str) -> dict | None:
    try:
        p = subprocess.run(["peekaboo", *args], capture_output=True, text=True, timeout=45)
        d = json.loads(p.stdout)
        return d.get("data") if d.get("success") else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def running_apps() -> list[tuple[str, str]]:
    script = ('tell application "System Events" to get bundle identifier of '
              'every process whose background only is false')
    try:
        p = subprocess.run(["/usr/bin/osascript", "-e", script],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for bid in (x.strip() for x in p.stdout.split(",")):
        if bid and bid != "missing value":
            out.append((bid, bid.split(".")[-1]))
    return out


def survey(bundle: str, scratch: Path) -> dict | None:
    data = peekaboo("see", "--app", bundle, "--json",
                    "--path", str(scratch / "s.png"), "--format", "png")
    if not data:
        return None
    els = data.get("ui_elements") or []
    labelled = [e for e in els if (e.get("label") or "").strip()]
    # Content text = a label that is not an internal view class name and is not
    # a window-chrome control. This is what a model can actually reason about.
    content = [e for e in labelled
               if not INTERNAL.search(e["label"])
               and e.get("ax_role") not in ("AXWindow", "AXGroup", "AXUnknown")]
    return {
        "app": data.get("application_name", bundle),
        "bundle": bundle,
        "elements": len(els),
        "actionable": sum(1 for e in els if e.get("is_actionable")),
        "settable": sum(1 for e in els if e.get("is_value_settable")),
        "labelled": len(labelled),
        "content": len(content),
        "samples": [e["label"][:24] for e in content[:4]],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apps", default=None,
                    help="comma-separated bundle ids; default is everything running")
    args = ap.parse_args()

    scratch = Path(tempfile.mkdtemp())
    targets = ([(b.strip(), b.strip().split(".")[-1]) for b in args.apps.split(",")]
               if args.apps else running_apps())
    if not targets:
        print("no applications to survey")
        return 1

    print(f"{'app':22} {'elem':>5} {'act':>5} {'set':>5} {'label':>6} {'content':>8}   sample")
    print("-" * 96)
    rows = []
    for bundle, _ in targets:
        r = survey(bundle, scratch)
        if r is None:
            continue
        rows.append(r)
        print(f"{r['app'][:22]:22} {r['elements']:>5} {r['actionable']:>5} "
              f"{r['settable']:>5} {r['labelled']:>6} {r['content']:>8}   "
              + ", ".join(r["samples"]))

    if rows:
        rich = [r for r in rows if r["content"] >= 10]
        poor = [r for r in rows if r["content"] < 10]
        print(f"\n{len(rich)} application(s) expose usable structure, {len(poor)} do not.")
        if poor:
            print("blank to the structured channel: "
                  + ", ".join(r["app"] for r in poor))
            print("On these, every target must be found in pixels -- the element-id "
                  "advantage does not exist and the comparison is with whoever aims best.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
