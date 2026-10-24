"""A text field of one window, found, written and read back through accessibility: the background typing route's
first try (an insertion at the caret, no keys) and its proof (the field's value read back), whichever way the text
went in. Found by structure and place -- a text role whose frame holds the observed element's centre, in the window
with the run's window-server id -- never by an app's name.

Measured 10-21: TextEdit takes the insertion (AXSelectedText); Chrome answers it with success and changes nothing,
which the read-back shows, and the route goes on to keys (peekaboo.py, _type_in_background).
"""
from __future__ import annotations

import time

TEXT_ROLES = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}
MAX_NODES = 4000


def _get(el, attr):
    from ApplicationServices import AXUIElementCopyAttributeValue
    err, v = AXUIElementCopyAttributeValue(el, attr, None)
    return v if err == 0 else None


def _set(el, attr, value) -> bool:
    """Every accessibility write of this module (focus, selection, inserted text): counted as sent before it is made,
    and taken back only on an answer that it never reached the app (base.ax_counted). True when accepted."""
    from ApplicationServices import AXUIElementSetAttributeValue
    from .base import ax_counted
    return ax_counted(lambda: AXUIElementSetAttributeValue(el, attr, value)) == 0


def _range(loc: int, length: int):
    from ApplicationServices import AXValueCreate, kAXValueCFRangeType
    return AXValueCreate(kAXValueCFRangeType, (loc, length))


def window(pid: int, wid: int):
    """The app's accessibility window with window-server id `wid`, or None."""
    from ApplicationServices import AXUIElementCreateApplication
    from .axchrome import _window_id
    for w in _get(AXUIElementCreateApplication(pid), "AXWindows") or []:
        if _window_id(w) == int(wid):
            return w
    return None


def field_at(pid: int, wid: int, frame: tuple[float, float, float, float]):
    """The text field of window `wid` at `frame` (screen points, x, y, w, h): the smallest element with a text role
    whose frame holds the centre of `frame`. None when there is none, or the window is not found."""
    from .axchrome import _frame
    win = window(pid, wid)
    if win is None:
        return None
    cx, cy = frame[0] + frame[2] / 2, frame[1] + frame[3] / 2
    best, best_area, seen, stack = None, None, 0, [win]
    while stack and seen < MAX_NODES:
        el = stack.pop()
        seen += 1
        if _get(el, "AXRole") in TEXT_ROLES:
            f = _frame(el)
            if f and f[0] - 1 <= cx <= f[0] + f[2] + 1 and f[1] - 1 <= cy <= f[1] + f[3] + 1:
                area = f[2] * f[3]
                if best is None or area < best_area:
                    best, best_area = el, area
        stack.extend(_get(el, "AXChildren") or [])
    return best


def value(el) -> str | None:
    v = _get(el, "AXValue")
    return None if v is None else str(v)


def _length(el) -> int:
    n = _get(el, "AXNumberOfCharacters")
    return int(n) if isinstance(n, int) else len(value(el) or "")


def select(el, replace_all: bool, wait: float = 0.3) -> bool:
    """The selection to the whole text (replace_all) or a caret at its end; True when it reads back so (within
    `wait` seconds: a web view applies it asynchronously)."""
    n = _length(el)
    want = (0, n) if replace_all else (n, 0)
    if not _set(el, "AXSelectedTextRange", _range(*want)):
        return False
    return selected(el, want, wait)


def selected(el, want: tuple[int, int], wait: float = 0.3) -> bool:
    """Whether the field's selection reads back as `want` within `wait` seconds."""
    deadline = time.monotonic() + wait
    while True:
        if _selection(el) == want:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def all_selected(el, wait: float = 0.5) -> bool:
    """Whether the whole text reads back selected."""
    return selected(el, (0, _length(el)), wait)


def _selection(el) -> tuple[int, int] | None:
    """The field's selected range as (location, length), or None."""
    got = _get(el, "AXSelectedTextRange")
    if got is None:
        return None
    from ApplicationServices import AXValueGetValue, kAXValueCFRangeType
    ok, r = AXValueGetValue(got, kAXValueCFRangeType, None)
    if not ok:
        return None
    return (int(r[0]), int(r[1])) if isinstance(r, tuple) else (int(r.location), int(r.length))


