"""Pop-up buttons (a web page's <select>, an AppKit NSPopUpButton) read and set through accessibility, in the
background.

A <select> in Safari reads as an AXPopUpButton with its current value and no children: its options exist only while
its menu is open. So the options are read by opening the menu (AXPress), reading the AXMenu's items and closing it
again (AXCancel), and a choice is made by opening it and pressing the item. Nothing is brought forward: measured on
09-30, Safari took all of it with another app in front. WebKit puts the open menu under the window; AppKit under the
popup; both are searched.
"""
from __future__ import annotations

import time

TIMEOUT_S = 2.0      # per accessibility call: a busy app cannot stall a step
MAX_NODES = 3000     # the search for popups and their menu stops here


def _ax():
    import ApplicationServices as AS
    return AS


def _get(el, attr):
    AS = _ax()
    err, v = AS.AXUIElementCopyAttributeValue(el, attr, None)
    return v if err == 0 else None


def _walk(root, want, limit=MAX_NODES):
    """Elements under `root` whose AXRole is in `want`, breadth first, at most `limit` nodes visited."""
    out, queue, seen = [], [root], 0
    while queue and seen < limit:
        el = queue.pop(0)
        seen += 1
        role = _get(el, "AXRole")
        if role in want:
            out.append(el)
            if role == "AXMenu":
                continue
        queue.extend(_get(el, "AXChildren") or [])
    return out


def app_element(pid: int):
    AS = _ax()
    app = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app, TIMEOUT_S)
    return app


def popups(pid: int) -> list[dict]:
    """The pop-up buttons in `pid`'s windows: {"el", "title", "value", "frame": (x, y, w, h) in screen points}."""
    out = []
    for w in _get(app_element(pid), "AXWindows") or []:
        for el in _walk(w, {"AXPopUpButton"}):
            pos, size = _get(el, "AXPosition"), _get(el, "AXSize")
            frame = None
            if pos is not None and size is not None:
                import ApplicationServices as AS
                ok_p, p = AS.AXValueGetValue(pos, AS.kAXValueCGPointType, None)
                ok_s, s = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
                if ok_p and ok_s:
                    frame = (p.x, p.y, s.width, s.height)
            out.append({"el": el, "title": _get(el, "AXTitle") or _get(el, "AXDescription") or "",
                        "value": _get(el, "AXValue"), "frame": frame})
    return out


def _open_menu(popup):
    """Open `popup`'s menu and return it (None if it did not open)."""
    AS = _ax()
    AS.AXUIElementSetMessagingTimeout(popup, TIMEOUT_S)
    if AS.AXUIElementPerformAction(popup, "AXPress") != 0:
        if AS.AXUIElementPerformAction(popup, "AXShowMenu") != 0:
            return None
    for _ in range(10):
        time.sleep(0.1)
        menus = _walk(popup, {"AXMenu"}, 200)
        if not menus:
            win = _get(popup, "AXWindow") or _get(popup, "AXTopLevelUIElement")
            menus = _walk(win, {"AXMenu"}) if win is not None else []
        if menus:
            return menus[0]
    return None


def _items(menu) -> list[tuple[str, object]]:
    return [(t, m) for m in _get(menu, "AXChildren") or []
            if _get(m, "AXRole") == "AXMenuItem" and (t := (_get(m, "AXTitle") or "").strip())]


def options(popup) -> list[str]:
    """The popup's choices, read by opening its menu and closing it again without choosing."""
    menu = _open_menu(popup)
    if menu is None:
        return []
    titles = [t for t, _ in _items(menu)]
    AS = _ax()
    if AS.AXUIElementPerformAction(menu, "AXCancel") != 0:
        AS.AXUIElementPerformAction(popup, "AXPress")   # a second press closes it
    time.sleep(0.1)
    return titles


def select(popup, option: str) -> tuple[bool, str]:
    """Choose `option` in `popup`; verified by the popup's value afterwards."""
    menu = _open_menu(popup)
    if menu is None:
        return False, "the pop-up's menu did not open"
    items = _items(menu)
    hit = next((m for t, m in items if t == option), None) or \
        next((m for t, m in items if t.lower() == option.strip().lower()), None)
    AS = _ax()
    if hit is None:
        AS.AXUIElementPerformAction(menu, "AXCancel")
        return False, f"{option!r} is not one of its choices: {[t for t, _ in items]}"
    AS.AXUIElementPerformAction(hit, "AXPress")
    now = ""
    for _ in range(15):   # the page updates the value a moment after the item is pressed
        time.sleep(0.1)
        now = _get(popup, "AXValue") or _get(popup, "AXTitle") or ""
        if str(now).strip().lower() == option.strip().lower():
            return True, f"chose {option!r} (verified)"
    return False, f"chose {option!r} but it now shows {now!r}"


def match(els_frames: list[tuple[int, tuple]], found: list[dict], tol: float = 6.0,
          titles: dict[int, str] | None = None) -> dict[int, dict]:
    """Pair elements (index, screen frame) with found popups by position: {element index: popup}. Where no popup is
    within `tol` of an element, the one popup with the element's title (`titles`), if exactly one has it: the expense
    form's category matched for five steps and then not at all, silently optionless, and an oracle drive gave the
    task up (09-30) -- a page that shifts a few points must not take the choices away."""
    out = {}
    for i, frame in els_frames:
        if frame is None:
            continue
        best = min(found, key=lambda p: abs(p["frame"][0] - frame[0]) + abs(p["frame"][1] - frame[1])
                   if p["frame"] else 1e9, default=None)
        if best and best["frame"] and abs(best["frame"][0] - frame[0]) <= tol and abs(best["frame"][1] - frame[1]) <= tol:
            out[i] = best
            continue
        title = (titles or {}).get(i)
        named = [p for p in found if title and (p.get("title") or "") == title]
        if len(named) == 1:
            out[i] = named[0]
    return out
