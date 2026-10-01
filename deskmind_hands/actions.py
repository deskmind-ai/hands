"""The unified action space every adapter must produce.

Models speak different dialects; the harness executes exactly one vocabulary.
Every action carries a *binding* to the observation it was decided from, so the
executor can refuse to act on a stale view of the screen -- the failure mode
behind task R11 (window moved between screenshot and click).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .geometry import CoordSpace, Point


class ActionKind(str, Enum):
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    TYPE_TEXT = "type_text"
    KEY = "key"
    SCROLL = "scroll"
    DRAG = "drag"
    WAIT = "wait"
    SCREENSHOT = "screenshot"
    FOCUS_APP = "focus_app"
    #: Move to another window of the *same* application. Separate from
    #: FOCUS_APP because the driver pins one window per app, and switching
    #: between two documents of one app was otherwise unreachable: the only
    #: way to drop the pin was to leave for a different app and come back,
    #: which lands on whichever window the platform happens to pick.
    FOCUS_WINDOW = "focus_window"
    #: Drive the menu bar by path. Some destinations have no on-screen
    #: control to click and no key chord that is stable across locales --
    #: Finder's "go to the enclosing folder" being the case that cost us
    #: four graded runs.
    MENU = "menu"
    ASK_USER = "ask_user"
    REQUEST_APPROVAL = "request_approval"
    DONE = "done"
    GIVE_UP = "give_up"


#: Actions that change the world. Everything else is observation or dialogue.
MUTATING = frozenset({
    ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK,
    ActionKind.TYPE_TEXT, ActionKind.KEY, ActionKind.SCROLL, ActionKind.DRAG,
    ActionKind.MENU,
})

#: Actions that end the episode.
TERMINAL = frozenset({ActionKind.DONE, ActionKind.GIVE_UP})

#: Actions that hand control back to the user and pause the step budget.
DIALOGUE = frozenset({ActionKind.ASK_USER, ActionKind.REQUEST_APPROVAL})

_MODIFIERS = {"cmd", "command", "ctrl", "control", "alt", "option", "shift", "fn"}
_CANONICAL_MOD = {"command": "cmd", "control": "ctrl", "option": "alt"}


class ActionError(ValueError):
    """The model emitted something that is not a legal action."""


@dataclass(frozen=True)
class Binding:
    """What this action was decided against, and what it expects to still be true."""

    observation_id: str | None = None
    app: str | None = None
    window_id: str | None = None
    element_id: str | None = None


@dataclass
class Action:
    kind: ActionKind
    point: Point | None = None
    to_point: Point | None = None          # drag endpoint
    coord_space: CoordSpace = CoordSpace.MODEL_IMAGE
    text: str | None = None
    #: type_text only. True means "set the field to this text", false means
    #: "insert at the caret". The distinction is not cosmetic: a real driver can
    #: verify the first by reading the value back and cannot verify the second,
    #: and an unverifiable write is how an agent reports done after typing into
    #: the wrong window.
    clear_first: bool = False
    #: Ask for the foreground. Background delivery does not move keyboard focus,
    #: so an agent that needs a field focused has to say so -- and stealing focus
    #: is user-visible disruption, which makes it something to record and count
    #: rather than a hidden default.
    foreground: bool = False
    keys: tuple[str, ...] = ()
    scroll_dx: int = 0
    scroll_dy: int = 0
    duration_s: float = 0.0
    binding: Binding = field(default_factory=Binding)
    #: For ASK_USER: the answers the question lists (the lines it found ambiguous), offered to the user to pick from.
    options: tuple[str, ...] = ()
    raw: Any = None                        # untouched model output, for the trace

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        k = self.kind
        if k in {ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK}:
            if self.point is None and self.binding.element_id is None:
                raise ActionError(f"{k.value} needs a point or an element_id")
        elif k is ActionKind.DRAG:
            if self.point is None or self.to_point is None:
                raise ActionError("drag needs both a start and an end point")
        elif k is ActionKind.TYPE_TEXT:
            if self.text is None:
                raise ActionError("type_text needs text (use empty string deliberately)")
        elif k is ActionKind.KEY:
            if not self.keys:
                raise ActionError("key needs at least one key")
        elif k is ActionKind.SCROLL:
            if self.scroll_dx == 0 and self.scroll_dy == 0:
                raise ActionError("scroll needs a non-zero delta")
        elif k is ActionKind.WAIT:
            if self.duration_s <= 0:
                raise ActionError("wait needs a positive duration")
        elif k is ActionKind.FOCUS_APP:
            if not self.text:
                raise ActionError("focus_app needs an app name or bundle id")
        elif k is ActionKind.FOCUS_WINDOW:
            if not self.text:
                raise ActionError("focus_window needs a window id from the window list")
        elif k is ActionKind.MENU:
            if not self.text:
                raise ActionError("menu needs a path such as 'File > Save As...'")
        elif k in DIALOGUE:
            if not self.text:
                raise ActionError(f"{k.value} needs the question or the proposal text")

    @property
    def is_mutating(self) -> bool:
        return self.kind in MUTATING

    def describe(self) -> str:
        """Short human-readable form, used in traces and approval prompts."""
        k = self.kind.value
        if self.point is not None:
            k += f" @({self.point.x:.0f},{self.point.y:.0f})"
        if self.binding.element_id:
            k += f" #{self.binding.element_id}"
        if self.text is not None:
            preview = self.text if len(self.text) <= 40 else self.text[:37] + "..."
            k += f" {preview!r}"
        if self.keys:
            k += " " + "+".join(self.keys)
        if self.kind is ActionKind.SCROLL:
            k += f" ({self.scroll_dx},{self.scroll_dy})"
        return k

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind.value}
        if self.point:
            d["point"] = list(self.point.as_tuple())
        if self.to_point:
            d["to_point"] = list(self.to_point.as_tuple())
        if self.point or self.to_point:
            d["coord_space"] = self.coord_space.value
        if self.text is not None:
            d["text"] = self.text
            if self.clear_first:
                d["clear_first"] = True
        if self.foreground:
            d["foreground"] = True
        if self.keys:
            d["keys"] = list(self.keys)
        if self.scroll_dx or self.scroll_dy:
            d["scroll"] = [self.scroll_dx, self.scroll_dy]
        if self.duration_s:
            d["duration_s"] = self.duration_s
        if self.options:
            d["options"] = list(self.options)
        b = self.binding
        if any((b.observation_id, b.app, b.window_id, b.element_id)):
            d["binding"] = {kk: vv for kk, vv in b.__dict__.items() if vv}
        return d


def normalise_keys(spec: str | list[str]) -> tuple[str, ...]:
    """Parse 'cmd+shift+s' or ['Command','S'] into canonical lowercase keys."""
    parts = re.split(r"[+\s]+", spec) if isinstance(spec, str) else list(spec)
    out: list[str] = []
    for p in parts:
        p = p.strip().lower()
        if not p:
            continue
        out.append(_CANONICAL_MOD.get(p, p))
    if not out:
        raise ActionError(f"no keys parsed from {spec!r}")
    mods = [k for k in out if k in _MODIFIERS or k in _CANONICAL_MOD.values()]
    rest = [k for k in out if k not in mods]
    if len(rest) > 1:
        raise ActionError(f"key chord has more than one non-modifier key: {out}")
    return tuple(mods + rest)


def from_json(data: dict[str, Any], *, default_space: CoordSpace = CoordSpace.MODEL_IMAGE) -> Action:
    """Parse the generic JSON action dialect. Adapters for models with their own
    output format parse natively and construct ``Action`` directly."""
    if not isinstance(data, dict):
        raise ActionError(f"action must be an object, got {type(data).__name__}")
    kind_raw = data.get("kind") or data.get("action") or data.get("type")
    if not kind_raw:
        raise ActionError(f"action has no kind: {data!r}")
    try:
        kind = ActionKind(str(kind_raw).strip().lower())
    except ValueError as exc:
        raise ActionError(f"unknown action kind {kind_raw!r}") from exc

    def _pt(key: str) -> Point | None:
        v = data.get(key)
        if v is None:
            return None
        if isinstance(v, dict):
            return Point(float(v["x"]), float(v["y"]))
        if isinstance(v, (list, tuple)) and len(v) == 2:
            return Point(float(v[0]), float(v[1]))
        raise ActionError(f"{key} must be [x,y] or {{x,y}}, got {v!r}")

    space = default_space
    if "coord_space" in data:
        try:
            space = CoordSpace(data["coord_space"])
        except ValueError as exc:
            raise ActionError(f"unknown coord_space {data['coord_space']!r}") from exc

    scroll = data.get("scroll") or [0, 0]
    binding_raw = data.get("binding") or {}
    return Action(
        kind=kind,
        point=_pt("point"),
        to_point=_pt("to_point"),
        coord_space=space,
        text=data.get("text"),
        clear_first=bool(data.get("clear_first", False)),
        foreground=bool(data.get("foreground", False)),
        keys=normalise_keys(data["keys"]) if data.get("keys") else (),
        scroll_dx=int(scroll[0]),
        scroll_dy=int(scroll[1]),
        duration_s=float(data.get("duration_s", 0.0)),
        binding=Binding(
            observation_id=binding_raw.get("observation_id"),
            app=binding_raw.get("app"),
            window_id=binding_raw.get("window_id"),
            element_id=binding_raw.get("element_id"),
        ),
        raw=data,
    )
