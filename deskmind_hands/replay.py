"""Replay a recorded run without the desktop or a model: what the harness would show the planner, step by step.

A run's trace (trace.jsonl + manifest.json) holds every observation the driver made and every choice the planner made.
Replaying it runs the real loop (runtime/loop.py) and the real System One adapter against a driver that hands back
the recorded observations and results, and a planner that answers each request with the recorded choice. What comes
out is every request the adapter built -- the state and questions as they would be sent -- plus the places where
the recorded choice is no longer among the options (a divergence).

That is the harness's own regression test (docs/harness-eval.md, layer 2). A change to the adapter, the loop or its
guards that alters what the planner is shown shows up as a diff of these requests; a change that takes away an
option a recorded run needed shows up as a divergence. Neither needs a desktop or a model, and the same recorded
run gives the same answer every time.

What a replay cannot cover is the driver itself: the observations are the recorded ones. Changes to what the driver
observes are tested against real apps (layer 3).

    from deskmind_hands.replay import replay
    result = replay(Path("runs/do-20260930-172539"))
"""
from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .actions import ActionKind
from .adapters.systemone import SystemOneAdapter
from .drivers.base import Element, ExecResult, Observation, WindowRef
from .geometry import ImageTransform, Rect, ScreenGeometry, Size

#: Replaying a run whose trace predates window titles and window lists (09-30) still works; its FOCUS_WINDOW options
#: are then empty, and the replay says so rather than guessing.
FULL_TRACE_KEYS = ("window_title", "windows")


def load_trace(run_dir: Path) -> tuple[dict, list[dict]]:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    records = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
               if line.strip()]
    return manifest, records


def _element(d: dict) -> Element:
    r = d.get("rect")
    return Element(id=d["id"], role=d.get("role", ""), ax_role=d.get("ax_role", ""), label=d.get("label", ""),
                   rect=Rect(*r) if r else None, value=d.get("value"), focused=bool(d.get("focused")),
                   enabled=d.get("enabled", True), settable=bool(d.get("settable")), app=d.get("app", ""),
                   window_id=d.get("window_id", ""), options=list(d.get("options") or []),
                   synthetic=bool(d.get("synthetic")))


def observation(rec: dict) -> Observation:
    """An Observation as the driver made it, from its trace record."""
    g = rec.get("geometry") or {}
    logical, pixels = Size(*g.get("logical", (1512, 982))), Size(*g.get("pixels", g.get("logical", (1512, 982))))
    t = rec.get("transform") or {}
    transform = ImageTransform(source=Size(*t.get("source", pixels.as_tuple())),
                               target=Size(*t.get("target", pixels.as_tuple())))
    return Observation(
        id=rec["id"], geometry=ScreenGeometry(logical, pixels), transform=transform,
        elements=[_element(e) for e in rec.get("elements") or []], focused_app=rec.get("focused_app", ""),
        window_title=rec.get("window_title", ""),
        windows=[WindowRef(id=w["id"], title=w.get("title", ""), active=bool(w.get("active")),
                           on_screen=w.get("on_screen", True)) for w in rec.get("windows") or []],
        dialog=bool(rec.get("dialog")), notes=list(rec.get("notes") or []),
        accepts_keys=bool(rec.get("accepts_keys", True)),
        layout_version=rec.get("layout_version", 0), ts=rec.get("ts", 0.0))


class ReplayDriver:
    """The recorded observations and results, in order. Nothing is executed."""

    name = "replay"

    def __init__(self, observations: list[Observation], results: list[dict]) -> None:
        self._obs, self._results = list(observations), list(results)
        self.observed = 0
        self.executed = 0
        self.ws = None

    def capabilities(self) -> set[str]:
        return {"ax", "screenshot"}

    def start(self, workspace: Path) -> None:
        self.ws = workspace

    def observe(self) -> Observation:
        if self.observed >= len(self._obs):
            raise ReplayExhausted(f"the loop asked for observation {self.observed + 1}; the run recorded "
                                  f"{len(self._obs)}")
        o = self._obs[self.observed]
        self.observed += 1
        return o

    def execute(self, action) -> ExecResult:
        r = self._results[self.executed] if self.executed < len(self._results) else {"ok": True, "detail": ""}
        self.executed += 1
        return ExecResult(bool(r.get("ok", True)), r.get("detail") or "", stale=bool(r.get("stale")),
                          unsupported=bool(r.get("unsupported")), indeterminate=bool(r.get("indeterminate")))

    def state(self) -> dict:
        return {}

    def clipboard(self) -> str | None:
        return None

    def inject(self, event: str, params: dict) -> ExecResult:
        return ExecResult(True, "replay: injection not replayed")

    def close(self) -> None:
        pass


