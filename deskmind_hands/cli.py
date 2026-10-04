"""Hands - a macOS computer-use agent, and the evals it is built against.

  deskmind-hands doctor                 what is actually available on this machine
  deskmind-hands verify-tasks           oracle + null control, no model, no API cost
  deskmind-hands run --set smoke        run a task set against a system configuration
  deskmind-hands do "<goal>" --in DIR   run against a real directory, no fixture, no grader
  deskmind-hands score <report.json>    aggregate one or more reports
  deskmind-hands replay <run_dir>       inspect or re-drive a recorded trajectory
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import os
import re
import shlex
import subprocess
from pathlib import Path

from .adapters.base import AdapterUnavailable
from .config import env_file, load_env, redact
from .adapters.scripted import NullAdapter, OracleAdapter, ReplayAdapter
from deskmind_bench.failures import Failure
from deskmind_bench.graders.score import Grade
from .runtime.loop import Metrics, RunState
from .drivers.base import DriverUnavailable
from .drivers.mock import MockDriver
from .env.workspace import LiveWorkspace, Workspace
from deskmind_bench.failures import FailureClass
from .record.recorder import Recorder, environment_manifest
from .record import replay as replay_mod
from .report.aggregate import aggregate, markdown_table
from .runtime.loop import RunConfig, RunResult, StdinUser, run_task
from .apps import APPS
from deskmind_bench.task import Task, load_set, load_task

REPO = Path(__file__).resolve().parent.parent
#: Task sets and fixtures: this checkout by default; deskmind-bench points them at its own suite.
TASKS_DIR = Path(os.environ.get("DESKMIND_TASKS_DIR") or REPO.joinpath("tasks"))
FIXTURES_DIR = Path(os.environ.get("DESKMIND_FIXTURES_DIR") or REPO.joinpath("fixtures"))
#: Where runs are written (traces, screenshots, reports): another repo's runs/ when it drives this one -- a private
#: overlay keeps its runs beside its data.
RUNS_DIR = Path(os.environ.get("DESKMIND_RUNS_DIR") or REPO.joinpath("runs"))
G = "\033[32m"; R = "\033[31m"; Y = "\033[33m"; D = "\033[2m"; X = "\033[0m"




#: A file named in an instruction: a name, a dot and a short extension of letters ("records.txt", "报销单.xlsx").
#: Not a version or a number ("v0.1", "3.5"), a web address or an app bundle.
NAMED_FILE = re.compile(r"(?<![\w./@-])([\w\u4e00-\u9fff][\w\u4e00-\u9fff .\-]*?\.[A-Za-z][A-Za-z0-9]{0,4})(?![\w/@-])")
NOT_FILES = {"app", "com", "org", "net", "io", "cn", "www", "html", "htm"}


def named_files(goal: str) -> list[str]:
    """The files an instruction names, in order (see NAMED_FILE)."""
    out = []
    for m in NAMED_FILE.finditer(goal or ""):
        name = m.group(1).strip().rsplit(" ", 1)[-1]
        ext = name.rsplit(".", 1)[-1].lower()
        if ext in NOT_FILES or "." not in name or name.startswith("."):
            continue
        if name not in out:
            out.append(name)
    return out


def _window_titles() -> list[str]:
    """Every window's title the window server reports (empty when it cannot be asked)."""
    try:
        import Quartz
        return [str(w.get("kCGWindowName") or "") for w in
                Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID) or []]
    except Exception:   # noqa: BLE001 -- unknown windows: nothing is refused
        return []


def _named_files_nowhere(goal: str, apps: set[str]) -> list[str]:
    """The files the instruction names that one of the run's apps opens (TextEdit's text, Preview's images), when not
    one of them is in an open window -- the run has nothing to open them from. Empty when it names none, when any is
    open, or when the windows cannot be listed. A name no app of the run opens (a mail attachment in a web app) is
    the app's business, never refused here."""
    from .drivers.peekaboo import PeekabooDriver
    exts = {e for b, es in PeekabooDriver.DOCUMENT_OPENERS.items() if b.lower() in {a.lower() for a in apps} for e in es}
    names = [n for n in named_files(goal) if "." + n.rsplit(".", 1)[-1].lower() in exts]
    titles = [t for t in _window_titles() if t]   # all untitled: no Screen Recording, so titles cannot be read
    if not names or not titles:
        return []
    return [] if any(n in t for n in names for t in titles) else names

def _bundle_running(bundle: str) -> bool:
    """Whether an app is running, asked of the window server's list of apps, never through AppleScript (which launches
    the app it names)."""
    try:
        from AppKit import NSRunningApplication
        return bool(NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle))
    except Exception:   # noqa: BLE001 -- unknown is "running": the old behaviour, a launch
        return True


