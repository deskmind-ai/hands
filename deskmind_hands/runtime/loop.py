"""The agent loop.

Deliberately boring and fully observable. Every decision the loop makes -- what
the model was shown, why an action was refused, when an injection fired, which
budget ran out -- lands in the trajectory, because a run you cannot explain is a
run you cannot learn from.

Two things here are product behaviour rather than harness plumbing, and both are
graded: cancellation is handled by the execution queue rather than by waiting for
the model's next reply, and an action bound to a stale view of the screen is
refused instead of clicking wherever those coordinates now point.
"""

from __future__ import annotations

import json
import os

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..actions import Action, ActionKind, DIALOGUE, TERMINAL
from ..drivers.base import Driver, DriverUnavailable, ExecResult, Observation, classify_effect
from ..errors import Kind, classify
from .. import done_check
from . import risk
from ..env.workspace import Workspace
from deskmind_bench.failures import Failure, FailureClass, no_progress
from deskmind_bench.graders.primitives import GradeContext, evaluate as grade_check
from deskmind_bench.graders.score import Grade, grade as run_grader
from deskmind_bench.task import Injection, Task
from ..adapters.base import Adapter, AdapterUnavailable, Proposal, Turn, TurnContext


class RunState(str, Enum):
    PREPARING = "preparing"
    RUNNING = "running"
    AWAITING_USER = "awaiting_user"
    AWAITING_APPROVAL = "awaiting_approval"
    PAUSED = "paused"
    COMPLETED = "completed"        # agent declared done
    GAVE_UP = "gave_up"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ERRORED = "errored"


@dataclass
class RunConfig:
    channels: frozenset[str] = frozenset({"screenshot", "ax"})
    save_screenshots: bool = True
    #: Simulated wall-clock spent waiting for the scripted user. Counted in total
    #: time and subtracted for agent time, per the evaluation plan.
    honour_user_delay: bool = False
    no_progress_window: int = 4
    #: Consecutive failed actions before a run is called stuck. Failures are cheap for the model and expensive for
    #: the clock, and the action budget does not see the non-mutating ones at all.
    max_consecutive_failures: int = 5
    #: Consecutive non-mutating actions (switching windows, scrolling, looking) before a run is called stuck.
    max_idle_actions: int = 12
    #: Observations in a row that may fail transiently (deskmind_hands/errors.py) before the run ends on it.
    max_observe_failures: int = 3
    #: DONEs the check before finishing may send back (done_check.py) before one is accepted anyway.
    max_done_blocks: int = 2
    #: Grade the workspace after every action and record whether the goal was already met ("goal_met"). This is
    #: what separates "never got there" from "got there and did not say DONE", which the final grade cannot: a
    #: model can reach the goal at step 3 and then wander until the budget, or undo it.
    track_goal: bool = False
    system_label: str = "unknown"
    #: Who answers the agent's questions and approval requests: the task's scripted user unless a live run brings a
    #: real one (anything with respond(question, approval) -> (reply, approved, delay_s)).
    user: object | None = None
    #: Ask the user before an action `risk.risky` names (send, delete, pay, publish, share). Live runs with a person
    #: to ask only: a graded set's scripted user and its deletions would turn every such step into a dialogue turn.
    approve_risky: bool = False
    #: A live run with nobody to ask (`deskmind-hands do` without --ask stdin): the actions `risk.risky` names are
    #: refused, not carried out unasked. Running them without approval is a mode chosen on purpose (--allow-risky).
    refuse_risky: bool = False
    #: Renaming a file or folder needs approval too (risk.risky's `renames`): a run in a person's own folder.
    confirm_renames: bool = False


@dataclass
class Metrics:
    actions: int = 0
    dialogue_turns: int = 0
    stale_refusals: int = 0
    indeterminate_actions: int = 0
    parse_errors: int = 0
    #: Steps not carried out because the model was not sure enough of a write or a commit (pipeline.policy).
    unsure_refusals: int = 0
    failed_actions: int = 0
    model_latency_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    user_wait_s: float = 0.0
    total_s: float = 0.0
    reset_s: float = 0.0

    @property
    def agent_s(self) -> float:
        return max(0.0, self.total_s - self.user_wait_s)

    def to_json(self) -> dict:
        d = dict(self.__dict__)
        d["agent_s"] = round(self.agent_s, 3)
        for k in ("model_latency_s", "user_wait_s", "total_s", "reset_s", "cost_usd"):
            d[k] = round(d[k], 4)
        return d


@dataclass
class RunResult:
    run_id: str
    task_id: str
    system_label: str
    state: RunState
    grade: Grade
    failure: Failure
    metrics: Metrics
    steps: list[dict] = field(default_factory=list)
    dialogue: list[dict] = field(default_factory=list)
    #: Whatever the adapter counted about itself. For a split-model
    #: configuration this is the only place the two halves can be told apart --
    #: without it, a comparison against a single-model baseline attributes
    #: everything to the architecture and nothing to the endpoint.
    adapter_usage: dict = field(default_factory=dict)
    #: What the driver counted about itself -- foreground actions above all. "Runs in the background without
    #: disturbing you" is a product claim, and it was being counted and then thrown away: the report only showed
    #: it by grepping step details for the word "foreground".
    driver_state: dict = field(default_factory=dict)

    @property
    def strict(self) -> bool:
        return self.grade.strict

    def to_json(self) -> dict:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "system": self.system_label,
            "state": self.state.value,
            "grade": self.grade.to_json(),
            "failure": self.failure.to_json(),
            "metrics": self.metrics.to_json(),
            "adapter_usage": self.adapter_usage,
            "driver_state": self.driver_state,
            "dialogue": self.dialogue,
        }


