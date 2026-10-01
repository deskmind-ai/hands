"""Export the states a System One policy actually reaches, for labelling.

DeskMind Brain's training histories are written by a teacher from scratch, so they contain what a well-behaved agent
sees. The states its own policy gets stuck in are not in there, and they are the ones that decide whether a run
finishes: a command button pressed with nothing selected, a field written while another app is frontmost, an
action that worked and moved nothing. Those cannot be synthesized -- they only exist on policy.

One row per model call, in the shape DeskMind Brain trains on, with three things added:

  answered   what the policy chose, with the probability of every option it was offered
  executed   what the harness then did and what the environment said back -- ok, error, page_changed
  oracle     the task's own action sequence, when the task file carries one

The rows worth labelling are the ones where ``executed.ok`` is false or ``executed.page_changed`` is false:
there the policy's choice is known to be wrong, and a teacher only has to say what the right one was.

    python tools/export_onpolicy.py --model brain-0.8b --url http://127.0.0.1:8793 --out /tmp/onpolicy.jsonl

Nothing here touches the screen: the mock desktop is a real filesystem behind a simulated Finder, which is what
makes this cheap enough to run on every checkpoint.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskmind_hands.adapters import systemone as S           # noqa: E402
from deskmind_bench.task import load_set                     # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="smoke", dest="task_set")
    ap.add_argument("--model", default="brain-0.8b")
    ap.add_argument("--url", default="http://127.0.0.1:8793")
    ap.add_argument("--text-helper", default=None, dest="text_helper")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--out", default="onpolicy.jsonl")
    args = ap.parse_args()

    # One record per _ask, in call order. The loop's own trace supplies the outcome afterwards; aligning by order
    # is exact because the adapter asks once per proposal and the loop executes at most one action per proposal.
    calls: list[dict] = []
    original = S.SystemOneAdapter._ask

    def recording_ask(self, state, questions):
        answers = original(self, state, questions)
        calls.append({"state": state, "questions": questions, "answers": answers})
        return answers

    S.SystemOneAdapter._ask = recording_ask

    argv = ["hands", "run", "--set", args.task_set, "--adapter", "systemone", "--model", args.model,
            "--systemone-url", args.url, "--repeats", str(args.repeats)]
    if args.text_helper:
        argv += ["--text-helper", args.text_helper]
    sys.argv = argv
    from deskmind_hands.cli import main as cli_main               # noqa: E402  (import after the patch)
    cli_main()

    runs_root = max(Path("runs").iterdir(), key=lambda p: p.stat().st_mtime)
    oracles = {t.id: list(t.oracle or []) for t in load_set(REPO / "tasks", args.task_set)}

    rows, i = [], 0
    for run_dir in sorted(runs_root.iterdir()):
        trace = run_dir / "trace.jsonl"
        if not trace.exists():
            continue
        entries = [json.loads(l) for l in trace.open()]
        steps = [e for e in entries if e["t"] == "step"]
        # Whether a step moved anything is the digest of the observation before it against the one after -- not the
        # observation id, which is new every time, and not the driver's ok, which says only that it dispatched.
        digest_after: dict[int, bool | None] = {}
        for pos, e in enumerate(entries):
            if e["t"] != "step":
                continue
            prev = next((x for x in reversed(entries[:pos]) if x["t"] == "obs"), None)
            nxt = next((x for x in entries[pos + 1:] if x["t"] == "obs"), None)
            digest_after[e.get("n")] = (None if not (prev and nxt) else
                                        prev.get("digest") != nxt.get("digest"))
        task_id = run_dir.name.rsplit("-r", 1)[0]
        for n, step in enumerate(steps):
            if i >= len(calls):
                break
            call = calls[i]
            i += 1
            executed = {"kind": (step.get("action") or {}).get("kind"),
                        "describe": step.get("describe"),
                        "ok": step.get("ok"),
                        "error": step.get("detail") if step.get("ok") is False else None,
                        "parse_error": step.get("parse_error")}
            executed["page_changed"] = digest_after.get(step.get("n"))
            rows.append({"task_id": task_id, "step": n + 1, "model": args.model,
                         "goal": (call["state"].get("instructions") or {}).get("goal")
                                 or call["questions"].get("operation", {}).get("instructions", {}).get("goal"),
                         "state": call["state"], "questions": call["questions"],
                         "answered": call["answers"], "executed": executed,
                         "oracle": oracles.get(task_id, []),
                         "label_me": executed["ok"] is False or executed["page_changed"] is False})

    out = Path(args.out)
    with out.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    worth = sum(1 for r in rows if r["label_me"])
    print(f"\n{len(rows)} states -> {out}  ({worth} with a known-wrong choice, the ones worth labelling)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