def _opens_folder_documents(bundle: str, target) -> bool:
    """Whether `bundle` is the app that opens a document of the attached folder (drivers.peekaboo DOCUMENT_OPENERS)."""
    from .drivers.peekaboo import PeekabooDriver
    exts = next((e for b, e in PeekabooDriver.DOCUMENT_OPENERS.items() if b.lower() == bundle.lower()), ())
    if target is None or not exts:
        return False
    try:
        return any(p.is_file() and p.suffix.lower() in exts for p in Path(target).iterdir())
    except OSError:
        return False

def _c(s: str, colour: str) -> str:
    return f"{colour}{s}{X}" if sys.stdout.isatty() else s


# ---------------------------------------------------------------- doctor

def cmd_doctor(args) -> int:
    print("hands doctor\n")
    rows: list[tuple[str, bool, str]] = []

    rows.append(("python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0]))
    try:
        import yaml  # noqa: F401
        rows.append(("pyyaml (task files)", True, "installed"))
    except ImportError:
        rows.append(("pyyaml (task files)", False, "pip install pyyaml"))
    try:
        import PIL  # noqa: F401
        rows.append(("pillow (mock screenshots)", True, PIL.__version__))
    except ImportError:
        rows.append(("pillow (mock screenshots)", False, "mock runs without images"))

    peekaboo = shutil.which("peekaboo")
    rows.append(("peekaboo (host driver)", bool(peekaboo), peekaboo or "not installed -- L2+ blocked"))
    rows.append(("UTM (formal-set guest)", Path("/Applications/UTM.app").exists(),
                 "installed" if Path("/Applications/UTM.app").exists() else "not installed -- L3 blocked"))

    for mod, label in (("anthropic", "anthropic sdk"), ("openai", "openai sdk"), ("mlx_vlm", "mlx-vlm (local)")):
        try:
            __import__(mod)
            rows.append((label, True, "installed"))
        except ImportError:
            rows.append((label, False, f"pip install {mod.replace('_','-')}"))

    import os
    load_env()
    cfg = env_file()
    rows.append((f"config {cfg.name}", cfg.is_file(), str(cfg) if cfg.is_file() else f"{cfg} not present"))
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY"):
        v = os.environ.get(var)
        rows.append((var, bool(v), redact(v) if "KEY" in var else (v or "<unset>")))

    width = max(len(r[0]) for r in rows)
    for name, ok, detail in rows:
        mark = _c("ok  ", G) if ok else _c("miss", Y)
        print(f"  [{mark}] {name.ljust(width)}  {_c(detail, D)}")

    tiers = [
        ("L0 contract tests", True, "pure python, no desktop"),
        ("L1 smoke on mock desktop", True, "oracle/null adapters, no model"),
        ("L1 smoke with a cloud model", any(bool(__import__('os').environ.get(v))
                                            for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")),
         "needs an sdk + api key"),
        ("L2 dev set on host desktop", bool(peekaboo), "needs peekaboo + accessibility permission"),
        ("L3 formal set in guest VM", Path("/Applications/UTM.app").exists(), "needs UTM + a macOS guest"),
    ]
    print("\n  eval tiers runnable here:")
    for name, ok, note in tiers:
        print(f"    {_c('yes', G) if ok else _c('no ', R)}  {name.ljust(28)} {_c(note, D)}")
    return 0


# ---------------------------------------------------------------- helpers

def _run_dir(root: Path, run_id: str) -> Path:
    d = root / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def task_apps(staged: list[str], apps: dict[str, str], start: str) -> list[str]:
    """The apps whose windows a `do` run offers: the staged ones and every other app the request names (bundle
    ids; the start app is observed anyway)."""
    return list(dict.fromkeys(staged + [b for b in apps.values() if b != start]))


def _make_driver(name: str, keep_shots: bool, scratch: Path | None = None,
                 app: str | None = None, stage: list[str] | None = None,
                 reset_apps: list[str] | None = None):
    if name == "mock":
        return MockDriver(render=keep_shots)
    if name == "peekaboo":
        from .drivers.peekaboo import PeekabooDriver
        # A desktop task starts from "the folder is on screen", and focus has to
        # be grantable: background delivery never moves keyboard focus, so a
        # field can otherwise never be typed into.
        # The workspace folder is Finder's starting point only; a chat-app task has nothing to open. The chat app is the
        # user's live chat app: nothing is ever delivered to it in the foreground.
        finder = (app or "com.apple.finder").lower() == "com.apple.finder"
        return PeekabooDriver(app=app or "com.apple.finder",
                              open_workspace=not stage and finder,
                              allow_foreground=(app or "").lower() != APPS.chat_bundle,
                              stage_commands=stage, reset_apps=reset_apps,
                              scratch=Path(scratch or "."),
                              transport=os.environ.get("HANDS_PEEKABOO_TRANSPORT", "cli"))
    raise SystemExit(f"unknown driver {name!r}")


def _execute(task: Task, adapter, *, system: str, run_id: str, runs_root: Path,
             channels: frozenset[str], keep_shots: bool, driver_name: str = "mock") -> RunResult:
    rd = _run_dir(runs_root, run_id)
    rec = Recorder(rd, keep_screenshots=keep_shots)
    rec.manifest({
        "run_id": run_id, "task_id": task.id, "system": system,
        "task_file": str(task.source_path), "fixture": task.fixture,
        "channels": sorted(channels), "adapter": adapter.name, "driver": driver_name,
        "budget": {"max_actions": task.budget.max_actions,
                   "wall_clock_s": task.budget.wall_clock_s},
        "environment": environment_manifest(REPO),
    })
    ws = Workspace.create(rd, FIXTURES_DIR)
    driver = _make_driver(driver_name, keep_shots, scratch=rd,
                          app=task.app, stage=task.stage, reset_apps=task.reset_apps)
    try:
        res = run_task(task, driver, adapter, ws,
                       config=RunConfig(channels=channels, save_screenshots=keep_shots,
                                        track_goal=os.environ.get("HANDS_TRACK_GOAL") == "1",
                                        system_label=system),
                       run_id=run_id, recorder=rec)
        rec.summary(res.to_json())
        return res
    finally:
        driver.close()
        rec.close()


def _unavailable(task: Task, system: str, run_id: str, reason: str,
                 cls: FailureClass = FailureClass.PROVIDER_UNAVAILABLE) -> RunResult:
    """A configuration that cannot start is recorded, never silently skipped.

    The class matters for routing even though both are excluded from capability
    scores: a missing model is the provider's problem, a missing driver is ours.
    """
    return RunResult(run_id=run_id, task_id=task.id, system_label=system,
                     state=RunState.ERRORED,
                     grade=Grade(strict=False, partial=0.0),
                     failure=Failure(cls, reason, auto=True),
                     metrics=Metrics())


def _verify_effect(task: Task, runs_root: Path) -> tuple[bool, str]:
    """Apply the task's effect oracle directly and grade the result.

    No driver, no model, no loop -- just: put the workspace in the state the task
    describes, and check that the grader says so. When this fails, the task or
    the grader is wrong, and that verdict holds for every driver.
    """
    from deskmind_bench.graders.primitives import GradeContext
    from deskmind_bench.graders.score import grade as run_grader

    rd = _run_dir(runs_root, f"{task.id}-effect")
    ws = Workspace.create(rd, FIXTURES_DIR)
    ws.reset(task.fixture)
    sentinels = ws.sentinel_digests(task.sentinels)
    env = {**os.environ, "WS": str(ws.ws)}
    for cmd in task.oracle_effect:
        proc = subprocess.run(["/bin/sh", "-c", cmd], cwd=str(ws.ws), env=env,
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return False, f"effect step failed: {cmd!r} -> {(proc.stderr or '').strip()[:160]}"
    g = run_grader(task, GradeContext(workspace=ws.ws, vars=task.vars,
                                      run={"state": "completed", "metrics": {}}),
                   sentinel_digests=sentinels)
    if g.error:
        return False, f"grader error: {g.error}"
    # Process checkpoints constrain the route, and an effect oracle takes no
    # route at all -- it writes the destination. Judging it on those would be
    # asking it to have asked a question it was never in a position to ask.
    outcome = {c.name for c in task.checkpoints if not c.process}
    bad = [k for k, v in g.checkpoints.items() if not v.ok and k in outcome]
    if bad or g.violations:
        return False, f"effect oracle did not reach the outcome; failed={bad} {g.violations}"
    return True, "effect oracle reaches strict"


def _line(res: RunResult, extra: str = "") -> str:
    tag = _c("PASS", G) if res.strict else _c("FAIL", R)
    cls = res.failure.cls.value
    note = "" if cls == "none" else f"  {_c(cls, Y)}: {res.failure.detail[:90]}"
    return (f"  [{tag}] {res.task_id.ljust(24)} partial={res.grade.partial:5.2f} "
            f"actions={res.metrics.actions:2d} {res.metrics.agent_s:5.2f}s{extra}{note}")


# ---------------------------------------------------------------- verify

def cmd_verify(args) -> int:
    """The gate that keeps the benchmark from measuring its own bugs.

    Oracle must pass; the do-nothing control must fail. A task that fails either
    check is not a hard task, it is a broken one, and no model score from it can
    be interpreted.
    """
    tasks = load_set(TASKS_DIR, args.set)
    runs_root = RUNS_DIR / f"verify-{time.strftime('%Y%m%d-%H%M%S')}"
    print(f"verifying {len(tasks)} tasks in set {args.set!r}\n")
    bad: list[str] = []

    for t in tasks:
        effect_ok, effect_detail = (True, "no effect oracle")
        if t.oracle_effect:
            effect_ok, effect_detail = _verify_effect(t, runs_root)
        nul_only = _execute(t, NullAdapter(), system="null", run_id=f"{t.id}-null",
                            runs_root=runs_root, channels=frozenset({"screenshot", "ax"}),
                            keep_shots=False) if not t.oracle else None
        if nul_only is not None:
            problems = []
            if not effect_ok:
                problems.append(effect_detail)
            if nul_only.strict:
                problems.append("do-nothing control scored strict -- grader is vacuous")
            if nul_only.grade.partial > 0 and not t.allow_vacuous:
                problems.append(f"do-nothing control earned partial={nul_only.grade.partial:.2f}"
                                " -- a checkpoint is satisfied by the fixture alone")
            mark = _c("OK  ", G) if not problems else _c("BAD ", R)
            print(f"  [{mark}] {t.id.ljust(24)} effect={effect_ok} "
                  f"null strict={nul_only.strict} null partial={nul_only.grade.partial:.2f}  "
                  f"{_c('(no GUI oracle yet)', D)}")
            for pr in problems:
                print(f"          {_c(pr, R)}")
            if problems:
                bad.append(t.id)
            continue

        orc = _execute(t, OracleAdapter(), system="oracle", run_id=f"{t.id}-oracle",
                       runs_root=runs_root, channels=frozenset({"screenshot", "ax"}),
                       keep_shots=False)
        nul = _execute(t, NullAdapter(), system="null", run_id=f"{t.id}-null",
                       runs_root=runs_root, channels=frozenset({"screenshot", "ax"}),
                       keep_shots=False)

        problems = []
        if not effect_ok:
            problems.append(effect_detail)
        if not orc.strict:
            problems.append("oracle did not reach strict success -- task or grader is broken")
        if nul.strict:
            problems.append("do-nothing control scored strict -- grader is vacuous")
        if nul.grade.partial > 0 and not t.allow_vacuous:
            problems.append(f"do-nothing control earned partial={nul.grade.partial:.2f} "
                            "-- a checkpoint is satisfied by the fixture alone")

        ok = not problems
        mark = _c("OK  ", G) if ok else _c("BAD ", R)
        print(f"  [{mark}] {t.id.ljust(24)} oracle strict={orc.strict} "
              f"null strict={nul.strict} null partial={nul.grade.partial:.2f} "
              f"reset={orc.metrics.reset_s*1000:.0f}ms")
        for p in problems:
            print(f"          {_c(p, R)}")
            if orc.failure.detail:
                print(f"          {_c('oracle detail: ' + orc.failure.detail[:120], D)}")
        if not ok:
            bad.append(t.id)

    print()
    if bad:
        print(_c(f"{len(bad)} task(s) failed verification: {', '.join(bad)}", R))
        return 1
    n_effect = sum(1 for t in tasks if t.oracle_effect)
    n_path = sum(1 for t in tasks if t.oracle)
    print(_c(f"all {len(tasks)} tasks verified "
             f"({n_effect} effect oracle, {n_path} GUI oracle, {len(tasks)} null control)", G))
    return 0


# ---------------------------------------------------------------- run

def _make_adapter(name: str, args):
    if name == "oracle":
        return OracleAdapter()
    if name == "null":
        return NullAdapter()
    if name == "anthropic":
        from .adapters.anthropic_cu import AnthropicAdapter
        return AnthropicAdapter(model=args.model,
                                toolset=getattr(args, "toolset", "custom"),
                                effort=getattr(args, "effort", "high"))
    if name == "openai":
        from .adapters.openai_cu import OpenAIAdapter
        return OpenAIAdapter(model=args.model)
    if name == "hybrid":
        from .adapters.anthropic_cu import AnthropicAdapter
        from .adapters.hybrid import HybridAdapter
        from .adapters.guiowl_local import GuiOwlLocalAdapter
        from .adapters.mlx_local import MLXAdapter
        local = getattr(args, "local_adapter", "guiowl")
        grounder = (GuiOwlLocalAdapter(model=getattr(args, "local_model", None), zoom=float(getattr(args, "zoom", 0.5)))
                    if local == "guiowl"
                    else MLXAdapter(model=getattr(args, "local_model", None),
                                    convention=getattr(args, "convention", "ui-tars")))
        return HybridAdapter(
            planner=AnthropicAdapter(model=args.model or "claude-opus-5",
                                     toolset="custom", effort=getattr(args, "effort", "high")),
            grounder=grounder,
            max_looks_per_turn=getattr(args, "max_looks", 2),
            mode=getattr(args, "hybrid_mode", "describe"),
            describe_px=getattr(args, "describe_px", 78_000))
    if name == "mlx":
        from .adapters.mlx_local import MLXAdapter
        return MLXAdapter(model=args.model,
                          convention=getattr(args, "convention", "ui-tars"))
    if name == "systemone":
        from .adapters.systemone import SystemOneAdapter
        return SystemOneAdapter(model=args.model or "brain-0.8b",
                                url=getattr(args, "systemone_url", "http://127.0.0.1:8793"),
                                api_key=os.environ.get("SYSTEMONE_API_KEY"),
                                text_helper=getattr(args, "text_helper", None),
                                # A slow planner (a general model answering a 10k-token state) takes well over the
                                # default minute; told nothing, the run reads it as the endpoint being down.
                                timeout=float(os.environ.get("HANDS_PLANNER_TIMEOUT", "60")))
    if name == "guiowl":
        from .adapters.guiowl_local import GuiOwlLocalAdapter
        return GuiOwlLocalAdapter(model=args.model, zoom=float(getattr(args, "zoom", 0.5)),
                                  coarse_pixels=int(getattr(args, "coarse_px", 2_007_040)),
                                  crop_pixels=int(getattr(args, "crop_px", 2_007_040)))
    raise SystemExit(f"unknown adapter {name!r}")


def cmd_run(args) -> int:
    tasks = load_set(TASKS_DIR, args.set)
    if args.task:
        tasks = [t for t in tasks if t.id in set(args.task)]
        if not tasks:
            raise SystemExit(f"no task in set {args.set!r} matched {args.task}")
    channels = frozenset(args.channels.split(","))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    system = args.system or f"{args.adapter}:{args.model or 'default'}"
    runs_root = RUNS_DIR / f"{stamp}-{args.adapter}"
    print(f"running set {args.set!r}: {len(tasks)} tasks x {args.repeats} "
          f"= {len(tasks)*args.repeats} runs   system={system}   channels={sorted(channels)}\n")

    results: list[RunResult] = []
    for rep in range(1, args.repeats + 1):
        if args.repeats > 1:
            print(_c(f"  -- repeat {rep}/{args.repeats}", D))
        for t in tasks:
            try:
                adapter = _make_adapter(args.adapter, args)
                res = _execute(t, adapter, system=system, run_id=f"{t.id}-r{rep}",
                               runs_root=runs_root, channels=channels,
                               keep_shots=not args.no_screenshots, driver_name=args.driver)
            except AdapterUnavailable as exc:
                res = _unavailable(t, system, f"{t.id}-r{rep}", str(exc),
                                   FailureClass.PROVIDER_UNAVAILABLE)
            except DriverUnavailable as exc:
                res = _unavailable(t, system, f"{t.id}-r{rep}", str(exc),
                                   FailureClass.ENVIRONMENT)
            results.append(res)
            print(_line(res))

    agg = aggregate(results, system)
    report = {
        "system": system, "task_set": args.set, "adapter": args.adapter,
        "model": args.model, "channels": sorted(channels), "repeats": args.repeats,
        "runs_dir": str(runs_root), "environment": environment_manifest(REPO),
        "aggregate": agg.to_json(),
        "runs": [r.to_json() for r in results],
    }
    out = Path(args.report) if getattr(args, "report", None) else REPO / "reports" / f"{stamp}-{args.adapter}.json"
    out.parent.mkdir(parents=True, exist_ok=True)   # a fresh checkout may have no reports/ yet
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + markdown_table([agg]))
    if agg.failures:
        print("\n  failure classes: " + ", ".join(f"{k}={v}" for k, v in agg.failures.items()))
    print(f"\n  report     {out}")
    print(f"  traces     {runs_root.relative_to(RUNS_DIR.parent)}")

    if args.gate is not None and agg.strict_pct < args.gate:
        print(_c(f"\n  GATE FAILED: strict {agg.strict_pct:.1f}% < required {args.gate:.1f}%", R))
        return 1
    return 0


# ---------------------------------------------------------------- score / replay

def cmd_do(args) -> int:
    """Run against a real directory: no fixture, no grader, a change report."""
    from .live import (Changes, TargetRefused, diff, guard_target, live_task,
                       render, snapshot)

    # A folder is optional, as in a chat that may or may not have one attached. Without one the run operates apps
    # only: nothing is staged in Finder, and there is no change report because there are no files of the user's in
    # scope. With one, file work is confined to it, as before.
    target, before = None, {}
    if args.into:
        try:
            target = guard_target(args.into)
            before = snapshot(target)
        except TargetRefused as exc:
            print(_c(f"refused: {exc}", R))
            return 2
    if target is None:
        run_apps = {args.app} | {b.partition("=")[2].strip() for b in (args.apps or "").split(",") if "=" in b}
        missing = _named_files_nowhere(args.goal, run_apps)
        if missing:
            # Named files with nothing to find them in: no folder attached and no window showing them. Started
            # anyway, TextEdit came up with its Open panel and the planner clicked in it until the run was stopped
            # as stuck (a D4 take with the folder left unattached, 10-01). Said now, in words the user can act on.
            names = ", ".join(missing)
            print(_c(f"refused: the instruction names {names}, but no folder is attached and no open window shows "
                     f"{'it' if len(missing) == 1 else 'them'}. Attach the folder "
                     f"{'it is' if len(missing) == 1 else 'they are'} in, or open "
                     f"{'it' if len(missing) == 1 else 'them'} first.", R))
            return 2
    apps = {}
    for item in (args.apps or "").split(","):
        name, _, bundle = item.partition("=")
        if name.strip() and bundle.strip():
            apps[name.strip()] = bundle.strip()
    if args.foreground_ok:
        # The user agreed that these apps may be brought forward for a moment when they ignore background input.
        allowed = {b.strip() for b in os.environ.get("HANDS_FLASH_APPS", "").split(",") if b.strip()}
        os.environ["HANDS_FLASH_APPS"] = ",".join(sorted(allowed | set(apps.values()) | {args.app}))

    task = live_task(args.goal, target, app=args.app, apps=apps,
                     max_actions=args.max_actions, wall_clock_s=args.minutes * 60)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    rd = RUNS_DIR / f"do-{stamp}"

    if target:
        print(f"target   {target}   ({len(before)} files)")
    else:
        print(f"target   none (apps only: {', '.join(apps) or args.app})")
    print(f"goal     {task.goal.strip().splitlines()[0][:88]}")
    print(f"budget   {args.max_actions} actions, {args.minutes} min   "
          f"model {args.model or 'default'}")
    print(_c("these files are real and there is no undo. ctrl-c now if the target is wrong.", D))
    if not args.yes:
        try:
            if input("proceed? [y/N] ").strip().lower() not in {"y", "yes"}:
                print("cancelled")
                return 1
        except EOFError:
            print(_c("no tty to confirm on; pass --yes if you meant it", R))
            return 1

    rec = Recorder(rd, keep_screenshots=not args.no_screenshots)
    rec.manifest({
        "run_id": task.id, "task_id": task.id, "system": f"do:{args.model or 'default'}",
        "goal": task.goal, "target": str(target) if target else None, "apps": apps, "adapter": args.adapter,
        # What a replay needs to rebuild the run's task (deskmind_hands/replay.py).
        "app": args.app, "ask": args.ask,
        "driver": "peekaboo", "live": True,
        "budget": {"max_actions": task.budget.max_actions,
                   "wall_clock_s": task.budget.wall_clock_s},
        "files_before": len(before),
        "environment": environment_manifest(REPO),
    })
    # LiveWorkspace, not Workspace: run_task resets unconditionally, and
    # Workspace.reset starts by deleting the tree. The subclass has no restore
    # at all, so the destructive branch does not exist on this path.
    if target is None:
        # Nothing of the user's: the run's own empty scratch folder stands in, and nothing reads or reports it.
        (rd / "scratch").mkdir(parents=True, exist_ok=True)
    ws = LiveWorkspace(root=rd, ws=target or (rd / "scratch"), fixtures_dir=FIXTURES_DIR)
    adapter = _make_adapter(args.adapter, args)
    # Staging is not scaffolding for measurement. An eval task opens its
    # documents before the agent looks, and the first live run showed what
    # happens without it: 29 actions spent hunting through File > Open for a
    # folder nobody had put on screen, and nothing written. The target is opened
    # here for the same reason -- but nothing is closed or quit, because the
    # other windows belong to the user.
    # The app is started (in the background) when it is not Finder; Finder shows the folder when there is one.
    if args.app.lower() == "com.apple.finder":
        stage = [f'open {shlex.quote(str(target))}'] if target else []
    elif _opens_folder_documents(args.app, target) and not _bundle_running(args.app):
        # Not started bare: a document app started with no document shows its Open panel (TextEdit's sat on screen
        # for 17 s of a recording, 10-01). It is started by opening the folder's document, the run's first step.
        stage = []
    else:
        stage = [f'open -b {shlex.quote(args.app)}']
    driver = _make_driver("peekaboo", not args.no_screenshots, scratch=rd,
                          app=args.app, stage=stage, reset_apps=[])
    # The other apps the request names are the task's apps: their windows are offered as windows to switch to, as a
    # task set's staged apps' are. Without them a request that reads in Safari and writes in TextEdit had no way to
    # TextEdit at all -- no window to switch to, and no FOCUS_APP -- and the planner clicked Safari's sidebar until
    # the run ran out (a Safari-to-TextEdit request, 0/5; the same strings in the task harness, 3/3).
    driver._stage_apps = task_apps(driver._stage_apps, apps, args.app)
    # The attached folder's documents are offered to open, and a document can be closed: a request that says "open
    # parts.csv from the folder" had nothing on screen to open it by.
    driver.offer_folder_files = target is not None
    res = None
    try:
        res = run_task(task, driver, adapter, ws,
                       config=RunConfig(channels=frozenset({"ax", "screenshot"}),
                                        save_screenshots=not args.no_screenshots,
                                        system_label="do",
                                        user=StdinUser() if args.ask == "stdin" else None,
                                        # A person is there to ask: sending, deleting, paying and the like wait
                                        # for their approval (runtime/risk.py).
                                        approve_risky=args.ask == "stdin",
                                        # Nobody to ask: those steps are refused, unless unattended risky steps
                                        # were chosen on purpose (--allow-risky: a sandbox like the gym).
                                        refuse_risky=args.ask != "stdin" and not args.allow_risky,
                                        # A person's own folder: renaming a file or folder waits for approval too.
                                        confirm_renames=args.confirm_renames),
                       run_id=task.id, recorder=rec)
    finally:
        driver.close()
        changes = diff(before, snapshot(target)) if target else Changes([], [], [])
        # Both halves, merged. Writing only the change list once cost a failure
        # reason: run.json held the diff and nothing about why the run stopped.
        rec.summary({**(res.to_json() if res else {"state": "crashed"}),
                     "changes": changes.to_json()})
        rec.close()

    print()
    if target:
        print(render(changes, target))
    # A question is answered by the DONE that ends the run ("answer: …", see the ANSWER operation).
    answer = next((str(st.get("text", ""))[8:] for st in reversed(getattr(res, "steps", None) or [])
                   if isinstance(st, dict) and str(st.get("text", "")).startswith("answer: ")), None)
    if answer:
        print(f"answer   {answer}")
    m = res.metrics
    print(f"\n{res.state.value}  {m.actions} actions  {m.total_s:.0f}s  "
          f"${m.cost_usd or 0:.2f}")
    if res.failure and res.failure.cls is not FailureClass.NONE:
        print(_c(f"{res.failure.cls.value}: {res.failure.detail[:200]}", R))
    print(_c(f"trace {rd.relative_to(RUNS_DIR.parent)}", D))
    # The grade is meaningless without checkpoints, so it is not printed. What
    # the run is answerable for is the change list above.
    return 0 if res.state is RunState.COMPLETED else 1


def cmd_score(args) -> int:
    aggs = []
    for p in args.reports:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        aggs.append(aggregate(data["runs"], data["system"]))
    print(markdown_table(aggs))
    for a in aggs:
        weak = [t for t, v in a.per_task_strict.items() if v < 100.0]
        if weak:
            print(f"\n  {a.system}: tasks below 100% strict -> {', '.join(sorted(weak))}")
    return 0


def cmd_replay(args) -> int:
    d = Path(args.run_dir)
    fr = replay_mod.frames(d)
    print(f"{len(fr)} frames in {d}")
    if args.grounding:
        pairs = replay_mod.grounding_pairs(d)
        out = REPO / "reports" / f"grounding-{d.name}.json"
        out.write_text(json.dumps(pairs, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"harvested {len(pairs)} labelled click targets -> {out.relative_to(REPO)}")
        return 0
    for f in fr:
        a = (f.action or {}).get("kind", "-")
        eid = ((f.action or {}).get("binding") or {}).get("element_id", "")
        print(f"  {f.obs_id}  {a:14s} {eid:24s} ok={f.ok}  {f.detail[:60]}")
    return 0


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hands", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="report what this machine can run").set_defaults(fn=cmd_doctor)

    v = sub.add_parser("verify-tasks", help="oracle + null control; no model calls")
    v.add_argument("--set", default="smoke")
    v.set_defaults(fn=cmd_verify)

    r = sub.add_parser("run", help="run a task set")
    r.add_argument("--set", default="smoke")
    r.add_argument("--task", action="append", help="run only these task ids")
    r.add_argument("--driver", default="mock", choices=["mock", "peekaboo"])
    r.add_argument("--adapter", default="oracle",
                   choices=["oracle", "null", "anthropic", "openai", "mlx", "guiowl", "systemone", "hybrid"])
    r.add_argument("--model", default=None)
    r.add_argument("--toolset", default="custom", choices=["custom", "native"],
                   help="native = Anthropic's computer toolset; custom = our own schema")
    r.add_argument("--local-model", default=None,
                   help="hybrid: the local model that answers visual questions")
    r.add_argument("--hybrid-mode", default="describe", choices=["ask", "describe"],
                   help="describe = local pre-reads every screen (1 cloud round); "
                        "ask = planner requests a look (2 cloud rounds, slower by design)")
    r.add_argument("--describe-px", type=int, default=78_000,
                   help="pixel budget for the local pass; 78k held 3/3 accuracy at 0.5s")
    r.add_argument("--max-looks", type=int, default=2,
                   help="hybrid: visual questions allowed per turn")
    r.add_argument("--text-helper", default=None, dest="text_helper",
                   help="systemone: model that supplies TYPE_TEXT values (a typed choice cannot produce a string)")
    r.add_argument("--local-adapter", default="guiowl", choices=["guiowl", "mlx"], dest="local_adapter",
                   help="hybrid: which local model answers the visual questions")
    r.add_argument("--systemone-url", default="http://127.0.0.1:8793", dest="systemone_url",
                   help="System One endpoint: a local DeskMind Brain server, or any other /v1/systemone server")
    r.add_argument("--zoom", default=0.5, type=float,
                   help="guiowl: crop fraction for the second pass (0 disables it)")
    r.add_argument("--coarse-px", default=2_007_040, type=int, dest="coarse_px")
    r.add_argument("--crop-px", default=2_007_040, type=int, dest="crop_px")
    r.add_argument("--convention", default="ui-tars",
                   help="local model coordinate convention; establish it with "
                        "tools/identify_coords.py, never guess")
    r.add_argument("--effort", default="high",
                   choices=["low", "medium", "high", "xhigh", "max"])
    r.add_argument("--system", default=None, help="label for the report")
    r.add_argument("--repeats", type=int, default=1)
    r.add_argument("--report", default=None, help="write the report here instead of reports/<stamp>.json")
    r.add_argument("--channels", default="screenshot,ax",
                   help="observation channels granted to the model")
    r.add_argument("--no-screenshots", action="store_true")
    r.add_argument("--gate", type=float, default=None,
                   help="exit non-zero if strict%% falls below this")
    r.set_defaults(fn=cmd_run)

    d = sub.add_parser("do", help="run against a real directory (no fixture, no grader)")
    d.add_argument("goal", help="what you want done, in your own words")
    d.add_argument("--apps", default="", metavar="NAME=BUNDLE,...",
                   help="apps the run may use, by name (what the goal calls them) and bundle id")
    d.add_argument("--ask", choices=["none", "stdin"], default="none",
                   help="stdin: the agent's questions go out as HANDS_ASK lines and answers come back on stdin")
    d.add_argument("--allow-risky", action="store_true",
                   help="without --ask stdin, carry out sending, deleting, paying, publishing and sharing unasked "
                        "(by default they are refused when there is nobody to ask); for sandboxes such as the gym")
    d.add_argument("--confirm-renames", action="store_true",
                   help="renaming a file or folder needs approval too, like deleting (there is no undo): for a "
                        "person's own folder, not a sample one")
    d.add_argument("--foreground-ok", action="store_true",
                   help="the user agreed these apps may be brought forward briefly when they ignore background input")
    d.add_argument("--in", dest="into", default=None, metavar="DIR",
                   help="the folder to work in; not a home directory or a system root")
    d.add_argument("--app", default="com.apple.finder",
                   help="bundle id of the app to start in")
    d.add_argument("--adapter", default="anthropic",
                   choices=["anthropic", "openai", "mlx", "guiowl", "systemone", "hybrid"])
    d.add_argument("--model", default=None)
    d.add_argument("--systemone-url", default="http://127.0.0.1:8793",
                   help="the /v1/systemone planner for --adapter systemone (a local DeskMind Brain server)")
    d.add_argument("--toolset", default="custom", choices=["custom", "native"])
    d.add_argument("--max-actions", type=int, default=60)
    d.add_argument("--minutes", type=float, default=20.0)
    d.add_argument("--effort", default=None,
                   choices=["low", "medium", "high", "xhigh", "max"])
    d.add_argument("--no-screenshots", action="store_true")
    d.add_argument("--yes", action="store_true", help="skip the confirmation")
    d.set_defaults(fn=cmd_do, local_model=None, hybrid_mode="describe",
                   describe_px=78_000, max_looks=None, convention=None)

    s = sub.add_parser("score", help="aggregate one or more reports")
    s.add_argument("reports", nargs="+")
    s.set_defaults(fn=cmd_score)

    rp = sub.add_parser("replay", help="inspect a recorded trajectory")
    rp.add_argument("run_dir")
    rp.add_argument("--grounding", action="store_true",
                    help="harvest labelled click targets into a grounding dataset")
    rp.set_defaults(fn=cmd_replay)

    args = p.parse_args(argv)
    load_env()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
