"""Summarize the newest (or a given) harness run the installed DeskMind helper recorded: outcome, cards asked,
stale refusals, steps, and the step timings -- the numbers an rc's on-device check reports.

    python3 scripts/rc_e2e/summarize.py [run dir]
"""
import glob
import json
import os
import statistics
import sys

runs = os.path.expanduser("~/Library/Application Support/DeskMind/work/harness-runs")
d = sys.argv[1] if len(sys.argv) > 1 else max(glob.glob(runs + "/run_*"), key=os.path.getmtime)
events = [json.loads(line) for line in open(os.path.join(d, "events.jsonl"), encoding="utf-8")]
asks = [e for e in events if e.get("kind") == "ask"]
stale = [e for e in events if e.get("kind") == "refused" and "stale" in json.dumps(e, ensure_ascii=False)]
other_refusals = [e for e in events if e.get("kind") == "refused" and e not in stale]
steps = [e for e in events if e.get("kind") == "step"]
done = next((e for e in events if e.get("kind") == "task_done"), {})
t = [e["t"] for e in events if isinstance(e.get("t"), (int, float))]
lat = [e["latency"] for e in steps if isinstance(e.get("latency"), (int, float)) and e["latency"] > 0]

print("run:", d)
print("outcome:", done.get("status"), "|", (done.get("reason") or "")[:160])
print("changes:", json.dumps(done.get("changes"), ensure_ascii=False))
print("duration_s:", round(max(t) - min(t), 1) if t else None)
print("steps:", len(steps))
print("cards:", len(asks), "by kind:", {k: sum(1 for a in asks if (a.get("card") or {}).get("kind") == k) for k in sorted({(a.get("card") or {}).get("kind") for a in asks})})
print("stale refusals:", len(stale))
for e in stale:
    print("   ", (e.get("why") or "")[:140])
print("other refusals:", len(other_refusals))
for e in other_refusals:
    print("   ", (e.get("why") or "")[:140])
if lat:
    print("step latency ms: median", round(statistics.median(lat)), "max", round(max(lat)), "n", len(lat))
