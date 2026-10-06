"""The harness's replay snapshots (layer 2 of docs/harness-eval.md): what the planner is shown, pinned per step.

Each case under tests/replay/<name>/ is a recorded `hands do` run (manifest.json + trace.jsonl, no screenshots) and
the expected replay (expected.json): the outcome, any divergences, and every request the adapter built, normalized.
A change to the harness that alters what the planner is shown fails `check` with the first differing request; if
the change is meant, `--update` rewrites the expectations and the diff goes to review -- and to whoever trains the
planner, since it is a change to what the model sees.

    python tools/harness_replay.py add runs/do-20260930-172539 --name d4-real-ask
    python tools/harness_replay.py check              # exit 1 on any difference
    python tools/harness_replay.py check --update     # accept the current output
    python tools/harness_replay.py coverage           # what the cases reach, and what none does

Every run recorded on this machine is a check too (`hands do`, bench and gym runs alike; they stay here, never in the
repository): dump them before a change that should leave what the planner sees alone, and compare after it.

    python tools/harness_replay.py corpus runs/ --out /tmp/before.json
    python tools/harness_replay.py corpus runs/ --against /tmp/before.json   # exit 1 on any difference
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
            "HANDS_DONE_CHECK", "HANDS_COMPLETION_CHECK")


def expected_of(case: Path) -> dict:
    env = json.loads((case / "env.json").read_text(encoding="utf-8")) if (case / "env.json").exists() else {}
    # No HANDS_* setting of the caller's reaches the replay, listed or not: HANDS_COMPLETION_CHECK, set in a shell,
    # added a question to every request after the first, and a snapshot passed or failed by whose machine it ran on.
    # (A switch read when a module is imported is out of reach here; none of those changes a request today.)
    outside = {k: v for k, v in os.environ.items() if not k.startswith("HANDS_")}
    with mock.patch.dict(os.environ, outside | {k: "0" for k in SWITCHES} | env, clear=True):
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
    (case / "expected.json").write_text(json.dumps(exp, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return case


def first_difference(a, b, path: str = "") -> str | None:
    """Where two JSON values first differ, as a path and the two values. Objects differ in key order too: the order
    of a question's options sets the letters the planner answers with."""
    if type(a) is not type(b):
        return f"{path or '/'}: {json.dumps(a, ensure_ascii=False)[:160]} != {json.dumps(b, ensure_ascii=False)[:160]}"
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f"{path}/{k}: {'missing now' if k not in b else 'new'}"
            d = first_difference(a[k], b[k], f"{path}/{k}")
            if d:
                return d
        i = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), None)
        return None if i is None else f"{path or '/'}: key {i + 1} is {list(a)[i]!r} expected, {list(b)[i]!r} now"
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
        got = json.loads(json.dumps(expected_of(case), ensure_ascii=False))
        diff = first_difference(want, got)
        if diff and update:
            (case / "expected.json").write_text(json.dumps(got, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            print(f"updated  {case.name}: {diff}")
        elif diff:
            failures.append((case.name, diff))
    return failures


def coverage(only: list[str] | None = None) -> dict[str, dict[str, int]]:
    """What the cases put to the planner, from their snapshots: how many requests asked each question, and offered
    each operation. A path no case reaches is one a refactor can change unseen (deskmind#58, step 0)."""
    heads: dict[str, int] = {}
    ops: dict[str, int] = {}
    for case in sorted(p for p in CASES.iterdir() if (p / "expected.json").exists()):
        if only and case.name not in only:
            continue
        for req in json.loads((case / "expected.json").read_text(encoding="utf-8"))["requests"]:
            for key, q in req["questions"].items():
                heads[key] = heads.get(key, 0) + 1
                if key == "operation":
                    crit = q.get("criteria") or {}
                    for op in (crit if isinstance(crit, dict) else [c.get("key") for c in crit]):
                        ops[op] = ops.get(op, 0) + 1
    return {"questions": heads, "operations": ops}


def every_path() -> dict[str, set[str]]:
    """Every question and operation the adapter can put to the planner."""
    from deskmind_hands.adapters import systemone
    heads = {"operation", "goal_complete", *(h for hs in systemone.HEADS.values() for h in hs)}
    return {"questions": heads, "operations": set(systemone.OPERATION_LABELS) | set(systemone.HEADS)}


def gaps(only: list[str] | None = None) -> dict[str, list[str]]:
    seen, every = coverage(only), every_path()
    return {k: sorted(every[k] - set(seen[k])) for k in every}


def corpus(runs: list[Path], env: dict[str, str] | None = None) -> dict[str, dict]:
    """Every run under `runs` that can be replayed, replayed: its requests, normalized, and its outcome. Not a
    snapshot -- recorded runs from the desktop stay on the machine that made them -- but the same comparison over many
    more runs: dumped before a change and after it, two dumps that differ name a run whose requests the change moved
    (deskmind#58: a step that only moves code must leave every one of them as it was)."""
    out = {}
    for run in sorted({p.parent for r in runs for p in Path(r).rglob("trace.jsonl")}):
        try:
            outside = {k: v for k, v in os.environ.items() if not k.startswith("HANDS_")}
            with mock.patch.dict(os.environ, outside | {k: "0" for k in SWITCHES} | (env or {}), clear=True):
                r = replay(run)
        except Exception as exc:  # noqa: BLE001 - a run that cannot be replayed is listed, not fatal
            out[str(run)] = {"skipped": f"{type(exc).__name__}: {exc}"[:200]}
            continue
        # The replay's scratch folder is new every time, and the grade's words name it.
        result = json.loads(re.sub(r"[^\"' ]*hands-replay-[^/\"' ]+", "<scratch>", json.dumps(r.to_json())))
        out[str(run)] = {"result": result, "requests": [normalize(q) for q in r.requests]}
    return out


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
    sub.add_parser("coverage", help="which questions and operations the cases reach, and which none does")
    k = sub.add_parser("corpus", help="replay every recorded run under the given folders; dump, or compare to a dump")
    k.add_argument("runs", nargs="+", type=Path)
    k.add_argument("--out", type=Path, help="write the dump here")
    k.add_argument("--against", type=Path, help="compare with an earlier dump; exit 1 on any difference")
    args = ap.parse_args()
    if args.cmd == "corpus":
        got = json.loads(json.dumps(corpus(args.runs), ensure_ascii=False))
        if args.out:
            args.out.write_text(json.dumps(got, ensure_ascii=False) + "\n", encoding="utf-8")
        replayed = [k for k, v in got.items() if "requests" in v]
        print(f"{len(replayed)} run(s) replayed, {sum(len(got[k]['requests']) for k in replayed)} requests; "
              f"{len(got) - len(replayed)} skipped")
        if args.against:
            want = json.loads(args.against.read_text(encoding="utf-8"))
            diffs = [(k, first_difference(want.get(k), got.get(k))) for k in sorted(set(want) | set(got))]
            diffs = [(k, d) for k, d in diffs if d]
            for k, d in diffs[:20]:
                print(f"CHANGED  {k}: {d}")
            print(f"{len(diffs)} run(s) changed" if diffs else "every run replays to the same requests")
            return 1 if diffs else 0
        return 0
    if args.cmd == "coverage":
        seen = coverage()
        for kind, counts in seen.items():
            print(f"{kind}: " + ", ".join(f"{k} {n}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1])))
        for kind, missing in gaps().items():
            print(f"no case reaches these {kind}: {', '.join(missing) or 'none'}")
        return 0
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
