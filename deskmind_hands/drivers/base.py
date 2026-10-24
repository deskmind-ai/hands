"""What the harness requires of any way of driving a desktop.

The same task file must run against the mock desktop, a Peekaboo-driven host and
a VM guest with no edits. That is only possible if this interface stays narrow
and every driver reports its own capabilities honestly rather than pretending to
support an action it silently drops.
"""

from __future__ import annotations

import hashlib
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..actions import Action
from ..geometry import ImageTransform, Rect, ScreenGeometry


@dataclass
class Element:
    """One node of the structured (accessibility) channel."""

    id: str
    #: Semantic role as the driver names it ("textField", "button").
    role: str
    #: Raw accessibility role ("AXTextArea"). Kept separate because the two
    #: vocabularies disagree -- Peekaboo calls the TextEdit body a "textField"
    #: while AX calls it an "AXTextArea" -- and collapsing them makes element
    #: selection silently miss its target.
    ax_role: str = ""
    label: str = ""
    rect: Rect | None = None
    value: str | None = None
    focused: bool = False
    enabled: bool = True
    #: True when text can be written here. The single most useful hint for
    #: "where does typing go", and it comes free from the accessibility tree.
    settable: bool = False
    app: str = ""
    window_id: str = ""
    #: The choices of a dropdown, by label.
    options: list[str] = field(default_factory=list)
    #: A control the driver presents that is not on screen: a desktop capability projected as the web control
    #: a planner already knows ("move to" as a dropdown). Always reported apart from what the GUI offered.
    synthetic: bool = False
    #: Where the element's label (and, for a pixel element, its place) comes from: "ax" (the accessibility tree,
    #: and the controls projected over it) or "pixels" (a reading of the screenshot: OCR words, grounded icons and
    #: the generic controls offered for an app read from its screenshot -- vision.py -- and an unnamed tree button
    #: whose name was grounded from the image -- grounding.name_unnamed). A pixel label is not what the app reports.
    source: str = "ax"
    #: The accessibility role description, localized ("组", "按钮"), when the app gave one; and whether the label is
    #: only that -- the element has no name of its own (an empty group reads as its role).
    role_description: str = ""
    label_from_role: bool = False

    def to_json(self) -> dict:
        d = {"id": self.id, "role": self.role, "label": self.label,
             "app": self.app, "window_id": self.window_id}
        if self.options:
            d["options"] = list(self.options)
        if self.synthetic:
            d["synthetic"] = True
        if self.source != "ax":
            d["source"] = self.source
        if self.ax_role and self.ax_role != self.role:
            d["ax_role"] = self.ax_role
        if self.settable:
            d["settable"] = True
        if self.rect:
            d["rect"] = [round(self.rect.x, 1), round(self.rect.y, 1),
                         round(self.rect.w, 1), round(self.rect.h, 1)]
        if self.value is not None:
            d["value"] = self.value
        if self.focused:
            d["focused"] = True
        if not self.enabled:
            d["enabled"] = False
        return d


@dataclass
class WindowRef:
    """A sibling window of the observed application.

    The driver pins one window and observes only that one, which is what stops
    a second document quietly becoming the target. The cost of the pin was that
    the other windows stopped existing as far as the model was concerned, so a
    task staging two documents in one app had no reachable move. Listing them
    here is the half of the fix that makes ``focus_window`` addressable: an id
    the model can name, with enough title to tell which is which.
    """

    id: str
    title: str = ""
    #: The one currently being observed and acted on.
    active: bool = False
    on_screen: bool = True

    def to_json(self) -> dict:
        d = {"id": self.id, "title": self.title}
        if self.active:
            d["active"] = True
        if not self.on_screen:
            d["on_screen"] = False
        return d


