"""Run the bench diag suite on the real desktop through harness 0.5 (the runtime + `deskmind_hands.executor`), and
grade with deskmind-bench's own grader — the same tasks, fixtures and checks `hands do` is measured on.

    uv run --extra macos python -m tools.harness_diag [--only G07,G08] [--config yy-luna] [--out runs/harness-diag]

Per task: unpack the fixture into a fresh sandbox, run its stage commands (documents opened in the background), start
`app-run` with `driver: hands` and the goal, answer its cards (approvals approved; questions answered from the task's
user_script; a cancel injection becomes `{"stop": true}` before the N-th step), then grade the folder. Process
checkpoints read the run: `state` (cancelled / succeeded / …) and `metrics.dialogue_turns` (questions asked).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from deskmind_bench.graders.primitives import GradeContext
from deskmind_bench.graders.score import grade
from deskmind_bench.scoring import pristine_sentinels
from deskmind_bench.task import load_task

from deskmind_hands.env.workspace import Workspace

REPO = Path(__file__).resolve().parents[1]
BENCH = REPO.parent / "bench"
HARNESS = REPO.parent / "harness"
FIXTURES = BENCH / "fixtures"


def reset_apps(bundles: list[str]) -> None:
    for b in bundles:
        name = {"com.apple.textedit": "TextEdit", "com.apple.finder": "Finder", "com.apple.safari": "Safari"}.get(b.lower())
        if name == "TextEdit":
            subprocess.run(["osascript", "-e", 'tell application "TextEdit" to close every window saving no'], capture_output=True, timeout=20)
        elif name == "Safari":
            subprocess.run(["osascript", "-e", 'tell application "Safari" to close (every document whose URL contains "/ws/")'], capture_output=True, timeout=20)


def stage(commands: list[str], ws: Path) -> None:
    for cmd in commands:
        cmd = re.sub(r"(^|[;&|]\s*)open\s+(?!-g)", r"\1open -g ", cmd)
        subprocess.run(["/bin/sh", "-c", cmd], cwd=str(ws), capture_output=True, timeout=30, env={**os.environ, "WS": str(ws)})
        time.sleep(1.5)


def run_one(task, out: Path, config: str, verbose: bool) -> dict:
    rd = out / task.id
    ws = Workspace.create(rd, FIXTURES)
    ws.reset(task.fixture)
    reset_apps(task.reset_apps)
    stage(task.stage, ws.ws)
    req = {"raw": task.goal.strip(), "scope": str(ws.ws), "config": config, "driver": "hands", "apps": [task.app] if task.app else []}
    env = {**os.environ, "DESKMIND_HANDS_DIR": str(REPO)}
    p = subprocess.Popen(["npx", "tsx", "src/cli/app.ts", "app-run", "--runs", str(rd / "app")], cwd=str(HARNESS), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, env=env)
    p.stdin.write(json.dumps(req, ensure_ascii=False) + "\n"); p.stdin.flush()
    cancel_at = next((i.at_action for i in task.inject if i.event == "cancel"), None)
    steps = 0; questions = 0; cards: list[str] = []; done: dict | None = None; t0 = time.time()
    for line in p.stdout:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        k = e.get("kind")
        if verbose and k in ("plan", "step", "ask", "consent", "refused", "task_done"):
            print("  ", json.dumps({x: y for x, y in e.items() if x not in ("seq", "t", "run_id")}, ensure_ascii=False)[:200])
        if k == "step":
            steps += 1
            if cancel_at is not None and steps >= cancel_at - 1 and p.stdin:
                p.stdin.write(json.dumps({"stop": True}) + "\n"); p.stdin.flush(); cancel_at = None
        elif k == "ask":
            card = e["card"]; cards.append(card["kind"])
            opts = card["options"]
            if card["kind"] in ("missing", "ambiguity"):
                questions += 1
                reply = next((u.reply for u in task.user_script if re.search(u.match, card["text"])), None)
                ans = {"ask_id": e["ask_id"], "text": reply} if reply else {"ask_id": e["ask_id"], "option": next((i for i, o in enumerate(opts) if o["kind"] == "delegate"), 0)}
            else:
                pick = next((i for i, o in enumerate(opts) if o["kind"] == "approve"), 0)
                ans = {"ask_id": e["ask_id"], "option": pick}
            p.stdin.write(json.dumps(ans, ensure_ascii=False) + "\n"); p.stdin.flush()
        elif k == "task_done":
            done = e
            break
        if time.time() - t0 > task.budget.wall_clock_s:
            p.stdin.write(json.dumps({"stop": True}) + "\n"); p.stdin.flush()
    try:
        p.wait(timeout=60)
    except subprocess.TimeoutExpired:
        p.kill()
    status = (done or {}).get("status", "errored")
    state = {"succeeded": "succeeded", "cancelled": "cancelled", "stopped": "cancelled", "unconfirmed": "failed", "failed": "failed", "environment": "errored"}.get(status, "errored")
    ctx = GradeContext(workspace=ws.ws, vars=dict(task.vars), run={"state": state, "metrics": {"dialogue_turns": questions, "actions": steps}})
    g = grade(task, ctx, sentinel_digests=pristine_sentinels(task, FIXTURES))
    row = {"task": task.id, "status": status, "state": state, "strict": g.strict, "partial": round(g.partial, 2), "violations": list(g.violations), "steps": steps,
           "questions": questions, "cards": cards, "seconds": round(time.time() - t0, 1), "reason": (done or {}).get("reason", "")[:160], "summary": (done or {}).get("summary", "")[:160],
           "checks": {k: v.ok for k, v in g.checkpoints.items()}}
    (rd / "result.json").write_text(json.dumps(row, ensure_ascii=False, indent=1))
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--config", default="yy-luna")
    ap.add_argument("--out", default="runs/harness-diag")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    out = REPO / a.out
    out.mkdir(parents=True, exist_ok=True)
    only = set(a.only.split(",")) if a.only else None
    rows = []
    for f in sorted((BENCH / "tasks" / "diag").glob("*.yaml")):
        task = load_task(f)
        if only and task.id not in only and task.id.split("-")[0] not in only:
            continue
        print(f"=== {task.id}: {task.goal.strip()[:80]}")
        row = run_one(task, out, a.config, a.v)
        rows.append(row)
        print(f"{'✓' if row['strict'] else '✗'} {task.id} {row['status']} partial={row['partial']} violations={row['violations']} steps={row['steps']} q={row['questions']} {row['seconds']}s | {row['reason'][:80]}")
        with (out / "results.jsonl").open("a") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    n = len(rows)
    print(f"\nstrict {sum(1 for r in rows if r['strict'])}/{n}; partial mean {sum(r['partial'] for r in rows) / max(1, n):.2f}; violations {sum(1 for r in rows if r['violations'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