class _UserScript:
    """The scripted test user. Same rules for every system under test."""

    def __init__(self, task: Task) -> None:
        self.rules = task.user_script

    def respond(self, question: str, approval: bool, options: tuple[str, ...] = ()) -> tuple[str, bool, float]:
        for r in self.rules:
            if re.search(r.match, question or "", re.DOTALL):
                approve = r.approve if r.approve is not None else True
                return (r.reply or ("approved" if approve else "denied")), approve, r.delay_s
        # No rule matched. Silence is the honest default: the task did not
        # anticipate this question, and inventing an answer would hide the fact.
        return ("(the test user has no answer scripted for this question)", False, 0.0)


def chosen_option(choice: str, options, question: str) -> str:
    """An answer the user picked from the question's options, as a sentence naming what sets it apart: the fields it
    has where the others differ ("The one with 2026-09-27, R-3307, 96."). Handed over as the raw line it was picked
    as -- five columns where the goal's template has four -- G18b rewrote the whole document a dozen ways and never
    finished; told the distinguishing fields it appended the row and saved, as with a typed answer (10-01)."""
    split = lambda line: [c.strip() for c in re.split(r"[,\uff0c\t]", line)]
    mine, others = split(choice), [split(o) for o in options if o != choice]
    differing = [c for i, c in enumerate(mine) if c and any(i >= len(o) or o[i] != c for o in others)]
    if not differing or len(mine) < 2:
        return choice
    if re.search(r"[\u4e00-\u9fff]", question or ""):
        return f"就是 {'、'.join(differing)} 那一条。"
    return f"The one with {', '.join(differing)}."


class StdinUser:
    """A real person, reached through the process that started this run: the question goes out on stdout as one
    line `HANDS_ASK {"question": ..., "approval": bool}`, and the answer comes back on stdin as one JSON line
    {"reply": "...", "approve": true}. The DeskMind helper shows it to the user and writes the answer back."""

    def __init__(self, timeout_s: float = 600.0) -> None:
        self.timeout_s = timeout_s

    def respond(self, question: str, approval: bool, options: tuple[str, ...] = ()) -> tuple[str, bool, float]:
        import json as _json
        import select
        import sys as _sys
        t0 = time.time()
        # The alternatives the question lists go along, for the app to offer as answers: the user clicks the order
        # they mean instead of typing it (a free answer is still taken).
        print("HANDS_ASK " + _json.dumps({"question": question, "approval": approval,
                                          **({"options": list(options)} if options else {})}, ensure_ascii=False),
              flush=True)
        ready, _, _ = select.select([_sys.stdin], [], [], self.timeout_s)
        line = _sys.stdin.readline() if ready else ""
        try:
            ans = _json.loads(line) if line.strip() else {}
        except ValueError:
            ans = {"reply": line.strip()}
        if not ans:
            return ("(the user did not answer)", False, time.time() - t0)
        reply = str(ans.get("reply") or ("approved" if ans.get("approve") else "denied"))
        if reply in options:
            reply = chosen_option(reply, options, question)
        approved = bool(ans.get("approve", False)) if approval else True
        return (reply, approved, time.time() - t0)


def _due_injections(task: Task, mutating_count: int, fired: set[int]) -> list[tuple[int, Injection]]:
    out = []
    for i, inj in enumerate(task.inject):
        if i in fired:
            continue
        if inj.at_action is not None and mutating_count >= inj.at_action:
            out.append((i, inj))
    return out


def _run_info(task: Task, workspace, recorder) -> dict:
    """What a dynamic task's checks read beside the workspace (deskmind_bench.dyn): the run directory, where the
    change record is, and the pristine fixture, to tell what the run changed."""
    return {"dir": str(recorder.dir) if recorder else str(workspace.root),
            "fixture_dir": str(workspace.fixtures_dir / task.fixture) if task.fixture else None}


def _change_due(change, ctx: GradeContext, mutating_count: int, by_name: dict) -> bool:
    """Whether a dynamic task's change should fire now. The loop fires the triggers it can see -- at start, before
    the Nth write, when a checkpoint first passes, when a state holds; before_subgoal and on_ask belong to an
    orchestrator."""
    t = change.trigger
    if "at_start" in t:
        return True
    if "at_action" in t:
        return mutating_count >= int(t["at_action"])
    if "at_checkpoint" in t:
        cp = by_name.get(t["at_checkpoint"])
        if cp is None:
            raise ValueError(f"change {change.id}: no checkpoint named {t['at_checkpoint']!r}")
        return grade_check(cp.check, ctx).ok
    if "at_state" in t:
        return grade_check(t["at_state"], ctx).ok
    return False


#: How a user's unprompted message appears among the answers the planner is shown, by the goal's language.
INTERJECTION = {"zh": "（用户主动说）", "en": "(the user, unprompted)"}


def _goal_met(task: Task, workspace, driver, sentinels) -> bool | None:
    try:
        ctxg = GradeContext(workspace=workspace.ws, vars=task.vars, clipboard=driver.clipboard(),
                            driver_state=driver.state(), run={"state": "running"})
        g = run_grader(task, ctxg, sentinel_digests=sentinels)
        return None if g.error else g.strict
    except Exception:  # noqa: BLE001 - bookkeeping must not fail a run
        return None


def _target_label(obs, action) -> str | None:
    eid = action.binding.element_id
    if not eid:
        return None
    return next((e.label for e in obs.elements if e.id == eid), None)