@dataclass
class Observation:
    id: str
    geometry: ScreenGeometry
    transform: ImageTransform
    screenshot_png: bytes | None = None
    screenshot_path: Path | None = None
    elements: list[Element] = field(default_factory=list)
    focused_app: str = ""
    #: What the window calls itself. In a file manager this is the current
    #: folder -- the "where am I" signal an agent needs to navigate, and the one
    #: it was never given.
    window_title: str = ""
    #: Every window of the focused application, including the observed one.
    windows: list[WindowRef] = field(default_factory=list)
    #: A modal alert or sheet is up. Nothing else in the application answers while it is, and the planner cannot
    #: infer that from a list of elements -- a run left Finder holding "the name docs is already taken" and every
    #: later window operation, including closing, failed with "Finder is busy" until a human clicked OK.
    dialog: bool = False
    #: What the driver knows about this observation that the element list cannot say: that the accessibility tree
    #: was cut short (element, depth or time limit), so controls and text may be missing from it.
    notes: list[str] = field(default_factory=list)
    #: Whether a key chord can reach this window now. False with no window open (nothing has the keys) and for an app
    #: read from its pixels (hands/vision.py), whose input only goes through a click's or a field's foreground flash:
    #: offered anyway, KEY was refused every time -- G18b pressed cmd+f twice in the music app just launched (10-01).
    accepts_keys: bool = True
    #: No window of the app could be observed: none is open, none this run may use, or the one read showed nothing
    #: (no element at all, or a ghost window: peekaboo._read_nothing). The elements are then not a
    #: window's, and a DONE on this observation is judged rather than taken (runtime/loop.py): read as an empty
    #: screen, it ended a web search "completed" with nothing searched (deskmind#73, live 10-17).
    no_window: bool = False
    #: The window observed is the browser window the run opened for itself, and no web page is open in it yet (it
    #: still shows the run's own empty start page). Whatever page the user has open is in a window of theirs, which
    #: the run may not use. Said in `notes` too; ASK is offered on it (adapters/systemone.py) -- a run with nothing
    #: to work on clicked the browser's sidebar twelve times instead (0.4.3-rc.3, live 10-17).
    own_window_empty: bool = False
    layout_version: int = 0
    ts: float = field(default_factory=time.time)

    def digest(self) -> str:
        """Content hash used for no-progress detection.

        Uses the structured channel when available and the screenshot bytes
        otherwise, so the check works for screenshot-only configurations too.
        """
        h = hashlib.sha256()
        if self.elements:
            for e in self.elements:
                h.update(repr(e.to_json()).encode("utf-8"))
        elif self.screenshot_png:
            h.update(self.screenshot_png)
        h.update(self.focused_app.encode("utf-8"))
        # Switching windows changes nothing in the element list of a driver that
        # observes only the pinned one, so without this a focus_window would
        # read as a no-progress repeat of the previous turn.
        for w in self.windows:
            h.update(f"{w.id}:{w.active}".encode("utf-8"))
        return h.hexdigest()


