"""Keep and inspect the lessons store (deskmind_hands/lessons.py).

    python tools/lessons.py add "When a goal asks for a table's rows in its order, ..." [--app BUNDLE] [--keyword K]
                                [--run RUN_DIR] [--by human|model:NAME]
    python tools/lessons.py list [--all]
    python tools/lessons.py recall "<goal>" [--app BUNDLE]      # what a run with this goal would be told, and why
    python tools/lessons.py retire ID

A lesson is advice for a kind of task, phrased so it applies beyond the run it came from: no file names, row values
or window titles that only that run had. Distilling lessons from a run's trace with a stronger model is the next
step; its output goes through `add` after review, with --by model:NAME.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskmind_hands import lessons  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("text")
    a.add_argument("--app", action="append", default=[])
    a.add_argument("--keyword", action="append", default=[])
    a.add_argument("--run", action="append", default=[], help="run directory or id the lesson came from")
    a.add_argument("--by", default="human")
    l = sub.add_parser("list")
    l.add_argument("--all", action="store_true", help="include retired lessons")
    r = sub.add_parser("recall")
    r.add_argument("goal")
    r.add_argument("--app", action="append", default=[])
    t = sub.add_parser("retire")
    t.add_argument("id")
    args = ap.parse_args(argv)

    if args.cmd == "add":
        lesson = lessons.Lesson(text=args.text, apps=args.app, keywords=args.keyword,
                                source={"by": args.by, "runs": [Path(x).name for x in args.run]})
        print(lessons.save(lesson))
    elif args.cmd == "list":
        for x in lessons.load():
            if args.all or x.status == "active":
                print(f"{x.id}  {x.status:7} {','.join(x.apps) or '-':28} {x.text}")
    elif args.cmd == "recall":
        for x, s in lessons.recall(args.goal, set(args.app)):
            print(f"{s:6.2f}  {x.id}  {x.text}")
    elif args.cmd == "retire":
        p = lessons.store_dir() / f"{args.id}.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        d["status"] = "retired"
        p.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