class ReplayExhausted(RuntimeError):
    """The replayed loop went on past the recorded run (a guard or a stop no longer fires where it did)."""


@dataclass
class Divergence:
    step: int
    what: str

    def to_json(self) -> dict:
        return {"step": self.step, "what": self.what}


def _one(key: str) -> dict:
    return {"type": "choice", "choice": key, "confidence": 1.0, "probabilities": {key: 1.0}}


def _first(q: dict) -> dict:
    keys = list((q.get("criteria") or {}).keys())
    if q.get("type") == "bool" or not keys:
        return {"type": "bool", "choice": "no", "confidence": 1.0, "probabilities": {"yes": 0.0, "no": 1.0}}
    return _one(keys[0])


class FollowAdapter(SystemOneAdapter):
    """The real adapter, answering each request with what the recorded run chose.

    Every request it builds is kept (`requests`). When the recorded choice is not among the options it was given, a
    Divergence is noted and the nearest thing is answered so the replay can go on: the options are what is under
    test, the recorded choice is the reference."""

    def __init__(self, steps: list[dict]) -> None:
        super().__init__(model="replay", url="http://replay.invalid")
        self._steps = steps
        self.requests: list[dict] = []
        self.divergences: list[Divergence] = []
        self._n = 0

    def _ask(self, state: dict, questions: dict) -> dict:
        self.last_request = {"state": state, "model": self.model, "questions": questions}
        self.requests.append(self.last_request)
        self._n += 1
        rec = self._steps[self._n - 1] if self._n - 1 < len(self._steps) else None
        answers = {k: _first(q) for k, q in questions.items()}
        if rec is None:
            self.divergences.append(Divergence(self._n, "the replay asked past the recorded run"))
            answers["operation"] = _one("DONE") if "DONE" in (questions["operation"]["criteria"]) else answers["operation"]
            return answers
        if "goal_complete" in questions:
            # The completion check (HANDS_COMPLETION_CHECK) is answered as recorded: "yes" only on the step it ended.
            # Its first option is "yes", and answered that way every replay ended at the second step.
            answers["goal_complete"] = _one("yes" if self._recorded_stop(rec) == "check" else "no")
        want = self._recorded_operation(rec)
        ops = questions["operation"]["criteria"]
        if want not in ops:
            self.divergences.append(Divergence(self._n, f"operation {want} not offered (offered: {', '.join(ops)})"))
            return answers
        answers["operation"] = _one(want)
        self._target(rec, want, state, questions, answers)
        return answers

    @staticmethod
    def _recorded_stop(rec: dict) -> str | None:
        try:
            return json.loads(rec.get("decision") or "{}").get("stop_by")
        except ValueError:
            return None

    @staticmethod
    def _recorded_operation(rec: dict) -> str:
        try:
            op = json.loads(rec.get("decision") or "{}").get("operation")
        except ValueError:
            op = None
        if op:
            return op
        kind = (rec.get("action") or {}).get("kind") or rec.get("kind") or ""
        return {"ask_user": "ASK", "done": "DONE", "give_up": "BLOCKED", "double_click": "OPEN", "click": "CLICK",
                "focus_window": "FOCUS_WINDOW", "focus_app": "FOCUS_APP", "key": "KEY"}.get(kind, kind.upper())

    def _target(self, rec: dict, op: str, state: dict, questions: dict, answers: dict) -> None:
        action = rec.get("action") or {}
        eid = (action.get("binding") or {}).get("element_id")
        # An answer ends the run: its step has no action, and the text ("answer: ...") is on the step itself.
        text = action.get("text") if action else rec.get("text")

        def pick(key: str, value, what: str) -> None:
            crit = (questions.get(key) or {}).get("criteria") or {}
            if value in crit:
                answers[key] = _one(value)
            else:
                self.divergences.append(Divergence(self._n, f"{what} {value!r} not offered for {op}"))

        if op in ("DONE", "BLOCKED", "ASK"):
            return
        if op == "FOCUS_WINDOW":
            pick("focus_window_target", text, "window")
        elif op == "FOCUS_APP":
            pick("focus_app_target", text, "app")
        elif op == "KEY":
            pick("key_target", "+".join(action.get("keys") or ()), "chord")
        elif op == "SELECT":
            opt = next((o["index"] for e in state["elements"] if e.get("id") == eid
                        for o in e.get("options") or [] if o.get("value") == text), None)
            pick("select_target", opt, f"option {text!r} of")
        elif op == "ANSWER":
            cands = [c.get("value") for c in ((questions.get("answer_value") or {}).get("criteria") or {}).values()]
            want = (text or "")[len("answer: "):]
            key = str(cands.index(want) + 1) if want in cands else None
            pick("answer_value", key, "answer")
        else:
            index = next((e["index"] for e in state["elements"] if e.get("id") == eid), None)
            pick(op.lower() + "_target", index, f"element {eid!r} as")
            if op in ("TYPE_TEXT", "APPEND_TEXT", "RENAME", "TYPE_FOCUSED") and text is not None:
                values = {k: c.get("value") for k, c in
                          ((questions.get("type_text_value") or {}).get("criteria") or {}).items()}
                key = next((k for k, v in values.items() if v == text), None) \
                    or next((k for k, v in values.items() if v is not None and v.rstrip("\n") == text.rstrip("\n")), None)
                if key is None:
                    self.divergences.append(Divergence(self._n, f"value {text[:60]!r} not among the "
                                                                f"{len(values)} value candidates"))
                else:
                    answers["type_text_value"] = _one(key)
            elif op == "REPLACE_TEXT":
                self._replace_pair(text or "", questions, answers)

    def _replace_pair(self, result: str, questions: dict, answers: dict) -> None:
        """The (old span, new value) pair whose edit gives the recorded text; the adapter applies it."""
        olds = (questions.get("replace_from") or {}).get("criteria") or {}
        news = (questions.get("type_text_value") or {}).get("criteria") or {}
        for ko, o in olds.items():
            for kn, n in news.items():
                old, new = o.get("value"), n.get("value")
                if old and new is not None and old in result or (new and new in result):
                    answers["replace_from"], answers["type_text_value"] = _one(ko), _one(kn)
                    return
        self.divergences.append(Divergence(self._n, "no offered (old, new) pair for the recorded replacement"))