@dataclass
class ExecResult:
    ok: bool
    detail: str = ""
    stale: bool = False        # the action referred to a view of the world that has moved
    unsupported: bool = False  # this driver genuinely cannot do it -- not a model failure
    #: Dispatched, but the driver could not verify where it landed. Neither a
    #: confirmed success nor a confirmed failure, and flattening it into either
    #: is a mistake with a direction: calling it success invites false
    #: completion, calling it failure invites a retry that does the work twice.
    #: Finder's inline rename field is the case in point -- it belongs to a
    #: window we cannot observe, so the write is only verifiable by its
    #: consequence. The platform models these three states; so should we.
    indeterminate: bool = False
    #: What is known of the action's effect (cua's action-result contract): confirmed (read back, or the window
    #: changed), partial, unverifiable (dispatched, nothing to check it by), suspected_noop (nothing on screen
    #: changed), refused. Empty until classified; the loop fills it in with classify_effect.
    effect: str = ""
    #: How the effect is known, in a few words: "value read back", "menu value", "window unchanged".
    evidence: str = ""
    #: Not carried out because the user was using their Mac (the driver waited for them and gave up): nothing was
    #: done, so it is neither the planner's failure nor a sign the run is stuck. Set from the driver's words
    #: (USER_BUSY) when not given.
    deferred: bool = False
    #: The files this action wrote on disk, as (absolute path, SHA-256 hex of the content written, or None): the
    #: receipt the execution layer above credits a screen action's files by (a save-as's target, a scripted save's
    #: document). The digest is of the content itself -- the bytes the driver wrote, or the text the app holds --
    #: never of the file read back from disk, which another program may have rewritten meanwhile (PR #16 review,
    #: tenth and eleventh rounds). No digest: the path is told but cannot be credited. Only files the driver knows
    #: it wrote; a driver that cannot tell leaves it empty. A scratch file of the driver's own (the staged 未命名-N
    #: under .hands/) is never one of these.
    files: tuple[tuple[str, str | None], ...] = ()
    #: A refusal's own words, kept when the step had already sent something (then `detail` is one fixed sentence):
    #: for the trace only -- nothing turns it into guidance, so no "not done" in it can reach a planner.
    refused_reason: str = ""
    #: Whether any input reached the app: False only where the driver knows none was sent -- a refusal before acting
    #: (stale, unsupported, waited out for the user, or words marked NothingSent). None: not known, which the layer
    #: above takes as "may have happened". Never False on a guess: an action that may have gone out stays unknown.
    performed: bool | None = None

    def __post_init__(self):
        if not self.ok and (self.detail or "").lower().startswith(USER_BUSY):
            self.deferred = True
        if isinstance(self.detail, MaybeSent):
            self.indeterminate = True
        if not self.ok and self.performed is None and not self.indeterminate and (
                self.stale or self.unsupported or self.deferred or isinstance(self.detail, NothingSent)):
            # The one place a refusal becomes "nothing done": inside a step that sent nothing. Outside any step
            # nothing is claimed (performed stays unknown: fail-safe). A refusal after something went in this step
            # (a fall-through, a refusal after a focus write) is unverified, and nothing of it says "not done" any
            # more: no stale / deferred / unsupported flag, no "nothing was done" words -- a reader of either would
            # take it again (review of hands#64 round 8: the hands loop said "Nothing changed. Take it again").
            if not in_step():
                pass
            elif sent_in_step():
                # the words are replaced whole, not filtered: any part of a refusal ("the user is active -- wait",
                # "not done: ...") read by a planner says "take it again" (review of hands#64 round 9)
                self.refused_reason = str(self.detail)
                self.indeterminate, self.stale, self.deferred, self.unsupported = True, False, False, False
                self.detail = MaybeSent(SENT_THEN_STOPPED)
            else:
                self.performed = False


#: How every driver says a step was left undone because the user was busy.
USER_BUSY = "the user is active"


# -- What a step sent to the user's apps, counted where it is sent (review of hands#64 round 7) -------------------------
#
# Every primitive that can reach an app counts itself BEFORE it calls the OS: an event post, an accessibility attribute
# write or action, a scripted command that tells the app to do something. Counting at the hand-on points of each route
# missed one send after another (the background route, the accessibility insertion, the focus write); counted here,
# no route can forget. The one thing that takes a count back is an OS answer that definitely means "not delivered"
# (AX_NOT_DELIVERED); anything else -- success, a timeout, kAXErrorCannotComplete, an exception -- stays counted.
# Bringing an app forward and making a window its key window are not counted (the definition at NothingSent).
#
# The count belongs to one step: Driver.execute runs inside input_scope(), and only there does a NothingSent refusal
# (or a stale / unsupported / user-busy one) become performed:false -- and only while nothing was counted in it
# (ExecResult.__post_init__, the one place).

#: Accessibility errors that mean the write or action never reached the app: an invalid argument or element, an
#: attribute or action the element does not have, not implemented, the accessibility API disabled. Nothing else --
#: kAXErrorFailure, kAXErrorCannotComplete (a timeout) and any unknown code may have been delivered.
AX_NOT_DELIVERED = frozenset({
    -25201,   # kAXErrorIllegalArgument
    -25202,   # kAXErrorInvalidUIElement
    -25205,   # kAXErrorAttributeUnsupported (includes not settable)
    -25206,   # kAXErrorActionUnsupported
    -25208,   # kAXErrorNotImplemented
    -25211,   # kAXErrorAPIDisabled
})

