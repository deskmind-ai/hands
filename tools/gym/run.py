"""Run a planner on gym tasks and write DAgger rows: every request it answered, labelled by the app's oracle.

    HANDS_PEEKABOO_TRANSPORT=mcp python -m tools.gym.run --app music --seeds 1-20 \\
        --url http://127.0.0.1:8793 --model brain-4b --out data/gym/music.rows.jsonl

One row per model call, in the shape tools/collect_states.py writes (state, questions, answers as sent), plus
`label` -- one-hot per head, keyed by that request's own option keys -- from the oracle, which reads the app's true
state at that moment. Rows the oracle cannot label (the right option was not offered) keep label None with the
reason in `note`. The app runs in its own chromeless window (tools/gym/host), behind whatever else is on screen; no
real app is touched.
"""
from __future__ import annotations

import argparse
import importlib
import json
import random
import re
import shutil
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from deskmind_hands.adapters import systemone as S          # noqa: E402
from tools.gym import server as gym_server         # noqa: E402
from tools.gym.server import serve                 # noqa: E402

#: The runs hands writes (see deskmind_hands.cli.RUNS_DIR): the answer and the approvals are read back from there.
RUNS = Path(os.environ.get("DESKMIND_RUNS_DIR") or REPO / "runs")
HOST_APP = REPO / "tools" / "gym" / "host" / "build" / "GymHost.app"
HOST_BUNDLE = "ai.deskmind.gymhost"

ORACLE_VERSION = 1


def _seeds(spec: str) -> list[int]:
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b or a) + 1))
    return out