def insert(el, text: str, replace_all: bool, go=None) -> bool:
    """`text` in place of the whole text (replace_all) or at the end, by the accessibility insertion
    (AXSelectedText); True when the write was sent and accepted -- which is not proof it changed anything. False:
    nothing was written (the selection was refused, or the write, or `go()` said no after the selection's wait)."""
    return select(el, replace_all) and (go is None or bool(go())) and _set(el, "AXSelectedText", text)


def focus(el, wait: float = 0.5) -> bool:
    """The keyboard focus inside its window to the field; True when it reads back focused within `wait` seconds
    (Chrome takes some 60 ms to say so: a read at once said no, and the first typing in a field fell back)."""
    _set(el, "AXFocused", True)
    deadline = time.monotonic() + wait
    while True:
        if is_focused(el):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def is_focused(el) -> bool:
    return bool(_get(el, "AXFocused"))


def can_set(el, attr: str) -> bool:
    """Whether `attr` of the element is settable, as accessibility says (asking writes nothing)."""
    try:
        from ApplicationServices import AXUIElementIsAttributeSettable
        err, ok = AXUIElementIsAttributeSettable(el, attr, None)
        return err == 0 and bool(ok)
    except Exception:  # noqa: BLE001 -- not known: no
        return False


def role(el) -> str:
    return str(_get(el, "AXRole") or "")


def names(el) -> list[str]:
    """What the field is called: its title, description and placeholder, those it has."""
    return [str(v) for v in (_get(el, a) for a in ("AXTitle", "AXDescription", "AXPlaceholderValue")) if v]


def in_web_area(el, depth: int = 80) -> bool:
    """Whether the field is web content (an AXWebArea above it): a page keeps its own state, which an accessibility
    insertion can show in the field without changing -- the form would then submit what the page holds."""
    cur = _get(el, "AXParent")
    for _ in range(depth):
        if cur is None:
            return False
        if _get(cur, "AXRole") == "AXWebArea":
            return True
        cur = _get(cur, "AXParent")
    return False


def settled_value(el, before: str | None, wait: float = 0.6) -> str | None:
    """The field's value once it has changed from `before`, or as it is after `wait` seconds."""
    deadline = time.monotonic() + wait
    while True:
        now = value(el)
        if now != before or time.monotonic() >= deadline:
            return now
        time.sleep(0.05)


_SAME = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', " ": " ", "\r": "\n"})


def _norm(s: str) -> str:
    """Leniently: the case an app's autocapitalisation changes, curly quotes it substitutes, non-breaking spaces."""
    return (s or "").translate(_SAME).casefold()


def typed_in(before: str | None, after: str | None, text: str, replace_all: bool) -> bool:
    """Whether the read-back shows `text` went in: the value changed, and holds the text in place of what was there
    (replace_all) or once more than before (an insertion). Lenient on case and quotes: TextEdit capitalises the
    first letter of synthetic keys."""
    if after is None or after == before:
        return False
    a, t = _norm(after), _norm(text)
    if replace_all:
        return a.strip() == t.strip()
    t = t.strip()
    return not t or a.count(t) > _norm(before or "").count(t)


def window_state(pid: int, wid: int) -> dict | None:
    """What a key press in window `wid` can change that can be read back: its title, the app's windows, and the
    focused element's value and selection. None when the window cannot be read."""
    from ApplicationServices import AXUIElementCreateApplication
    from .axchrome import _window_id
    try:
        app = AXUIElementCreateApplication(pid)
        win = window(pid, wid)
        if win is None:
            return None
        el = _get(app, "AXFocusedUIElement")
        return {"title": str(_get(win, "AXTitle") or ""),
                "windows": tuple(sorted(i for i in (_window_id(w) for w in _get(app, "AXWindows") or []) if i)),
                "value": None if el is None else str(_get(el, "AXValue")),
                "selection": None if el is None else _selection(el)}
    except Exception:   # noqa: BLE001 -- unread is unknown
        return None