class _Step:
    """One step's count of sends (and how deep it is inside uncounted()), and `by`: the last moment it may send for its
    caller (base.act_by of the caller's deadline; None: no caller deadline, `hands do`)."""
    __slots__ = ("n", "quiet", "by", "releasing", "pressed")

    def __init__(self, by: float | None = None):
        self.n, self.quiet, self.by, self.releasing = 0, 0, by, 0
        #: what is held down now (a key, a mouse button), with how to let it go: never left held, whatever ends the step
        self.pressed: dict = {}


class StepExpired(BaseException):
    """A send asked for after the step's caller's deadline (less the margin): not made. Raised from the counted
    primitive itself, so no route can send late (review of hands#64: the chat click and the address bar sent after
    their checks); a BaseException, so no route's broad `except Exception` carries on past it. Driver.execute turns
    it into the step's answer -- nothing done if nothing went yet, else the fixed sent-then-stopped result."""


#: The step in progress, per thread (a context variable: a new thread starts with none). A send on another thread,
#: or another thread's step, never touches this one's count (review of hands#64 round 8: one process-wide dict).
_STEP: ContextVar[_Step | None] = ContextVar("hands_step", default=None)


def counted(call, refused=None):
    """Count a send in the step in progress, then make it: `call()`. `refused(result)` True only for an answer that
    definitely means it was not delivered (then the count is taken back). An exception leaves it counted. Inside
    uncounted(), or outside any step (nothing to count it in), it is just made."""
    st = _STEP.get()
    if st is None or st.quiet:
        return call()
    if st.by is not None and not st.releasing and time.time() > st.by:
        # checked right before the OS call: no NEW input reaches an app late. The completion of input already started
        # (an up for a down sent, a release) is never held back: it goes inside releasing() (review of hands#64,
        # Codex: a key-up blocked by the deadline left the key held)
        raise StepExpired()
    st.n += 1
    result = call()
    if refused is not None and refused(result):
        st.n -= 1
    return result


def ax_counted(call):
    """An accessibility write or action (returns an AXError code): counted unless the code is in AX_NOT_DELIVERED."""
    return counted(call, refused=lambda err: err in AX_NOT_DELIVERED)


class releasing:
    """What runs inside completes input already started (a key-up, a mouse-up) or cleans up after it: counted, never
    refused by the deadline."""

    def __enter__(self):
        self._st = _STEP.get()
        if self._st is not None:
            self._st.releasing += 1
        return self

    def __exit__(self, *exc):
        if self._st is not None:
            self._st.releasing -= 1
        return False


def note_pressed(key, let_go) -> None:
    """`key` (a key code, a mouse button) is going down in the step in progress -- told before the first OS call that
    could deliver it; `let_go(in_place=False)` sends its up (`in_place`: without moving the pointer)."""
    st = _STEP.get()
    if st is not None:
        st.pressed[key] = let_go


def note_released(key) -> None:
    st = _STEP.get()
    if st is not None:
        st.pressed.pop(key, None)


def release_all(in_place: bool = False) -> None:
    """Let go of everything the step in progress still holds down (its downs whose ups did not go: an exception, a
    stop, the deadline between them). Called first in every cleanup that gives something back (the front, the focus,
    the pointer, the clipboard, the input source), so the ups go where the downs went; and once more as the step ends,
    whatever ended it, `in_place` -- the last resort, after the pointer may have been put back, never moving it."""
    st = _STEP.get()
    if st is None:
        return
    for key, let_go in list(st.pressed.items()):
        try:
            with releasing():
                let_go(in_place=in_place)
        except Exception:  # noqa: BLE001 -- one that cannot be let go must not keep the others held
            pass
        st.pressed.pop(key, None)


