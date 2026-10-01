"""Replay the arm-C completion check at other thresholds, offline, from runs that logged P(goal complete) every step.

For each run and threshold t (>= the threshold the run was made with), the check would have stopped at the first
step whose P(yes) >= t. That is exact up to the step where the run actually stopped; a run the check ended at a
P(yes) below t would have gone on, and what it did then was never observed -- it is reported as censored, never
guessed.

Also the ranking-vs-calibration split: P(yes) at the step the goal was first met, against the highest P(yes) on
any step before it. Overlap means no threshold works (the question ranks badly); separation means one does.

    python tools/replay_check.py reports/bench/diag-armC-*.json
"""

from __future__ import annotations

import json
import sys

THRESHOLDS = (0.7, 0.8, 0.9, 0.95)


def p_of(row):
    return ((row.get("decision") or {}).get("p_complete"))


def replay(run: dict, t: float) -> dict:
    log = run["log"]
    # goal_met on step n is graded after step n executed; the P(yes) logged on step n was asked about the state
    # before it, i.e. after step n-1. So a stop at step n is right iff step n-1 left the goal met.
    met_after = {r["n"]: bool(r.get("goal_met")) for r in log if r.get("goal_met") is not None}
    first_met = min((n for n, m in met_after.items() if m), default=None)
    model_done = next((r["n"] for r in log if r.get("op") == "done"
                       and (r.get("decision") or {}).get("stop_by") == "model"), None)
    check_stop = next((r["n"] for r in log if p_of(r) is not None and p_of(r) >= t), None)
    stop = min((x for x in (check_stop, model_done) if x is not None), default=None)
    ended_by_check = run["done_timing"].get("stop_by") == "check"
    censored = stop is None and ended_by_check        # the run ended on a lower threshold; this one never fired
    right = stop is not None and met_after.get(stop - 1, False)
    actual_end = log[-1]["n"] if log else 0
    return {
        "censored": censored,
        "reached": first_met is not None and (stop is None or first_met < stop) and not censored,
        "done_after_goal": right,
        "false_done": stop is not None and not right,
        "wasted": (max(0, (stop if stop else actual_end + 1) - first_met - 1)
                   if first_met is not None and (stop is None or first_met < stop) else 0),
    }


def split(run: dict):
    log = run["log"]
    first_met = next((r["n"] for r in log if r.get("goal_met")), None)
    if first_met is None:
        return None
    at = next((p_of(r) for r in log if r["n"] == first_met + 1 and p_of(r) is not None), None)
    before = [p_of(r) for r in log if r["n"] <= first_met and p_of(r) is not None]
    return {"task": run["task_id"], "p_at_goal": at, "max_p_before": max(before) if before else None}


def main(paths):
    for path in paths:
        d = json.load(open(path))
        runs = [r for r in d["runs"] if not r.get("unavailable")]
        print(f"\n== {d['label']}  ({path.split('/')[-1]}, check={d.get('completion_check')}, n={len(runs)})")
        print(f"   {'t':>5} {'DONE after goal':>16} {'false DONE':>11} {'wasted':>7} {'censored':>9}")
        for t in THRESHOLDS:
            if d.get("completion_check") and t < d["completion_check"]:
                continue
            rs = [replay(r, t) for r in runs]
            reached = [x for x in rs if x["reached"]]
            print(f"   {t:>5} {sum(x['done_after_goal'] for x in reached):>9}/{len(reached):<6} "
                  f"{sum(x['false_done'] for x in rs):>11} {sum(x['wasted'] for x in rs):>7} "
                  f"{sum(x['censored'] for x in rs):>9}")
        sp = [s for s in (split(r) for r in runs) if s]
        print("   ranking vs calibration (goal-reached runs):")
        for s in sp:
            print(f"     {s['task']:26s} P(yes) at goal {s['p_at_goal']}  max before {s['max_p_before']}")
        befores = [p_of(r) for r in [x for rr in runs for x in rr["log"]] if p_of(r) is not None]
        if befores:
            print(f"   all logged P(yes): n={len(befores)} min={min(befores):.2f} max={max(befores):.2f}")


if __name__ == "__main__":
    main(sys.argv[1:])
