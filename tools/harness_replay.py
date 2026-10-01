"""The harness's replay snapshots (layer 2 of docs/harness-eval.md): what the planner is shown, pinned per step.

Each case under tests/replay/<name>/ is a recorded `hands do` run (manifest.json + trace.jsonl, no screenshots) and
the expected replay (expected.json): the outcome, any divergences, and every request the adapter built, normalized.
A change to the harness that alters what the planner is shown fails `check` with the first differing request; if
the change is meant, `--update` rewrites the expectations and the diff goes to review -- and to whoever trains the
planner, since it is a change to what the model sees.

    python tools/harness_replay.py add runs/do-20260930-172539 --name d4-real-ask
    python tools/harness_replay.py check              # exit 1 on any difference
    python tools/harness_replay.py check --update     # accept the current output
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from deskmind_hands.replay import normalize, replay   # noqa: E402

CASES = REPO / "tests" / "replay"
#: The recording machine's home directory never goes into the (public) corpus.
HOME = str(Path.home())


#: ...and neither does its login name, which Finder and open panels show as the home folder in their sidebar.
LOGIN = Path.home().name


def _sanitize(text: str) -> str:
    text = text.replace(HOME, "/Users/user")
    return re.sub(rf"(?<![\w.-]){re.escape(LOGIN)}(?![\w.-])", "user", text) if LOGIN else text


#: The optional parts of what the planner is shown and of what may stop a run. A case replays with the ones it was
#: recorded with (env.json) and every other one off, whatever the caller's environment: a run recorded through the
#: app, with the DONE check on, replayed without it built two requests fewer.
SWITCHES = ("HANDS_EFFECT_NOTES", "HANDS_ENV_SECTIONS", "HANDS_MARK_NEW", "HANDS_PROGRESS", "HANDS_LESSONS",
            "HANDS_DONE_CHECK")


def expected_of(case: Path) -> dict:
    env = json.loads((case / "env.json").read_text(encoding="utf-8")) if (case / "env.json").exists() else {}
    with mock.patch.dict(os.environ, {k: "0" for k in SWITCHES} | env):
        r = replay(case)
    return {"result": r.to_json() | {"run": case.name},
            "requests": [normalize(q) for q in r.requests]}


def add(run: Path, name: str, env: dict[str, str] | None = None) -> Path:
    case = CASES / name
    case.mkdir(parents=True, exist_ok=True)
    for f in ("manifest.json", "trace.jsonl"):
        (case / f).write_text(_sanitize((run / f).read_text(encoding="utf-8")), encoding="utf-8")
    if env:
        (case / "env.json").write_text(json.dumps(env, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    exp = expected_of(case)
    (case / "expected.json").write_text(json.dumps(exp, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                        encoding="utf-8")
    return case


def first_difference(a, b, path: str = "") -> str | None:
    """Where two JSON values first differ, as a path and the two values."""
    if type(a) is not type(b):
        return f"{path or '/'}: {json.dumps(a, ensure_ascii=False)[:160]} != {json.dumps(b, ensure_ascii=False)[:160]}"
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f"{path}/{k}: {'missing now' if k not in b else 'new'}"
            d = first_difference(a[k], b[k], f"{path}/{k}")
            if d:
                return d
        return None
    if isinstance(a, list):
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_difference(x, y, f"{path}[{i}]")
            if d:
                return d
        return None if len(a) == len(b) else f"{path}: {len(a)} items expected, {len(b)} now"
    return None if a == b else f"{path}: {json.dumps(a, ensure_ascii=False)[:160]} != {json.dumps(b, ensure_ascii=False)[:160]}"


def check(update: bool = False, only: list[str] | None = None) -> list[tuple[str, str]]:
    """(case, what differs) for every case whose replay no longer matches."""
    failures = []
    for case in sorted(p for p in CASES.iterdir() if (p / "expected.json").exists()) if CASES.exists() else []:
        if only and case.name not in only:
            continue
        want = json.loads((case / "expected.json").read_text(encoding="utf-8"))
        got = json.loads(json.dumps(expected_of(case), ensure_ascii=False, sort_keys=True))
        diff = first_difference(want, got)
        if diff and update:
            (case / "expected.json").write_text(json.dumps(got, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                                encoding="utf-8")
            print(f"updated  {case.name}: {diff}")
        elif diff:
            failures.append((case.name, diff))
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("run", type=Path)
    a.add_argument("--name", required=True)
    a.add_argument("--env", action="append", default=[], metavar="HANDS_X=1",
                   help="a switch the run was recorded with (" + ", ".join(SWITCHES) + ")")
    c = sub.add_parser("check")
    c.add_argument("--update", action="store_true")
    c.add_argument("cases", nargs="*")
    args = ap.parse_args()
    os.environ.setdefault("HANDS_EFFECT_NOTES", "0")
    if args.cmd == "add":
        env = dict(e.split("=", 1) for e in args.env)
        unknown = set(env) - set(SWITCHES)
        if unknown:
            ap.error(f"not a switch: {', '.join(sorted(unknown))}")
        case = add(args.run, args.name, env)
        r = json.loads((case / "expected.json").read_text(encoding="utf-8"))["result"]
        print(f"added {case.relative_to(REPO)}: {r['state']} in {r['steps']} steps "
              f"(recorded {r['recorded_state']} in {r['recorded_steps']}), {len(r['divergences'])} divergences")
        return 0
    failures = check(args.update, args.cases or None)
    for name, diff in failures:
        print(f"CHANGED  {name}: {diff}")
    print(f"{len(failures)} case(s) changed" if failures else "all replay cases match")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