def _state(port: int, run: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/state?run={run}", timeout=5) as r:
        return json.load(r)


def _states(port: int, run: str, task: dict) -> dict:
    """The page's state, or for a task with several pages each page's state by name."""
    if "pages" not in task:
        return _state(port, run)
    return {name: _state(port, f"{run}-{name}") for name in task["pages"]}


def _answer(since: float) -> str | None:
    """What the run answered, for a question goal: the "answer: ..." its last step carried (see ANSWER)."""
    runs = [d for d in RUNS.glob("do-*") if d.stat().st_mtime >= since - 1]
    if not runs:
        return None
    trace = max(runs, key=lambda d: d.stat().st_mtime) / "trace.jsonl"
    answer = None
    for line in trace.read_text().splitlines() if trace.exists() else []:
        step = json.loads(line)
        text = str(step.get("text") or (step.get("action") or {}).get("text") or "")
        if text.startswith("answer: "):
            answer = text[len("answer: "):]
    return answer


def _apps_seen(since: float) -> list[str]:
    """Every app the run observed as frontmost (its trace's observations): the out-of-scope check."""
    runs = [d for d in RUNS.glob("do-*") if d.stat().st_mtime >= since - 1]
    if not runs:
        return []
    trace = max(runs, key=lambda d: d.stat().st_mtime) / "trace.jsonl"
    seen = []
    for line in trace.read_text().splitlines() if trace.exists() else []:
        app = json.loads(line).get("focused_app")
        if app and app not in seen:
            seen.append(app)
    return seen


def host_args(urls: list[str], env=os.environ) -> list[str]:
    """GymHost's arguments: the pages, and with HANDS_GYM_DISPLAY=<display id> the display its windows open on
    (tools/native/vdisplay makes a virtual one), so a collection run does not use the user's screen."""
    shown = ["--display", env["HANDS_GYM_DISPLAY"]] if env.get("HANDS_GYM_DISPLAY", "").strip().isdigit() else []
    return [*shown, *urls]


def _open_host(url: str | list[str], timeout: float = 15.0) -> int:
    """Open the page in GymHost and wake its accessibility tree; returns how many elements its window has.

    WebKit builds the page's tree only once an assistive client has walked into it: until then Peekaboo sees the
    window's frame and an empty group (and hands falls back to screenshots). One walk of the window is enough, and
    the tree then follows the page as it changes. Only the window is walked -- the menu bar lists recent items."""
    from ApplicationServices import AXUIElementCopyAttributeValue, AXUIElementCreateApplication

    def get(e, attr):
        err, v = AXUIElementCopyAttributeValue(e, attr, None)
        return v if err == 0 else None

    def count(e) -> int:
        return 1 + sum(count(c) for c in get(e, "AXChildren") or [])

    subprocess.run(["pkill", "-f", "GymHost.app/Contents/MacOS/GymHost"], capture_output=True)
    urls = [url] if isinstance(url, str) else list(url)
    subprocess.run(["/usr/bin/open", "-g", "-n", str(HOST_APP), "--args", *host_args(urls)], capture_output=True,
                   timeout=20)
    end, n = time.time() + timeout, 0
    while time.time() < end:
        time.sleep(1.0)
        pids = subprocess.run(["pgrep", "-f", "GymHost.app/Contents/MacOS/GymHost"],
                              capture_output=True, text=True).stdout.split()
        if not pids:
            continue
        app = AXUIElementCreateApplication(int(pids[0]))
        n = sum(count(w) for w in get(app, "AXWindows") or [])
        if n >= 20:   # the frame alone is under 10
            break
    return n




def _waited() -> float:
    """Seconds the running hands driver has waited on the user so far (drivers.peekaboo.WAITED_S)."""
    from deskmind_hands.drivers import peekaboo
    return round(peekaboo.WAITED_S, 1)

def _user_wait(started: float) -> float | None:
    """The user_wait_s of the hands run that started after `started` (its run.json), or None."""
    runs = [d for d in RUNS.glob("do-*") if d.is_dir() and d.stat().st_mtime >= started - 1]
    for d in sorted(runs, key=lambda d: d.stat().st_mtime, reverse=True):
        try:
            return round(float(json.loads((d / "run.json").read_text(encoding="utf-8"))["metrics"]["user_wait_s"]), 1)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None

def _stage_lessons(a, seed: int, run: str) -> tuple[str, list[str]]:
    """Pinned lessons for this run (--lessons): ("off", []), or ("relevant" | "irrelevant", their texts). Drawn by seed,
    like the vision share, so a rerun shows the same ones. Each run gets its own lessons store."""
    os.environ["HANDS_LESSONS"] = "0"
    if a.lessons <= 0:
        return "off", []
    r = random.Random(f"lessons-{seed}")
    if r.random() >= a.lessons:
        return "off", []
    if not a.lessons_pool:
        raise SystemExit("--lessons needs --lessons-pool")
    pool = json.loads(a.lessons_pool.read_text(encoding="utf-8"))
    mode = "relevant" if r.random() < 0.5 else "irrelevant"
    if mode == "relevant":
        own = pool.get(a.app) or {}
        texts = list(own.get("relevant") or []) + list(own.get("general") or [])
    else:
        # A family's "general" lessons hold for any task ("change only what the goal names"): taught as lessons to
        # ignore, they would teach ignoring sound advice, so they are never another family's irrelevant ones.
        texts = [t for fam, p in pool.items() if fam not in (a.app, "*") for t in (p.get("relevant") or [])]
        texts += list((pool.get("*") or {}).get("irrelevant") or [])
    if not texts:
        return "off", []
    chosen = r.sample(texts, min(len(texts), 1 + (r.random() < 0.5)))
    from deskmind_hands import lessons as lessons_mod
    d = RUNS / "gym-lessons" / run
    shutil.rmtree(d, ignore_errors=True)
    for t in chosen:
        lessons_mod.save(lessons_mod.Lesson(text=t, pinned=True, source={"by": "gym", "runs": [run]}), d)
    os.environ["HANDS_LESSONS"], os.environ["HANDS_LESSONS_DIR"] = "1", str(d)
    return mode, chosen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="music")
    ap.add_argument("--seeds", default="1-5")
    ap.add_argument("--url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--port", type=int, default=8977)
    ap.add_argument("--max-actions", type=int, default=12)
    ap.add_argument("--out", required=True)
    ap.add_argument("--beta", type=float, default=0.5, help="probability the oracle's action is executed (DAgger)")
    ap.add_argument("--oracle-only", action="store_true",
                    help="the harness's own test (docs/harness-eval.md, layer 3): no model is called, the oracle's "
                         "answer is carried out at every step, and a step it cannot answer (the right choice is not "
                         "among the options) is recorded as unreachable and ends the task. Every task should pass.")
    ap.add_argument("--foreground", action="store_true",
                    help="allow the brief foreground (needed to type into web pages); only when nobody uses the Mac")
    ap.add_argument("--approve", choices=("off", "auto", "deny"), default="off",
                    help="a stand-in for the person answering approvals (deskmind-hands do --ask stdin): approve every one, or "
                         "deny every one; what was asked goes into --summary")
    ap.add_argument("--trap", type=float, default=0.0,
                    help="share of runs in which one plausible wrong step (the app's trap()) is carried out")
    ap.add_argument("--summary", help="one JSON line per task: passed, the approvals asked, the app's final state")
    ap.add_argument("--vision", type=float, default=0.0,
                    help="share of runs (chosen by seed) that see and act through the screenshot, not the AX tree")
    ap.add_argument("--split", type=int, choices=(1, 2), default=1,
                    help="1: held out by skin only (every row before G19); 2: concept-disjoint -- held-out goal "
                         "templates and values never in training rows (tools/gym/split.py)")
    ap.add_argument("--grounder", default="http://127.0.0.1:18861/ground", help="the Eyes server, for vision runs")
    ap.add_argument("--lessons", type=float, default=0.0,
                    help="share of runs (chosen by seed) shown lessons (hands' HANDS_LESSONS), half of them lessons for "
                         "this family's tasks and half lessons that do not apply, so the planner learns to use the one "
                         "and ignore the other; needs --lessons-pool")
    ap.add_argument("--lessons-pool", type=Path,
                    help='JSON {"<family>": {"relevant": [...], "general": [...]}, "*": {"irrelevant": [...]}}: a '
                         "family's irrelevant lessons are the other families' relevant ones (never their general ones, "
                         "which hold for any task) and the shared irrelevant ones")
    a = ap.parse_args()
    import deskmind_hands.grounding as grounding_mod
    import deskmind_hands.vision as vision_mod
    vision_mod.GROUNDER_URL = grounding_mod.GROUNDER_URL = a.grounder
    # Web tables name their rows "row": name them by their cells, or no row can be told from another (hands' opt-in).
    os.environ["HANDS_NAME_ROWS"] = "1"
    # A web field takes an accessibility write without the page seeing it: paste it in the brief foreground instead.
    os.environ["HANDS_WEB_TYPE_FALLBACK"] = "1"
    # A switch clicked twice was flipped back, not ineffective: it stays on offer (hands' opt-in).
    os.environ["HANDS_TOGGLE_REPEAT_OK"] = "1"
    import random
    rng = random.Random(12345)
    gym_app = importlib.import_module(f"tools.gym.{a.app}")
    serve(a.port)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    current: dict = {}
    calls: list[dict] = []
    original = S.SystemOneAdapter._ask

    def recording_ask(self, state, questions):
        app_state = _states(a.port, current["run"], current["task"])
        request = {"state": state, "questions": questions}
        label, why = gym_app.oracle(current["task"], app_state, request)
        if a.oracle_only:
            # No model: the oracle drives. Where it has no answer among the options, the run is given up there, so
            # an unreachable step ends the task as a failure the report can point at.
            if label is None:
                current.setdefault("unreachable", []).append({"step": len(calls) + 1, "why": why})
                label = {"operation": {"type": "choice", "choice": "BLOCKED", "confidence": 1.0,
                                       "probabilities": {"BLOCKED": 1.0}}}
            calls.append({"state": state, "questions": questions, "answers": None, "label": label, "note": why,
                          "app_state": app_state, "executed_by": "oracle", "user_wait_s_before": _waited()})
            return label
        answers = original(self, state, questions)
        # DAgger's mixed driver: with probability beta the oracle's answer is the one carried out, so a run gets past
        # the step the student cannot do and later states (results, the right row, paused, done) are visited too.
        # Every state is labelled by the oracle either way.
        # A trap, once per run in --trap of the runs: a plausible wrong step (the live cut, the switch next to the
        # right one, the other email) carried out instead, so the states that look finished but are not -- where a
        # planner says DONE -- are visited and labelled with the fix. The app's trap() says what it would be here.
        trap = None
        if current.get("trap_armed") and hasattr(gym_app, "trap"):
            trap = gym_app.trap(current["task"], app_state, request)
            if trap is not None and not (len(trap) > 2 and trap[2]):   # a third element: more steps to come
                current["trap_armed"] = False
        by_oracle = trap is None and label is not None and rng.random() < a.beta
        calls.append({"user_wait_s_before": _waited(),
                      "state": state, "questions": questions, "answers": answers, "label": label, "note": why,
                      "app_state": app_state,
                      "executed_by": "trap" if trap is not None else "oracle" if by_oracle else "student",
                      **({"trap": trap[1]} if trap is not None else {})})
        return trap[0] if trap is not None else label if by_oracle else answers

    S.SystemOneAdapter._ask = recording_ask
    import deskmind_hands.cli as cli_mod
    from deskmind_hands.cli import main as cli_main              # noqa: E402  (after the patch)
    asked: list[dict] = []

    class StandInUser:
        """Answers the approvals a live run asks (hands' StdinUser), the same way every time, and keeps them."""

        def respond(self, question, approval, options=()):
            if not approval:   # a question, not an approval: the goal already says which one
                asked.append({"question": question, "approval": False})
                return ("用目标里说的那一个" if re.search(r"[\u4e00-\u9fff]", question) else "The one the goal names"), \
                    True, 0.0
            ok = a.approve == "auto"
            asked.append({"question": question, "approval": True, "approved": ok})
            return ("approved" if ok else "denied"), ok, 0.0

    if a.approve != "off":
        cli_mod.StdinUser = StandInUser

    passed = 0
    for seed in _seeds(a.seeds):
        task = gym_app.make_task(seed, a.split)
        run = f"{a.app}-{seed}-{int(time.time())}"
        current.update(task=task, run=run, trap_armed=random.Random(f"trap-{seed}").random() < a.trap,
                       unreachable=[])
        calls.clear()
        # A task with several apps has a page for each (task["pages"]: app page name -> page), each its own window of
        # the host and its own run id; one app's task has one page, named after it.
        pages = task.get("pages") or {a.app: task["page"]}
        urls = []
        for name, pg in pages.items():
            rid = run if "pages" not in task else f"{run}-{name}"
            gym_server.TASKS[rid] = pg
            urls.append(f"http://127.0.0.1:{a.port}/apps/{name}.html?run={rid}")
        url = urls if len(urls) > 1 else urls[0]
        # Its own chromeless window (tools/gym/host): in Safari the tree was mostly the browser and the user's own
        # sidebar, which ended up in rows.
        mode = "vision" if random.Random(f"vision-{seed}").random() < a.vision else "ax"
        lesson_mode, lesson_texts = _stage_lessons(a, seed, run)
        if _open_host(url) < 20:
            print(f"seed {seed}: the page's accessibility tree did not come up; skipped", flush=True)
            continue
        # A family with a real app beside the page (the expense receipt in Preview) stages it: its apps join
        # --apps, and the ones read by pixels join the vision apps.
        staged = gym_app.stage(task, RUNS / "gym-files", HOST_BUNDLE) if hasattr(gym_app, "stage") else {}
        os.environ["HANDS_VISION_APPS"] = ",".join(([HOST_BUNDLE] if mode == "vision" else []) + staged.get("vision", []))
        apps_arg = [f"{n}={HOST_BUNDLE}" for n in (task.get("apps") or [task["app"]])] + \
            [f"{n}={b}" for n, b in (staged.get("apps") or {}).items()]
        sys.argv = ["hands", "do", task["goal"], "--app", HOST_BUNDLE, "--apps", ",".join(apps_arg),
                    "--adapter", "systemone", "--systemone-url", a.url, "--model", a.model,
                    "--max-actions", str(a.max_actions), "--minutes", "6", "--yes", "--no-screenshots"]
        if a.foreground:
            sys.argv.append("--foreground-ok")   # web typing needs the brief foreground (see the module notes)
        if a.approve != "off":
            sys.argv += ["--ask", "stdin"]       # a live run: risky steps wait for approval (hands/runtime/risk.py)
        asked.clear()
        started = time.time()
        try:
            cli_main()
        except SystemExit:
            pass
        final = _states(a.port, run, task)
        answer = _answer(started)
        user_wait_s = _user_wait(started)
        ok = gym_app.passed(task, final, answer)
        if a.summary:
            with open(a.summary, "a") as fh:
                fh.write(json.dumps({"task": f"gym-{a.app}-{task['app'].lower()}-s{seed:04d}", "app": a.app,
                                     "seed": seed, "split": task["split"], "mode": mode, "goal": task["goal"],
                                     "kind": task.get("kind"), "passed": ok, "answer": answer,
                                     "split_version": task.get("split_version", 1),
                                     "template_id": task.get("template_id"),
                                     "approvals": list(asked), "final": final,
                                     "unreachable": list(current.get("unreachable") or []),
                                     "apps_seen": _apps_seen(started)}, ensure_ascii=False) + "\n")
        passed += ok
        with out.open("a") as fh:
            for n, c in enumerate(calls):
                fh.write(json.dumps({
                    "task": f"gym-{a.app}-{task['app'].lower()}-s{seed:04d}",
                    "app": a.app, "app_name": task["app"], "seed": seed, "split": task["split"], "mode": mode,
                    "executed_by": c["executed_by"], "beta": a.beta, "trap": c.get("trap"),
                    # Seconds this run had waited on the user before this step's request: a jump between two rows
                    # is a wait during which the app may have moved under the planner.
                    "user_wait_s_before": c.get("user_wait_s_before"),
                    "lang": task["lang"], "goal": task["goal"], "step": n + 1, "model": a.model,
                    "state": c["state"], "questions": c["questions"], "answers": c["answers"],
                    "label": c["label"], "label_source": "oracle" if c["label"] else None,
                    "oracle_version": getattr(gym_app, "ORACLE_VERSION", ORACLE_VERSION), "note": c["note"],
                    # How the family draws what is read from pixels (the expense receipt's font), when it says.
                    "render_version": getattr(gym_app, "RENDER_VERSION", None),
                    # Which optional parts of the planner state these rows were collected with (hands' switches):
                    # G19 mixes rows with and without them, and a row has to say which it is.
                    "state_switches": {k: os.environ.get(k, "0") == "1" for k in
                                       ("HANDS_EFFECT_NOTES", "HANDS_ENV_SECTIONS", "HANDS_MARK_NEW", "HANDS_PROGRESS",
                                        "HANDS_LESSONS", "HANDS_DONE_CHECK")},
                    # The split this task was made under, the goal template and what the goal asks for (with whether
                    # each is held out under split 2): so rows of either split can be cut template-disjoint.
                    "split_version": task.get("split_version", 1), "template_id": task.get("template_id"),
                    "template_heldout": task.get("template_heldout", False), "concepts": task.get("concepts", []),
                    # The lessons this run was shown (--lessons): none, ones for this family, or ones that do not apply.
                    "lessons": {"mode": lesson_mode, "texts": lesson_texts},
                    # Seconds this run spent waiting for the user at the Mac (not counted in its wall clock): rows
                    # from a long wait are rows where the app state may have moved under the planner.
                    "run_result": {"passed": ok, "user_wait_s": user_wait_s},
                }, ensure_ascii=False) + "\n")
        labelled = sum(1 for c in calls if c["label"])
        print(f"seed {seed} [{task['split']}, {mode}] {task['goal']!r}: {'PASS' if ok else 'FAIL'}  "
              f"{len(calls)} states, {labelled} labelled", flush=True)
        if hasattr(gym_app, "unstage"):
            gym_app.unstage(task)
        subprocess.run(["pkill", "-f", "GymHost.app/Contents/MacOS/GymHost"], capture_output=True)
    print(f"{passed}/{len(_seeds(a.seeds))} passed -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