class ReplayUser:
    """The recorded replies to the run's questions, in order."""

    def __init__(self, replies: list[tuple[str, bool]]) -> None:
        self._replies = list(replies)

    def respond(self, question: str, approval: bool, options: tuple[str, ...] = ()) -> tuple[str, bool, float]:
        if not self._replies:
            return ("(the user did not answer)", False, 0.0)
        reply, approved = self._replies.pop(0)
        return (reply, approved, 0.0)


@dataclass
class ReplayResult:
    run: str
    requests: list[dict]
    divergences: list[Divergence]
    state: str
    failure: str
    recorded_state: str
    steps: int
    recorded_steps: int
    complete_trace: bool
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"run": self.run, "state": self.state, "failure": self.failure,
                "recorded_state": self.recorded_state, "steps": self.steps, "recorded_steps": self.recorded_steps,
                "complete_trace": self.complete_trace, "divergences": [d.to_json() for d in self.divergences],
                "notes": self.notes}


def replay(run_dir: Path) -> ReplayResult:
    """Replay a `hands do` run (manifest "live": true) through the real loop and adapter."""
    from .env.workspace import LiveWorkspace
    from .live import live_task
    from .runtime.loop import RunConfig, run_task

    manifest, records = load_trace(run_dir)
    if not manifest.get("live"):
        raise ValueError(f"{run_dir}: only `hands do` runs can be replayed so far")
    obs_recs = [r for r in records if r.get("t") == "obs"]
    step_recs = [r for r in records if r.get("t") == "step"]
    summary = next((r for r in reversed(records) if r.get("t") == "summary"), {})
    # Planner calls, in order: every step that came from a proposal (not the loop's own stops).
    planned = [s for s in step_recs if s.get("decision") or s.get("kind") in ("ask_user", "done")]
    results = [{"ok": s.get("ok", True), "detail": s.get("detail"), "stale": s.get("stale"),
                "unsupported": s.get("unsupported"), "indeterminate": s.get("indeterminate")}
               for s in step_recs if s.get("action")]
    replies = [(s.get("reply") or "", True) for s in step_recs if s.get("question")]
    target = Path(manifest["target"]) if manifest.get("target") else None
    app = manifest.get("app") or _first_app(obs_recs, manifest)
    budget = manifest.get("budget") or {}
    task = live_task(manifest["goal"], target, app=app, apps=manifest.get("apps") or {},
                     max_actions=budget.get("max_actions", 30), wall_clock_s=10 ** 9)
    driver = ReplayDriver([observation(r) for r in obs_recs], results)
    adapter = FollowAdapter(planned)
    scratch = Path(tempfile.mkdtemp(prefix="hands-replay-"))
    ws = LiveWorkspace(root=scratch, ws=scratch / "ws", fixtures_dir=scratch)
    (scratch / "ws").mkdir()
    notes = []
    if obs_recs and not all(k in obs_recs[0] for k in FULL_TRACE_KEYS):
        notes.append("trace predates window titles and window lists: FOCUS_WINDOW options are empty")
    # The account name is taken out of states as they are built (SystemOneAdapter._redact), by the login name of the
    # machine the adapter runs on. A replay must not depend on which machine that is: the recorded traces are
    # sanitized (the home folder reads "user"), and on a machine whose login name was "user" that label was
    # redacted a second time and the snapshot differed (hands#3). The name is pinned for the replay: the one
    # the recording names, else none.
    from .adapters import systemone
    home_name, systemone.HOME_NAME = systemone.HOME_NAME, manifest.get("home_name") or ""
    try:
        res = run_task(task, driver, adapter, ws,
                       config=RunConfig(channels=frozenset({"ax", "screenshot"}), save_screenshots=False,
                                        system_label="replay",
                                        user=ReplayUser(replies) if replies or manifest.get("ask") == "stdin" else None,
                                        approve_risky=False),
                       run_id="replay")
        state, failure, steps = res.state.value, res.failure.detail if res.failure else "", len(res.steps)
    except ReplayExhausted as exc:
        state, failure, steps = "exhausted", str(exc), driver.executed
    finally:
        systemone.HOME_NAME = home_name
    return ReplayResult(run=run_dir.name, requests=adapter.requests, divergences=adapter.divergences,
                        state=state, failure=failure, recorded_state=summary.get("state", ""),
                        steps=steps, recorded_steps=len(step_recs),
                        complete_trace=not notes, notes=notes)


