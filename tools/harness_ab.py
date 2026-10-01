"""Layer 4 of docs/harness-eval.md: the same planner, the old and the new harness, compared task by task.

Each input is a JSONL file of runs, one line per run: {"task": ..., "passed": bool, "cause": optional}. These are
the lines `tools/gym/run.py --summary` writes, and a trial script can write the same shape. Give each file a
variant name: base=old.jsonl new=new.jsonl. The verdict applies the gate:

- no task has fewer passes under `new` than under `base`;
- the total pass rate does not drop;
- every task has the same number of runs under both. Otherwise there is no headline: an incomplete pair is how
  noise is passed off as a result.

    python tools/harness_ab.py base=runs/ab/base.jsonl new=runs/ab/new.jsonl
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def load(path: Path) -> dict[str, list[dict]]:
    by_task: dict[str, list[dict]] = defaultdict(list)
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            by_task[r["task"]].append(r)
    return by_task


def compare(base: dict[str, list[dict]], new: dict[str, list[dict]]) -> dict:
    tasks = sorted(set(base) | set(new))
    rows, worse, better, incomplete = [], [], [], []
    for t in tasks:
        b, n = base.get(t, []), new.get(t, [])
        pb, pn = sum(bool(r.get("passed")) for r in b), sum(bool(r.get("passed")) for r in n)
        if len(b) != len(n) or not b:
            incomplete.append(t)
        elif pn < pb:
            worse.append(t)
        elif pn > pb:
            better.append(t)
        rows.append({"task": t, "base": f"{pb}/{len(b)}", "new": f"{pn}/{len(n)}",
                     "new_failures": dict(Counter(r.get("cause") or "unattributed" for r in n if not r.get("passed")))})
    tb = sum(sum(bool(r.get("passed")) for r in v) for v in base.values())
    tn = sum(sum(bool(r.get("passed")) for r in v) for v in new.values())
    nb, nn = sum(len(v) for v in base.values()), sum(len(v) for v in new.values())
    if incomplete:
        verdict = "no headline: incomplete pairs"
    elif worse or tn < tb:
        verdict = "worse"
    elif better or tn > tb:
        verdict = "better"
    else:
        verdict = "no change"
    return {"verdict": verdict, "total": {"base": f"{tb}/{nb}", "new": f"{tn}/{nn}"}, "worse": worse,
            "better": better, "incomplete": incomplete, "tasks": rows}


def main(argv: list[str]) -> int:
    named = dict(a.split("=", 1) for a in argv)
    if set(named) != {"base", "new"}:
        print(__doc__)
        return 2
    res = compare(load(Path(named["base"])), load(Path(named["new"])))
    for r in res["tasks"]:
        mark = "WORSE" if r["task"] in res["worse"] else "better" if r["task"] in res["better"] else \
            "incomplete" if r["task"] in res["incomplete"] else ""
        print(f"{r['task']:<40} {r['base']:>7} -> {r['new']:<7} {mark:<10} {r['new_failures'] or ''}")
    print(f"\ntotal {res['total']['base']} -> {res['total']['new']}   verdict: {res['verdict']}")
    return 0 if res["verdict"] in ("better", "no change") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