class uncounted:
    """What runs inside is no send: a read, or bringing an app or a window forward (NothingSent's definition)."""

    def __enter__(self):
        self._st = _STEP.get()
        if self._st is not None:
            self._st.quiet += 1
        return self

    def __exit__(self, *exc):
        if self._st is not None:
            self._st.quiet -= 1
        return False


class input_scope:
    """One step: its sends are counted in it (Driver.execute, the executor around it, a runtime around it), and none is
    made after `by` (the caller's deadline less the margin: act_by). Entered inside another step's scope on the same
    thread, it is that step: the count is shared, never reset; the earlier `by` holds."""

    def __init__(self, by: float | None = None):
        self._by = by

    def __enter__(self):
        cur = _STEP.get()
        self._token = _STEP.set(_Step(self._by)) if cur is None else None
        if cur is not None and self._by is not None:
            cur.by = self._by if cur.by is None else min(cur.by, self._by)
        return self

    def __exit__(self, *exc):
        if self._token is not None:
            _STEP.reset(self._token)
        return False


def step_expired_result() -> "ExecResult":
    """The answer of a step stopped by StepExpired: nothing done if it had sent nothing, else (the enforcement in
    ExecResult) the fixed sent-then-stopped one."""
    return ExecResult(False, NothingSent("the caller's deadline came before the step could act; nothing was done"))


def time_left() -> float | None:
    """Seconds until the step in progress may no longer send (None: no deadline): what a preparation wait may take."""
    st = _STEP.get()
    return None if st is None or st.by is None else st.by - time.time()


def in_step() -> bool:
    """Whether a step's scope is open on this thread."""
    return _STEP.get() is not None


def sends_so_far() -> int:
    """How many sends the step in progress has made so far (0 outside a step): for a route that compares before and
    after its own sending."""
    st = _STEP.get()
    return st.n if st is not None else 0


def sent_in_step() -> int:
    """How many sends the step in progress made (0 outside a step)."""
    return sends_so_far()


#: The whole detail of a refusal that came after a send in the same step (its own words go to refused_reason)
SENT_THEN_STOPPED = ("input already went to the app in this step; the outcome is unknown -- do not repeat it without "
                     "checking: observe the window first")


class NothingSent(str):
    """A driver's words for a refusal made before any input was sent (ExecResult.performed becomes False). Words built
    from them by formatting are plain strings again: unknown, the safe side.

    What "nothing was sent" means (the coordinator's decision, review of hands#64 rounds 6 and 8): nothing went to the
    app that could change its content or lasting state -- no click, key, paste, typing, accessibility write, no
    accessibility focus moved into a field, no scripted command, no document or URL opened, no app launched, and no
    reopen (`open -b` sends one: it un-minimises windows, and opens a window on an app with none -- so the drivers use
    it only for an app with no window on screen). Bringing a running app with a window on screen to the front without
    a reopen (NSRunningApplication activation, AXFrontmost, AppleScript `activate`), or making a window its key
    window, and giving that back, does not count: a refusal after only those is still "nothing was sent"."""


class MaybeSent(str):
    """A driver's words for a step stopped after some of its input went (a click and a paste, then a refused key):
    neither done nor not done -- ExecResult.indeterminate, so the reply is unverified."""

#: Seconds kept before a caller's deadline for the action itself and what is read after it (the executor's window and
#: document reads, the reply): a step that has not started acting by then does nothing. HANDS_DEADLINE_MARGIN_S
#: overrides it.
DEADLINE_MARGIN_S = 20.0


def act_by(deadline: float | None) -> float | None:
    """The last moment a step may start acting for a caller that gives up at `deadline` (epoch seconds); None when
    the caller set no deadline (`hands do`)."""
    if deadline is None:
        return None
    return deadline - float(os.environ.get("HANDS_DEADLINE_MARGIN_S", DEADLINE_MARGIN_S))