def _first_app(obs_recs: list[dict], manifest: dict) -> str:
    """The app a run started in, for a manifest written before it was recorded: the first element's app, mapped
    back through the run's own apps by name."""
    apps = manifest.get("apps") or {}
    return next(iter(apps.values()), "com.apple.finder")


# ---------------------------------------------------------------- snapshots

_VOLATILE = re.compile(r"(?:obs|elem)[-_]\d+|\b\d{10}(?:\.\d+)?\b|/private/tmp/[^\s\"']+|/var/folders/[^\s\"']+")


def normalize(request: dict) -> dict:
    """A request as it is compared across runs of the replay: element ids, timestamps and temporary paths stand
    for themselves by position, so a diff shows what the planner was shown, not incidental numbering. Key order is
    kept: the order of the options and of the state is part of what the planner is shown (brain#8: the same model
    re-sorted fell from 220 to about 130 of 223 valid steps)."""
    text = json.dumps(request, ensure_ascii=False)
    ids: dict[str, str] = {}

    def sub(m: re.Match) -> str:
        v = m.group(0)
        if v.startswith(("obs", "elem")):
            ids.setdefault(v, f"<{v.split('-')[0].split('_')[0]}{len(ids) + 1}>")
            return ids[v]
        if v[0] == "/":
            return "<tmp>"
        return "<ts>"
    return json.loads(_VOLATILE.sub(sub, text))
