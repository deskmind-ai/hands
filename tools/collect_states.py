"""Collect every state a System One planner visits on a task set, for distillation.

One JSONL row per model call: the state and questions exactly as sent, the planner's answers (every option's
probability), what the harness then did and what the environment said, the run's final grade, and which split the
task belongs to. Rows from a student run are labelled afterwards, e.g. by tools/oracle_label.py.

    HANDS_PEEKABOO_TRANSPORT=mcp python tools/collect_states.py --set train --driver peekaboo \\
        --model brain-0.8b --url http://127.0.0.1:8793 --role student --out data/student_train.jsonl

Rows are appended as each run finishes, so a long collection that dies (an endpoint outage, a locked screen)
keeps what it had; --skip-done resumes by skipping tasks already in the output.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from deskmind_hands.adapters import systemone as S          # noqa: E402
from deskmind_bench.task import load_set                    # noqa: E402
from deskmind_bench.bench import SUITE_VERSION     # noqa: E402
from deskmind_hands.cli import TASKS_DIR            # noqa: E402  (DESKMIND_TASKS_DIR, else tasks/ here)


def _digest_changes(entries: list[dict]) -> dict:
    """step n -> did the observation after it differ from the one before it."""
    out = {}
    for pos, e in enumerate(entries):
        if e["t"] != "step":
            continue
        prev = next((x for x in reversed(entries[:pos]) if x["t"] == "obs"), None)
        nxt = next((x for x in entries[pos + 1:] if x["t"] == "obs"), None)
        out[e.get("n")] = None if not (prev and nxt) else prev.get("digest") != nxt.get("digest")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", required=True, dest="task_set")
    ap.add_argument("--driver", default="mock")
    ap.add_argument("--model", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--role", choices=["teacher", "student"], required=True)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--tasks", default="", help="comma-separated task ids (default: the whole set)")
    ap.add_argument("--skip-done", action="store_true")
    ap.add_argument("--out", required=True)
    ap.add_argument("--beta", type=float, default=0.0,
                    help="DAgger: probability the oracle's answer (tools/oracle_label.py, computed online) is the one "
                         "carried out; every state is labelled by the oracle either way")
    ap.add_argument("--trap", type=float, default=0.0,
                    help="share of runs in which, once, the file a move_by_ref goal names as the place is moved "
                         "instead (the step G10 got wrong); the oracle labels undoing it")
    args = ap.parse_args()
    import random
    from oracle_label import label_row                   # noqa: E402
    rng = random.Random(12345)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tasks = {t.id: t for t in load_set(TASKS_DIR, args.task_set)}
    todo = [t for t in (args.tasks.split(",") if args.tasks else tasks)]
    if args.skip_done and out.exists():
        done = {json.loads(l)["task_id"] for l in out.open()}
        todo = [t for t in todo if t not in done]
    print(f"{len(todo)} tasks to run", flush=True)

    calls: list[dict] = []
    original = S.SystemOneAdapter._ask

    current: dict = {}

    def trap_for(task, state, questions):
        """Move the reference file (the one the goal says must stay) to another folder: SELECT on its dropdown."""
        ref = next((Path(c.check["file_exists"]["path"]).name for c in task.checkpoints
                    if c.name == "reference_stayed"), None)
        if ref is None or "SELECT" not in questions["operation"]["criteria"]:
            return None
        dd = next((e for e in state.get("elements") or [] if e.get("id") == f"syn:move:{ref}"), None)
        opts = (dd or {}).get("options") or []
        if not opts or "select_target" not in questions:
            return None
        o = opts[-1]
        if o["index"] not in questions["select_target"]["criteria"]:
            return None
        hot = lambda q, k: {"type": "choice", "choice": k, "confidence": 1.0,
                            "probabilities": {c: (1.0 if c == k else 0.0) for c in q["criteria"]}}
        return {"operation": hot(questions["operation"], "SELECT"), "select_target": hot(questions["select_target"],
                                                                                         o["index"])}

    def recording_ask(self, state, questions):
        answers = original(self, state, questions)
        task = current["task"]
        label, source = (label_row({"state": state, "questions": questions}, task) if (args.beta or args.trap)
                         else (None, None))
        trap = trap_for(task, state, questions) if current.get("trap_armed") else None
        if trap is not None:
            current["trap_armed"] = False
        by_oracle = trap is None and label is not None and source == "oracle" and rng.random() < args.beta
        calls.append({"state": state, "questions": questions, "answers": answers, "label": label,
                      "label_source": source,
                      "executed_by": "trap" if trap else "oracle" if by_oracle else "student"})
        return trap if trap else label if by_oracle else answers

    S.SystemOneAdapter._ask = recording_ask
    from deskmind_hands.cli import main as cli_main                  # noqa: E402  (import after the patch)

    report = Path(tempfile.mkstemp(suffix=".json", prefix="collect-")[1])
    for tid in todo:
        calls.clear()
        current["task"] = tasks[tid]
        current["trap_armed"] = random.Random(f"trap-{tid}").random() < args.trap
        report.write_text("")
        sys.argv = ["hands", "run", "--set", args.task_set, "--task", tid, "--driver", args.driver,
                    "--adapter", "systemone", "--model", args.model, "--systemone-url", args.url,
                    "--repeats", str(args.repeats), "--report", str(report)]
        try:
            cli_main()
        except SystemExit:
            pass
        except Exception as exc:                            # noqa: BLE001 - one bad task must not end the run
            print(f"{tid}: {type(exc).__name__}: {exc}", flush=True)
            continue
        # From this run's own report, never "the newest directory under runs/": another session benchmarking on the
        # same machine made that someone else's run, and four tasks' states were silently dropped.
        try:
            runs_root = Path(json.loads(report.read_text())["runs_dir"])
        except (ValueError, KeyError):
            print(f"{tid}: no report written", flush=True)
            continue
        i = 0
        with out.open("a") as fh:
            for run_dir in sorted(runs_root.glob(f"{tid}-r*")):
                trace = run_dir / "trace.jsonl"
                if not trace.exists():
                    continue
                entries = [json.loads(l) for l in trace.open()]
                steps = [e for e in entries if e["t"] == "step"]
                changed = _digest_changes(entries)
                result = {}
                if (run_dir / "run.json").exists():
                    r = json.loads((run_dir / "run.json").read_text())
                    result = {"strict": (r.get("grade") or {}).get("strict"),
                              "partial": (r.get("grade") or {}).get("partial"),
                              "failure": (r.get("failure") or {}).get("class")}
                for n, step in enumerate(steps):
                    if i >= len(calls):
                        break
                    call = calls[i]
                    i += 1
                    task = tasks[tid]
                    fh.write(json.dumps({
                        "task_id": tid, "run_id": run_dir.name, "step": n + 1, "set": args.task_set,
                        "family": next((x for x in task.tags if x not in ("train", "holdout", "finder", "textedit")),
                                       None),
                        "goal": task.goal, "model": args.model, "role": args.role,
                        "suite_version": SUITE_VERSION,
                        "state": call["state"], "questions": call["questions"],
                        "answers": call["answers"],
                        "teacher": call["answers"] if args.role == "teacher" else None,
                        **({"label": call["label"], "label_source": call["label_source"],
                            "executed_by": call["executed_by"], "beta": args.beta} if (args.beta or args.trap) else {}),
                        "executed": {"describe": step.get("describe") or step.get("kind"),
                                     "ok": step.get("ok"),
                                     "error": step.get("detail") if step.get("ok") is False else None,
                                     "parse_error": step.get("parse_error"),
                                     "page_changed": changed.get(step.get("n"))},
                        "run_result": result,
                    }, ensure_ascii=False) + "\n")
        print(f"{tid}: {i} states, {result}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