#: Where a driver's wait for the user is told. None (the CLI, `hands do`): the plain lines on stdout it has always
#: printed, `HANDS_WAIT {json}` then `HANDS_RESUME`, which the app's runner reads. The executor sets a function
#: (kind, body) that writes a JSON event line beside its replies, under the same lock: a plain line there was not
#: JSON, and harness dropped it, so a run waiting for the user never showed "paused".
_WAIT_SINK = None


def set_wait_sink(sink) -> None:
    """Send the waits for the user to `sink(kind, body)` (kind "user_wait" or "user_resumed"); None: the plain lines."""
    global _WAIT_SINK
    _WAIT_SINK = sink


def tell_wait(kind: str, body: dict) -> None:
    """Say that a step waits for the user ("user_wait", with reason, app and what) or that the wait is over
    ("user_resumed")."""
    sink = _WAIT_SINK
    if sink is not None:
        sink(kind, body)
        return
    import json
    if kind == "user_wait":
        print("HANDS_WAIT " + json.dumps(body, ensure_ascii=False), flush=True)
    else:
        print("HANDS_RESUME", flush=True)


EFFECTS = ("confirmed", "partial", "unverifiable", "suspected_noop", "refused")


def classify_effect(res: "ExecResult") -> tuple[str, str]:
    """(effect, evidence) for a result that does not say, from its flags and the driver's own words.

    "confirmed" only where something was read back or seen; a dispatched input with nothing to check it by is
    "unverifiable" -- a DONE after one of those is checked again (the pixel digest missed a song pausing, and the run
    said DONE over it)."""
    if res.effect:
        return res.effect, res.evidence
    low = (res.detail or "").lower()
    if res.stale:
        return "refused", "stale observation"
    if res.unsupported:
        return "refused", "unsupported"
    if "nothing on screen changed" in low or "nothing changed" in low or "(suspected_noop)" in low:
        return "suspected_noop", "window unchanged"
    if not res.ok:
        return "refused", ""
    if res.indeterminate or "could not be confirmed" in low or "(unverifiable)" in low or "not verified" in low:
        return "unverifiable", "not read back"
    if "verified" in low or "(confirmed)" in low:
        return "confirmed", "menu value" if low.startswith("chose") else "value read back"
    if "in the background" in low and ("entered" in low or "pasted" in low):
        return "unverifiable", "pasted, not read back"
    return "confirmed", "reported by the driver"


class DriverUnavailable(RuntimeError):
    """The driver cannot start here (missing binary, permission, VM)."""


@runtime_checkable
class Driver(Protocol):
    name: str

    def capabilities(self) -> set[str]: ...
    def start(self, workspace: Path) -> None: ...
    def observe(self) -> Observation: ...
    def execute(self, action: Action) -> ExecResult: ...
    def state(self) -> dict: ...
    def clipboard(self) -> str | None: ...
    def inject(self, event: str, params: dict) -> ExecResult: ...
    def close(self) -> None: ...


def env_sections() -> bool:
    """Whether the planner's state carries an `environment` section: today's date and the attached folder with its files
    (docs/harness-eval.md; the research's "environment context"). Off unless HANDS_ENV_SECTIONS=1, for the reason
    effect_notes is: G18b and earlier never saw it. For G19 on."""
    import os
    return os.environ.get("HANDS_ENV_SECTIONS", "0") == "1"


def mark_new() -> bool:
    """Whether elements that were not in the previous observation of the same window carry `"new": true` (browser-use
    marks them `*[id]`): a popover's rows, a dialog's buttons, a field that appeared. Off unless HANDS_MARK_NEW=1."""
    import os
    return os.environ.get("HANDS_MARK_NEW", "0") == "1"


def effect_notes() -> bool:
    """Whether the planner is shown the effect notes: an action's unconfirmed effect in its history, the state's
    last_effect and its "check before finishing" note, and the new-window and focus-guard remarks on a result.
    Off unless HANDS_EFFECT_NOTES=1: G14-G18b never saw them, and their re-tests and shoots must get the state they
    were trained on. On from G19, whose data is collected with them. The trace records the effect either way."""
    import os
    return os.environ.get("HANDS_EFFECT_NOTES", "0") == "1"