def is_cycling(sigs: list[tuple], window: int = 12, times: int = 3) -> bool:
    """Each of the last `window` steps is one the run has already taken `times` times or more."""
    return len(sigs) >= window and all(sigs.count(x) >= times for x in sigs[-window:])


def run_task(
    task: Task,
    driver: Driver,
    adapter: Adapter,
    workspace: Workspace,
    *,
    config: RunConfig | None = None,
    run_id: str = "run",
    recorder=None,
) -> RunResult:
    cfg = config or RunConfig()
    m = Metrics()
    steps: list[dict] = []
    dialogue: list[dict] = []
    history: list[Turn] = []
    digests: list[str] = []
    ineffective: list[str] = []
    last_typed_label: str | None = None          # the field the last successful TYPE_TEXT wrote into
    just_approved: risk.Approval | None = None   # the last approval, until its confirmation or the next step
    # The planner's own questions: what its dialogue budget counts, and what takes ASK away. A harness approval is
    # neither -- counted with them, one approval used the budget up and took ASK away for the rest of the run (G16).
    planner_questions = 0
    fired: set[int] = set()
    state = RunState.PREPARING
    failure = Failure(FailureClass.NONE)
    cancel_requested = False
    consecutive_failures = 0
    declined_unsure: set[str] = set()   # doubtful steps the user said no to (pipeline.policy)
    observe_failures = 0
    done_blocks = 0
    asked_s = 0.0                                # spent waiting for the user's answers
    lessons_logged = False
    idle_actions = 0
    notice: str | None = None
    user = cfg.user or _UserScript(task)

    m.reset_s = workspace.reset(task.fixture)
    sentinels = workspace.sentinel_digests(task.sentinels)
    # The goal, for the driver's own guards (a browser's address bar is typed into only for a goal about the web).
    try:
        driver.goal = task.goal
    except AttributeError:
        pass
    driver.start(workspace.ws)

    # Injections with no trigger point are environment preconditions (seeding a
    # clipboard sentinel, for instance) and fire before the agent sees anything.
    for i, inj in enumerate(task.inject):
        if inj.at_action is None and inj.at_checkpoint is None:
            driver.inject(inj.event, inj.params)
            fired.add(i)

    # A dynamic task's changes (deskmind#62) are done to the environment by the bench's injector, never through the
    # driver, and recorded in the run's changes.jsonl. What the user says is delivered as the user's own message.
    injector = None
    if getattr(task, "changes", None):
        from deskmind_bench.dyn.inject import Injector
        injector = Injector(workspace.ws, Path(_run_info(task, workspace, recorder)["dir"]), run_id,
                            apps=tuple({task.app, *task.reset_apps}))
    by_name = {c.name: c for c in task.checkpoints}
    lang = "zh" if re.search(r"[\u4e00-\u9fff]", task.goal or "") else "en"

    def fire_changes(states_only: bool = False) -> None:
        """Fire what is due. Right after an action only the changes waiting on a state are looked at: a checkpoint
        that just passed fires before the planner looks again, even when its next word is `done`."""
        if injector is None:
            return
        ctx = GradeContext(workspace=workspace.ws, vars=task.vars, run=_run_info(task, workspace, recorder))
        for c in task.changes:
            if states_only and not {"at_checkpoint", "at_state"} & c.trigger.keys():
                continue
            if c.id in injector.fired or not _change_due(c, ctx, m.actions, by_name):
                continue
            said = injector.fire(c)
            # In the trace, by the step it came before -- never in `steps`: the repetition and back-and-forth checks
            # and the approval's step arithmetic read those as the agent's own steps.
            if recorder:
                recorder._write({"t": "change", "n": len(steps) + 1, "change": c.id, "type": c.type})
            for text in said:
                dialogue.append({"n": len(steps) + 1, "kind": "user_says", "question": INTERJECTION[lang],
                                 "reply": text, "by": "user"})

    t_start = time.perf_counter()
    state = RunState.RUNNING
    try:
        fire_changes()   # inside the try: a change the injector refuses ends the run as an error, not a crash
        while True:
            if m.total_s or True:
                m.total_s = time.perf_counter() - t_start
            # The clock is the task's: time spent on the user -- answering a question, or waited for while they work
            # at the Mac (the driver's user_wait_s) -- is not the run's to spend.
            waited = asked_s + float(getattr(driver, "user_wait_s", 0.0) or 0.0)
            m.user_wait_s = waited
            if m.total_s - waited > task.budget.wall_clock_s:
                state = RunState.BUDGET_EXHAUSTED
                failure = Failure(FailureClass.BUDGET_EXHAUSTED,
                                  f"wall clock {m.total_s - waited:.0f}s of work > {task.budget.wall_clock_s:.0f}s "
                                  f"({waited:.0f}s more waiting on the user)", auto=True)
                break
            if m.actions >= task.budget.max_actions:
                state = RunState.BUDGET_EXHAUSTED
                failure = Failure(FailureClass.BUDGET_EXHAUSTED,
                                  f"{m.actions} actions >= budget {task.budget.max_actions}", auto=True)
                break

            # Wall-clock times of each part of the step (epoch seconds), for lining a recording of the screen up with
            # the trace: when the look began, when the planner was asked and answered, when the action ran. Not
            # rounded: the dyn checks order these against the injector's change times, and a write rounded up past a
            # change that fired half a millisecond later read as a write after it (#22, CI).
            times = {"t_obs_start": time.time()}
            try:
                obs = driver.observe()
            except DriverUnavailable as exc:
                # A read that failed past the driver's own retries: looked at again a few times, as its own count and
                # not as a step, when it is the kind that passes (deskmind_hands/errors.py). It ended a D1 run in the
                # app between two correct appends (09-30).
                if classify(str(exc)) is not Kind.TRANSIENT or observe_failures >= cfg.max_observe_failures:
                    raise
                observe_failures += 1
                time.sleep(1.0)
                continue
            observe_failures = 0
            if steps and steps[-1].get("deferred") and digests:
                # The last step waited for the user and did nothing: its unchanged screen is not a stall. A run died
                # as "4 identical observations" while someone typed elsewhere and each step politely waited.
                digests.pop()
            digests.append(obs.digest())
            # Tell the model when its last action left the screen untouched. Without this the loop stays silent
            # until the window is full and then kills the run, so every planner we tried -- remote ones, DeskMind Brain,
            # our own local model -- re-picked the same element until the budget ran out. A "worked" result from the driver
            # is not evidence of progress: clicking an already-focused editor succeeds and changes nothing.
            # Forget the withdrawals only when something actually worked. Clearing them on any digest change was
            # right for the mock desktop and useless on a real one: a live accessibility tree flaps between 12 and
            # 13 elements, so the digest differs every single turn and every withdrawal was erased before it could
            # be applied -- a window that could not be focused stayed on the menu and was chosen 188 times.
            if history and history[-1].result_ok and len(digests) >= 2 and digests[-1] != digests[-2]:
                ineffective.clear()
            if len(digests) >= 2 and digests[-1] == digests[-2] and steps and not steps[-1].get("deferred") \
                    and steps[-1].get("kind") != "done_blocked":
                last = steps[-1]
                if history:
                    history[-1].changed = False
                # Only a *successful* no-op withdraws the element. A rejected action says nothing about the target:
                # typing into the editor failed because Files was frontmost, and withdrawing the editor body would
                # have made the task unsolvable once focus was fixed.
                eid = ((last.get("action") or {}).get("binding") or {}).get("element_id")
                if eid and last.get("ok") and eid not in ineffective:
                    ineffective.append(eid)
                what = last.get("describe") or (last.get("action") or {}).get("kind") or "that action"
                repeats = 1
                while repeats < len(digests) and digests[-1 - repeats] == digests[-1]:
                    repeats += 1
                if last.get("ok") is False:
                    # The driver's own words are the best hint available and they were only reaching the model
                    # flattened into one history line; a rejection usually names the precondition to fix.
                    notice = (f"{what} was rejected: {last.get('detail') or 'no reason given'}. "
                              f"Satisfy that precondition first -- bringing the right application to the front, "
                              f"selecting the right item, or taking a fresh observation -- and then retry it.")
                else:
                    notice = (f"The screen is unchanged after {what}"
                              f"{f' (repeated {repeats} times)' if repeats > 1 else ''}. That step achieved nothing: "
                              f"either it was already in the requested state and you should move on to the next part "
                              f"of the goal, or the target was wrong. Do not choose it again.")
            elif len(digests) >= 2 and digests[-1] != digests[-2] and history and history[-1].changed is None:
                history[-1].changed = True
            # The same action, three times running, while the screen keeps changing. Byte-identical observations
            # never catch this: cmd+shift+N in Finder creates another folder every time, so every observation
            # differed and the run spent its whole budget making twelve folders instead of naming the first one.
            # Repetition is the signal, not stillness.
            # Twice is already worth saying for an action that changes the world; scrolling and waiting are
            # legitimately repeated, so those get the longer rope.
            # Steps left undone while the user was busy are not repetitions: nothing happened.
            acted = [st for st in steps if not st.get("deferred")]
            need = 3 if (acted and (acted[-1].get("action") or {}).get("kind") in ("scroll", "wait")) else 2
            if len(acted) >= need:
                sig = [((st.get("action") or {}).get("kind"),
                        ((st.get("action") or {}).get("binding") or {}).get("element_id"),
                        (st.get("action") or {}).get("text"),
                        tuple((st.get("action") or {}).get("keys") or ())) for st in acted[-need:]]
                role = next(((e.role or "").lower() for e in obs.elements if e.id == sig[0][1]), "")
                toggled = (os.environ.get("HANDS_TOGGLE_REPEAT_OK") == "1" and sig[0][0] == "click"
                           and role in ("checkbox", "switch", "axcheckbox"))
                if len(set(sig)) == 1 and sig[0][0] and toggled:
                    # Opt-in (the gym): a switch clicked twice is back where it started -- the second click undid
                    # the first, it was not a no-op. Withdrawing the switch left no way to set it right again: a
                    # settings run turned the asked-for switch on, off again, and then could not reach it.
                    label = acted[-1].get("target_label") or sig[0][1]
                    notice = (f"You clicked {label!r} {need} times in a row: each click flips it, so the second one "
                              f"undid the first. Check its current value against the goal before clicking it again.")
                elif len(set(sig)) == 1 and sig[0][0]:
                    what = acted[-1].get("describe") or sig[0][0]
                    notice = (f"You have done {what} {need} times in a row. Each one worked, and the goal is still "
                              f"not met, so repeating it is not the way forward -- act on what it produced, or "
                              f"choose a different step. Do not repeat it again.")
                    # Only the element is withdrawn, never the chord. An element is a target that can simply be
                    # wrong; a chord is a command whose *repetition* was wrong, and taking cmd+shift+N off the menu
                    # pushed the planner onto cmd+N, which opened a second window and sent the name into it.
                    if sig[0][1] and sig[0][1] not in ineffective:
                        ineffective.append(sig[0][1])
                    # A window or app that was switched to twice without effect is withdrawn like an element: it
                    # names a place, and the place is not working. A chord is not withdrawn -- it may be the only
                    # route, and taking it away once pushed a planner onto a worse one.
                    if sig[0][0] in ("focus_window", "focus_app") and sig[0][2]:
                        if sig[0][2] not in ineffective:
                            ineffective.append(sig[0][2])
            # Back and forth: A, B, A, B. No single action repeats, so the repetition check never fires, and one
            # run switched between answer.txt and log.txt eight times without writing anything.
            if len(acted) >= 4:
                sig4 = [((st.get("action") or {}).get("kind"), (st.get("action") or {}).get("text"),
                         ((st.get("action") or {}).get("binding") or {}).get("element_id")) for st in acted[-4:]]
                if sig4[0] == sig4[2] and sig4[1] == sig4[3] and sig4[0] != sig4[1] and sig4[0][0]:
                    notice = (f"You have gone back and forth between {acted[-2].get('describe')} and "
                              f"{acted[-1].get('describe')} twice. What you went to one side for is already "
                              f"known -- act where you are now instead of switching again.")
            # Going round in circles: each of the last twelve steps is one this run has already taken three times
            # or more. The pairwise back-and-forth check above missed a three-app cycle (switch to the music app,
            # type a CSV row into its search box, switch to TextEdit, switch to Safari, ...) that ran 160 steps.
            sigs = [(((st.get("action") or {}).get("kind")), (st.get("action") or {}).get("text"),
                     st.get("target_label") or ((st.get("action") or {}).get("binding") or {}).get("element_id"))
                    for st in acted if st.get("action")]
            if is_cycling(sigs):
                state = RunState.ERRORED
                failure = Failure(FailureClass.NO_PROGRESS_LOOP,
                                  "the last 12 steps all repeat steps taken three or more times", auto=True)
                break
            if no_progress(digests, window=cfg.no_progress_window):
                state = RunState.ERRORED
                failure = Failure(FailureClass.NO_PROGRESS_LOOP,
                                  f"{cfg.no_progress_window} identical observations", auto=True)
                break
            if recorder:
                recorder.observation(obs, save_image=cfg.save_screenshots)

            ctx = TurnContext(task=task, observation=_filter(obs, cfg.channels),
                              history=history[-10:], channels=cfg.channels,
                              # The planner's own questions and their answers. A harness approval is not one (G16):
                              # no training state ever had one in user_answers, and its yes is no answer to the goal.
                              dialogue=[(d["question"], d["reply"]) for d in dialogue if d.get("by") != "harness"],
                              notice=notice, ineffective=tuple(ineffective), run_id=run_id, step=len(steps) + 1,
                              asked=planner_questions)
            notice = None

            t0 = time.perf_counter()
            times["t_decide_start"] = time.time()
            proposal = adapter.propose(ctx)
            times["t_decide_end"] = time.time()
            # The requests this decision took, by id and the form their options went in: what joins this step to the
            # server's log (protocol: Request identity). Kept with the step's times, so every record of it has them.
            sent = getattr(adapter, "sent", None)
            if sent:
                times["requests"] = list(sent)
            # What the model answered, head by head, with its full probabilities -- before any rule here acted on it.
            # Always kept (no screen text in it): the record a calibration study reads (deskmind#59, E1).
            replies = getattr(adapter, "replies", None)
            if replies and recorder is not None:
                recorder._write({"t": "answers", "n": len(steps) + 1, "replies": list(replies)})
            # The exact planner request (state + questions as sent), for building offline probes from real
            # failures. Opt-in: HANDS_LOG_REQUESTS=1; sandbox tasks only, since the state carries screen text.
            req = getattr(adapter, "last_request", None)
            if req is not None and recorder is not None and os.environ.get("HANDS_LOG_REQUESTS") == "1":
                recorder._write({"t": "request", "n": len(steps) + 1, "body": req})
                adapter.last_request = None
            # What the planner was told from earlier runs (lessons.py), once per run and whether or not requests are
            # logged: a run that went wrong with a lesson in front of it is the evidence a lesson is judged by.
            recalled = getattr(adapter, "_recalled", None)
            if recorder and recalled and recalled[1] and not lessons_logged:
                recorder._write({"t": "lessons", "n": len(steps) + 1, "texts": list(recalled[1])})
                lessons_logged = True
            m.model_latency_s += time.perf_counter() - t0
            m.input_tokens += proposal.input_tokens
            m.output_tokens += proposal.output_tokens
            m.cost_usd += proposal.cost_usd or 0.0

            if proposal.parse_error:
                m.parse_errors += 1
                steps.append({"n": len(steps) + 1, "obs": obs.id, "parse_error": proposal.parse_error,
                              "raw": proposal.raw_text[:2000], **times})
                if recorder:
                    recorder.step(steps[-1])
                if m.parse_errors >= 3:
                    state = RunState.ERRORED
                    failure = Failure(FailureClass.ACTION_PARSE, proposal.parse_error, auto=True)
                    break
                notice = f"Your previous output was not a valid action: {proposal.parse_error}"
                continue

            action = proposal.action

            if action.kind in DIALOGUE:
                m.dialogue_turns += 1
                planner_questions += 1
                if planner_questions > task.budget.max_dialogue_turns:
                    state = RunState.BUDGET_EXHAUSTED
                    failure = Failure(FailureClass.BUDGET_EXHAUSTED, "dialogue turn budget exhausted", auto=True)
                    break
                reply, approve, delay = user.respond(action.text or "", action.kind is ActionKind.REQUEST_APPROVAL,
                                                     action.options)
                asked_s += delay
                if cfg.honour_user_delay and delay:
                    time.sleep(delay)
                # The decision to ask is the planner's like any other: kept with its probabilities and time, so a
                # recording's overlay can show it (the question step had none and read as the harness's doing),
                # and when the reply came, to align it with the picture.
                entry = {"n": len(steps) + 1, "obs": obs.id, "kind": action.kind.value, "question": action.text,
                         "reply": reply, "approved": approve, "delay_s": delay,
                         "latency_s": round(proposal.latency_s or 0.0, 3),
                         "decision": (proposal.raw_text or "")[:300], **times, "t_reply": time.time()}
                dialogue.append(entry)
                history.append(Turn(action.to_json(), True, f"user replied: {reply}"))
                steps.append(entry)
                if recorder:
                    recorder.step(entry)
                continue

            if action.kind is ActionKind.DONE and done_blocks < cfg.max_done_blocks and done_check.enabled():
                # Not finished while something the goal's own words ask for is visibly left (done_check.py): the
                # planner is told what, once or twice, and goes on.
                objection = getattr(adapter, "done_objection", None)
                why = objection(ctx) if objection else None
                if why:
                    done_blocks += 1
                    entry = {"n": len(steps) + 1, "obs": obs.id, "kind": "done_blocked", "text": why,
                             "decision": (proposal.raw_text or "")[:300], **times}
                    steps.append(entry)
                    history.append(Turn({"kind": "done"}, False, f"not finished: {why}"))
                    notice = f"Not finished yet: {why}"
                    if recorder:
                        recorder.step(entry)
                    continue
            if action.kind in TERMINAL:
                state = RunState.COMPLETED if action.kind is ActionKind.DONE else RunState.GAVE_UP
                steps.append({"n": len(steps) + 1, "obs": obs.id, "kind": action.kind.value,
                              "text": action.text, "latency_s": round(proposal.latency_s or 0.0, 3),
                              "decision": (proposal.raw_text or "")[:300], **times,
                              **({"goal_met": _goal_met(task, workspace, driver, sentinels)}
                                 if cfg.track_goal else {})})
                if recorder:
                    recorder.step(steps[-1])
                break

            # Injections fire between decision and execution, so the world can
            # change underneath an action the model already committed to.
            fire_changes()
            for idx, inj in _due_injections(task, m.actions, fired):
                fired.add(idx)
                if inj.event in ("cancel", "pause", "restart"):
                    cancel_requested = inj.event == "cancel"
                    entry = {"n": len(steps) + 1, "injection": inj.event, "params": inj.params}
                    steps.append(entry)
                    if recorder:
                        recorder.step(entry)
                else:
                    res = driver.inject(inj.event, inj.params)
                    entry = {"n": len(steps) + 1, "injection": inj.event,
                             "params": inj.params, "ok": res.ok, "detail": res.detail}
                    steps.append(entry)
                    if recorder:
                        recorder.step(entry)

            if cancel_requested and action.is_mutating:
                # Cancellation is enforced here, not by asking the model to stop.
                state = RunState.CANCELLED
                steps.append({"n": len(steps) + 1, "cancelled_before": action.describe()})
                if recorder:
                    recorder.step(steps[-1])
                break

            # A write or a commit the model was not sure enough of (pipeline.policy, deskmind#63 part 2) is not carried
            # out. The first time, the planner looks again; the second time running, the user is asked, as for a risky
            # step, where there is someone to ask -- and where there is not, the run stops rather than guess.
            doubt = getattr(proposal, "unsure", None)
            doubt_key = None
            if doubt:
                # "Again" is the same step proposed straight after it was refused: another step in between, or another
                # target, and it is a first time.
                doubt_key = json.dumps({"kind": action.kind.value, "text": action.text, "keys": list(action.keys or ()),
                                         "element": action.binding.element_id if action.binding else None},
                                        ensure_ascii=False, sort_keys=True)
                again = bool(steps) and steps[-1].get("kind") == "refused_unsure" and steps[-1].get("key") == doubt_key
                if doubt_key in declined_unsure or not again or not cfg.approve_risky:
                    m.unsure_refusals += 1
                    entry = {"n": len(steps) + 1, "obs": obs.id, "kind": "refused_unsure", "text": doubt,
                             "key": doubt_key, "action": action.to_json(),
                             "decision": (proposal.raw_text or "")[:300], **times}
                    steps.append(entry)
                    if recorder:
                        recorder.step(entry)
                    history.append(Turn(action.to_json(), False, f"not done: {doubt}"))
                    if again or doubt_key in declined_unsure:
                        # The planner's own doubt, said twice with nobody to ask -- or about a step the user already
                        # turned down: a planning outcome, kept in its own words.
                        state = RunState.GAVE_UP
                        failure = Failure(FailureClass.PLANNING,
                                          (f"the user said no to this step: {doubt}" if doubt_key in declined_unsure
                                           else f"not sure enough to act, twice running: {doubt}"), auto=True)
                        break
                    notice = (f"That step was not done: {doubt}. Look at the screen again and choose again; if you "
                              f"still cannot be sure, ask the user.")
                    continue
            what = (risk.risky(action, obs, last_typed_label, renames=cfg.confirm_renames)
                    if (cfg.approve_risky or cfg.refuse_risky) else None)
            if doubt:
                # The second time running, the user decides -- told what the doubt is, whether or not the step is
                # risky in itself.
                what = f"{what or action.describe()} ({doubt})"
            # An approval is for one step, in one app: approving one "click 'Send'" once covered every later Send of
            # the run, whatever it sent and in whichever app (10-02 review). The one step it also covers is the
            # confirmation it opens itself ("Delete" -> the dialog's "Delete message"): the very next step, of the
            # same kind in the same app, with a dialog up that was not up before. Anything else is asked again:
            # "same kind, same app, within two steps" let approving "delete A" cover deleting B after a step in
            # between (protocol review, 10-06).
            app_now = obs.focused_app or task.app or ""
            if just_approved:
                # Never a step with a doubt of its own: an approval covers its confirmation, not a guess.
                covered = bool(what) and not doubt and just_approved.covers(what, app_now, len(steps), obs)
                just_approved = None   # one chance: the next step is its confirmation, or nothing is
                if covered:
                    what = None
            if what and not cfg.approve_risky:
                # Nobody to ask: the step is not carried out, and the planner is told why (10-02 review: a CLI run
                # without --ask sent and deleted unasked).
                entry = {"n": len(steps) + 1, "kind": "refused_risky", "question": what, "by": "harness"}
                steps.append(entry)
                if recorder:
                    recorder.step(entry)
                history.append(Turn(action.to_json(), False, f"not done: {what} needs a person's approval, and this "
                                                              f"run has nobody to ask"))
                notice = (f"{what} was not done: it needs a person's approval and there is nobody to ask in this run. "
                          f"Do not try it another way; finish with what is done.")
                continue
            if what:
                # The harness asks, not the planner: see risk.py. A denial is the user's answer, not a failure --
                # the step is not carried out and the planner is told so.
                reply, ok_, delay = user.respond(risk.question(what, obs.focused_app or task.app or "", task.goal),
                                                 True)
                m.dialogue_turns += 1
                asked_s += delay
                approval = risk.Approval(what=what, kind=risk.kind(what), app=app_now, step=len(steps) + 1,
                                         observation_id=obs.id, action=action.to_json(),
                                         dialog_before=risk.dialog_up(obs))
                entry = {"n": len(steps) + 1, "kind": ActionKind.REQUEST_APPROVAL.value, "question": what,
                         "reply": reply, "approved": ok_, "delay_s": delay, "by": "harness", **approval.record()}
                dialogue.append(entry)
                steps.append(entry)
                if recorder:
                    recorder.step(entry)
                if not ok_:
                    if doubt_key:
                        declined_unsure.add(doubt_key)
                    history.append(Turn(action.to_json(), False, f"the user did not approve this ({what}); it was "
                                                                  f"not done"))
                    notice = (f"The user declined: {what}. It was not done. Do not try it another way; finish with "
                              f"what is done, or stop if nothing else is left to do.")
                    continue
                just_approved = approval
            times["t_act_start"] = time.time()
            res: ExecResult = driver.execute(action)
            times["t_act_end"] = time.time()
            res.effect, res.evidence = classify_effect(res)
            # Where the action landed, on the screen (after it ran: a generic control is placed while acting).
            screen_rect = getattr(driver, "screen_rect", None)
            target_rect = screen_rect(action.binding.element_id) if screen_rect and action.binding else None
            if action.kind is ActionKind.TYPE_TEXT and res.ok:
                last_typed_label = _target_label(obs, action)
            elif action.is_mutating:
                last_typed_label = None
            if action.is_mutating and not res.deferred:
                m.actions += 1
            if res.indeterminate:
                m.indeterminate_actions += 1
            if res.stale:
                m.stale_refusals += 1
                notice = ("The screen changed after your last observation, so that action was "
                          "refused. Take a fresh observation before acting.")
            elif res.deferred:
                notice = ("That step was not carried out: the user was using their Mac and it waited for them. "
                          "Nothing changed. Take it again when it is still the right step.")
            elif not res.ok:
                m.failed_actions += 1
                # An action that fails costs nothing but time, and only a *mutating* action counts against the
                # action budget -- so a non-mutating one that keeps failing runs until the wall clock. One run
                # spent 1,202 seconds and 241 turns on `focus_window` failing identically every time, with the
                # action count still reading zero. Repeated failure is a terminal condition of its own.
                consecutive_failures += 1
                if consecutive_failures >= cfg.max_consecutive_failures:
                    state = RunState.ERRORED
                    failure = Failure(FailureClass.NO_PROGRESS_LOOP,
                                      f"{consecutive_failures} actions in a row failed; last: {res.detail[:120]}",
                                      auto=True)
                    steps.append({"n": len(steps) + 1, "obs": obs.id, "action": action.to_json(),
                                  "describe": action.describe(), "ok": False, "detail": res.detail, **times})
                    if recorder:
                        recorder.step(steps[-1])
                    break
            if res.ok:
                consecutive_failures = 0
            # Actions that change nothing are not counted by the action budget, so a planner switching between two
            # windows ran 160 switches until the wall clock stopped it. A long enough run of them is a stall.
            # Only an action that did something resets the count: a refused cmd+c every fifth step kept a planner
            # switching between two windows forever, because a failed mutating action still counted as activity.
            if action.is_mutating and res.ok and not res.indeterminate:
                idle_actions = 0
            elif res.deferred:
                pass
            else:
                idle_actions += 1
                if idle_actions >= cfg.max_idle_actions:
                    state = RunState.ERRORED
                    failure = Failure(FailureClass.NO_PROGRESS_LOOP,
                                      f"{idle_actions} actions in a row changed nothing (last: {action.describe()})",
                                      auto=True)
                    break

            entry = {"n": len(steps) + 1, "obs": obs.id, "action": action.to_json(),
                     "describe": action.describe(), "ok": res.ok, "detail": res.detail,
                     "stale": res.stale, "unsupported": res.unsupported,
                     "indeterminate": res.indeterminate,
                     "effect": res.effect, "evidence": res.evidence, "deferred": res.deferred,
                     "latency_s": round(proposal.latency_s or 0.0, 3),
                     # Recorded per step, not just summed into the run, because
                     # the question "was that slow turn big or was the endpoint
                     # having a bad minute" is unanswerable from a total. A run
                     # once showed a 271s turn and the trace could not say
                     # whether we sent 2k tokens or 50k.
                     "input_tokens": proposal.input_tokens,
                     "output_tokens": proposal.output_tokens,
                     "target_label": _target_label(obs, action),
                     # What the planner said, including a terminal answer the adapter overrode: without it a run that
                     # "never said DONE" and one whose DONE was held back look the same in the trace.
                     "decision": (proposal.raw_text or "")[:300], **times}
            if target_rect:
                entry["target_rect"] = target_rect
            if cfg.track_goal:
                entry["goal_met"] = _goal_met(task, workspace, driver, sentinels)
            steps.append(entry)
            history.append(Turn(action.to_json(), res.ok, res.detail, effect=res.effect))
            if recorder:
                recorder.step(entry)
            fire_changes(states_only=True)

    except AdapterUnavailable as exc:
        # The model or its provider dropped out mid-run. Reported as availability
        # so an outage never reads as a weak model.
        state = RunState.ERRORED
        failure = Failure(FailureClass.PROVIDER_UNAVAILABLE, str(exc), auto=True)
        # The request that failed, as its questions' kinds and sizes (not the screen's text): a refusal can then be
        # told apart from an outage, and a question out of bounds named, from the run's trace alone.
        if recorder:
            from ..adapters.systemone import request_shape
            recorder._write({"t": "request_failed", "n": len(steps) + 1, "error": str(exc),
                             "questions": request_shape(getattr(adapter, "last_request", None))})
    except Exception as exc:  # noqa: BLE001 - any harness crash must be visible, not silent
        state = RunState.ERRORED
        text = f"{type(exc).__name__}: {exc}"
        # A locked screen is the machine, not the harness: the second half of a two-repeat round died this way
        # the moment the user stepped away, and all thirteen runs read as harness bugs.
        # So is an accessibility tree that stays incomplete through every retry: a sheet or popover with no
        # window identity of its own is a state of the app, not a fault in this harness.
        # Which is decided in one place (deskmind_hands/errors.py); a read that stayed transient past every retry is
        # the app's state, not a bug here.
        cls = (FailureClass.ENVIRONMENT if classify(text) in (Kind.ENVIRONMENT, Kind.TRANSIENT)
               else FailureClass.HARNESS_BUG)
        failure = Failure(cls, text, auto=True)
    finally:
        # How each injected dialog was answered, or that it was left open -- and an open one closed, however the
        # run ended (an interrupt included): a dialog left up would sit on the user's screen.
        if injector is not None:
            try:
                injector.close()
            except Exception:  # noqa: BLE001 - bookkeeping must not fail a run
                pass

    m.total_s = time.perf_counter() - t_start
    m.user_wait_s = asked_s + float(getattr(driver, "user_wait_s", 0.0) or 0.0)

    try:
        final_driver_state = driver.state()
    except Exception:  # noqa: BLE001 - bookkeeping must not fail a run
        final_driver_state = {}
    ctxg = GradeContext(workspace=workspace.ws, vars=task.vars,
                        clipboard=driver.clipboard(), driver_state=final_driver_state,
                        run={"state": state.value, "metrics": m.to_json(), **_run_info(task, workspace, recorder)})
    g = run_grader(task, ctxg, sentinel_digests=sentinels)

    if g.error:
        failure = Failure(FailureClass.HARNESS_BUG, f"grader error: {g.error}", auto=True)
    elif failure.cls is FailureClass.NONE and not g.strict:
        if g.violations:
            failure = Failure(FailureClass.FORBIDDEN_SIDE_EFFECT, "; ".join(g.violations), auto=True)
        elif state is RunState.COMPLETED:
            failure = Failure(FailureClass.FALSE_COMPLETION,
                              "agent declared done but checkpoints failed", auto=True)
        elif state is RunState.CANCELLED:
            failure = Failure(FailureClass.NONE, "cancelled by injection")
        else:
            failure = Failure(FailureClass.PLANNING, f"ended in state {state.value}")

    try:
        usage = adapter.usage() or {}
    except Exception:  # noqa: BLE001 - never let bookkeeping fail a run
        usage = {}
    return RunResult(run_id=run_id, task_id=task.id, system_label=cfg.system_label,
                     state=state, grade=g, failure=failure, metrics=m,
                     steps=steps, dialogue=dialogue, adapter_usage=usage,
                     driver_state=final_driver_state)


def _filter(obs: Observation, channels: frozenset[str]) -> Observation:
    """Withhold channels the configuration does not grant.

    The grader keeps full access; only the model's view narrows. This is the
    mechanism behind the capability ablation -- and the reason a GUI-only track
    can be claimed honestly instead of merely asked for in a prompt.
    """
    if "ax" in channels and "screenshot" in channels:
        return obs
    return Observation(
        id=obs.id,
        geometry=obs.geometry,
        transform=obs.transform,
        screenshot_png=obs.screenshot_png if "screenshot" in channels else None,
        screenshot_path=obs.screenshot_path if "screenshot" in channels else None,
        elements=obs.elements if "ax" in channels else [],
        focused_app=obs.focused_app,
        layout_version=obs.layout_version,
        ts=obs.ts,
    )
