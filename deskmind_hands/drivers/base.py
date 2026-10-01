"""What the harness requires of any way of driving a desktop.

The same task file must run against the mock desktop, a Peekaboo-driven host and
a VM guest with no edits. That is only possible if this interface stays narrow
and every driver reports its own capabilities honestly rather than pretending to
support an action it silently drops.
"""

from __future__ import annotations

import hashlib
import time
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

    def to_json(self) -> dict:
        d = {"id": self.id, "role": self.role, "label": self.label,
             "app": self.app, "window_id": self.window_id}
        if self.options:
            d["options"] = list(self.options)
        if self.synthetic:
            d["synthetic"] = True
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

    def __post_init__(self):
        if not self.ok and (self.detail or "").lower().startswith(USER_BUSY):
            self.deferred = True


#: How every driver says a step was left undone because the user was busy.
USER_BUSY = "the user is active"


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
