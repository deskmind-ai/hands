"""A browser's own text fields, told apart from the page's: the address bar and any other field of a window that holds a
web page but sits outside it. Found through accessibility by structure, not by any browser's name or wording.

Typed into, the address bar sends what is typed to a website: G18b, copying a parts table into a file, typed a row
into Safari's and pressed Return, and the row went to a search engine (09-30, twice). The driver refuses that unless
the run has judged that the text belongs there (runtime/judge.py; peekaboo.py, browser_field).

Measured 09-30: Safari's address bar is found and the fields of the page it shows are not; TextEdit and Preview have
none. Chrome shows no web area to accessibility until a client turns its tree on, so its address bar is not found --
a known gap; the background input hands relies on is WebKit's anyway.
"""
from __future__ import annotations

TEXT_ROLES = {"AXTextField", "AXComboBox", "AXSearchField", "AXTextArea"}
#: Address bars by their accessibility identifier: one is the browser's own field whether or not the window shows a
#: page yet -- a start page or a blank tab has no web area, and its address bar went unguarded (deskmind#73, live run).
ADDRESS_BAR_IDS = {"WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD"}
MAX_NODES = 4000


def _get(el, attr):
    from ApplicationServices import AXUIElementCopyAttributeValue
    err, v = AXUIElementCopyAttributeValue(el, attr, None)
    return v if err == 0 else None


def _frame(el) -> tuple[float, float, float, float] | None:
    from ApplicationServices import AXValueGetValue, kAXValueCGPointType, kAXValueCGSizeType
    pos, size = _get(el, "AXPosition"), _get(el, "AXSize")
    if pos is None or size is None:
        return None
    ok1, p = AXValueGetValue(pos, kAXValueCGPointType, None)
    ok2, s = AXValueGetValue(size, kAXValueCGSizeType, None)
    return (p.x, p.y, s.width, s.height) if ok1 and ok2 else None


def fields_outside_page(pid: int) -> list[tuple[float, float, float, float]]:
    """Screen frames of a browser's own text fields: those in a window holding a web page (an AXWebArea) but not
    inside it, and an address bar known by its identifier (ADDRESS_BAR_IDS) in any window, with a page or not."""
    return [f for f, _ in _own_fields(pid)]


def address_bars(pid: int) -> list[tuple[float, float, float, float]]:
    """Screen frames of the address bars known by their identifier, in every window of the app."""
    return [f for f, known in _own_fields(pid) if known]


def own_fields(pid: int, frame: tuple | None = None, wid=None) -> list[tuple[tuple[float, float, float, float], bool]]:
    """The browser-own fields of the window at `frame` (the window the run observes; `wid`, its window-server id) as
    (screen frame, known address bar): one walk for both questions. Another window's fields are not this window's
    options."""
    return _own_fields(pid, frame, wid) if frame is not None else []


def focused_identifier(pid: int, frame: tuple | None = None, wid=None) -> str | None:
    """The accessibility identifier of the app's focused element: where a paste would land. With `frame`, only an
    element of the window at `frame` counts: every window's address bar has the same identifier, so a focus left in
    another window (the user's own) must not read as this window's address bar. That reads as "another window"."""
    from ApplicationServices import AXUIElementCreateApplication
    el = _get(AXUIElementCreateApplication(pid), "AXFocusedUIElement")
    if el is None:
        return None
    if frame is not None:
        win = _get(el, "AXWindow")
        if win is None or not _window_at(win, frame) or _the_window(pid, frame, wid) is None:
            return "another window"
        own = _window_id(win)
        if wid is not None and own is not None and str(own) != str(wid):
            return "another window"     # the user's window at the same place and size: the focus is in theirs
        if wid is not None and own is None and len(_windows_at(pid, frame)) > 1:
            return "another window"     # two at that frame, and which one has the focus cannot be read
    return _get(el, "AXIdentifier")


def _window_at(win, frame) -> bool:
    f = _frame(win)
    return bool(f) and all(abs(a - b) <= 2 for a, b in zip(f, frame))


def _window_id(win) -> int | None:
    """The window-server id of an accessibility window (the private but long-stable _AXUIElementGetWindow), or None
    when it cannot be read."""
    try:
        import ctypes
        import objc
        fn = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")._AXUIElementGetWindow
        fn.argtypes, fn.restype = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)], ctypes.c_int32
        out = ctypes.c_uint32(0)
        return int(out.value) if fn(objc.pyobjc_id(win), ctypes.byref(out)) == 0 and out.value else None
    except Exception:   # noqa: BLE001 -- unknown, never a guess
        return None


def _windows_at(pid: int, frame: tuple) -> list:
    from ApplicationServices import AXUIElementCreateApplication
    return [w for w in _get(AXUIElementCreateApplication(pid), "AXWindows") or []
            if not _get(w, "AXMinimized") and _window_at(w, frame)]


def _the_window(pid: int, frame: tuple | None, wid=None):
    """The one window of the app at `frame`, or None: none there, or two on top of each other (same place, same size),
    where a frame names no window -- the check read the other's page, and its address bar passed for this one's
    (review). A minimised window is no candidate: the window a run observes is on screen. With `wid` (the run's
    window-server id) two at one frame are told apart by their ids -- a user's window zoomed to fill the screen shares
    its frame with the run's -- and a window whose id is readable and another is never taken."""
    if frame is None:
        return None
    wins = _windows_at(pid, frame)
    if wid is not None:
        ids = [_window_id(w) for w in wins]
        if any(i is not None for i in ids):
            mine = [w for w, i in zip(wins, ids) if i is not None and str(i) == str(wid)]
            return mine[0] if len(mine) == 1 else None
    return wins[0] if len(wins) == 1 else None


def _own_fields(pid: int, frame: tuple | None = None, wid=None) -> list[tuple[tuple[float, float, float, float], bool]]:
    from ApplicationServices import AXUIElementCreateApplication
    out: list = []
    if frame is None:
        wins = _get(AXUIElementCreateApplication(pid), "AXWindows") or []
    else:
        one = _the_window(pid, frame, wid)
        wins = [one] if one is not None else []
    for win in wins:
        fields, has_page, seen = [], False, 0
        stack = [(win, False)]
        while stack and seen < MAX_NODES:
            el, in_page = stack.pop()
            seen += 1
            role = _get(el, "AXRole")
            if role == "AXWebArea":
                has_page, in_page = True, True
            elif role in TEXT_ROLES and not in_page:
                f = _frame(el)
                if f:
                    fields.append((f, _get(el, "AXIdentifier") in ADDRESS_BAR_IDS))
            stack.extend((c, in_page) for c in (_get(el, "AXChildren") or []))
        out += fields if has_page else [x for x in fields if x[1]]
    return out


def at(frames, x: float, y: float) -> bool:
    return any(fx <= x <= fx + fw and fy <= y <= fy + fh for fx, fy, fw, fh in frames)


#: What sits over a browser window without being the browser's own controls: a sheet or a dialog (a site's sign-in
#: prompt, a download question) and a popover. Its buttons are the next step, as the page's are.
OVERLAY_ROLES = {"AXSheet", "AXPopover"}
OVERLAY_SUBROLES = {"AXDialog", "AXSystemDialog"}


def _clip(a, b) -> tuple | None:
    """The part of frame `a` inside frame `b`, or None when they do not overlap."""
    x, y = max(a[0], b[0]), max(a[1], b[1])
    r, btm = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return (x, y, r - x, btm - y) if r > x and btm > y else None


def regions(pid: int, frame: tuple | None, wid=None) -> dict | None:
    """Where the window at `frame` (the window the run observes; `wid`, its window-server id) shows the page, and what
    else in it is not the browser's own: {"window": its frame, "page": the visible part of each web area, "overlay":
    sheets', dialogs' and popovers' frames, "address_bar": the address bars' frames, "menus": menus the browser's own
    controls opened}, all screen frames. Everything else inside the window -- the toolbar, the tab bar, the sidebar with
    the user's tab groups, bookmarks and reading list -- is the browser's own (chrome). None when no single window is
    at `frame` (_the_window). By structure only: roles and the address bar's identifier, never a word on screen.

    A web area's frame is the whole document, not what is shown: scrolled, it starts above the window and covers the
    toolbar. The page is the web area clipped to the scroll area holding it (the viewport) and to the window (review of
    de228eb).

    Live 10-17 (0.4.3-rc.3, run 4): in its own window, still on its marker page, a run asked to copy a table from a page
    open in the user's window clicked 「显示边栏」 and double-clicked the sidebar's 「默认，含1个标签页的标签页组」 -- the
    user's tab groups, which open in the window clicked and can be rearranged."""
    win = _the_window(pid, frame, wid)
    if win is None:
        return None
    wf = _frame(win) or tuple(frame)
    out: dict = {"window": wf, "page": [], "overlay": [], "address_bar": [], "menus": []}
    # Each node with the viewport it is shown in: the nearest scroll area's frame, else the window's.
    stack, seen = [(c, wf) for c in reversed(_get(win, "AXChildren") or [])], 0
    while stack and seen < MAX_NODES:
        el, view = stack.pop()
        seen += 1
        role = _get(el, "AXRole")
        f = _frame(el)
        if role == "AXWebArea":
            shown = _clip(f, view) if f else None
            shown = _clip(shown, wf) if shown else None
            if shown:
                out["page"].append(shown)
            continue        # what is inside is the page's: not walked
        if role in OVERLAY_ROLES or _get(el, "AXSubrole") in OVERLAY_SUBROLES:
            if f:
                out["overlay"].append(f)
            continue        # likewise an overlay's
        if role == "AXMenu":
            if f:
                out["menus"].append(f)     # opened from the browser's own controls (the tab group picker, say)
            continue
        if role in TEXT_ROLES and _get(el, "AXIdentifier") in ADDRESS_BAR_IDS and f:
            out["address_bar"].append(f)
        inner = (_clip(f, view) or f) if role == "AXScrollArea" and f else view
        stack.extend((c, inner) for c in reversed(_get(el, "AXChildren") or []))
    return out


def _overlap(a, b) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return min(ax + aw, bx + bw) > max(ax, bx) and min(ay + ah, by + bh) > max(ay, by)


#: How far an element's frame is shrunk before it is tested: a control whose edge only touches the page (a toolbar
#: button flush with the page's top) is not on it.
INSET = 3


def _inset(rect) -> tuple:
    x, y, w, h = rect
    dx, dy = min(INSET, max(0, (w - 1) / 2)), min(INSET, max(0, (h - 1) / 2))
    return (x + dx, y + dy, w - 2 * dx, h - 2 * dy)


def chrome(r: dict, rect: tuple) -> bool:
    """Whether the screen rectangle `rect` (x, y, w, h: an element's) is one of the browser's own controls in the
    window `r` describes (regions): on a menu the browser's own controls opened, or inside the window and touching none
    of its page, overlays or address bar. Any part on the page keeps an element the page's: a row scrolled half under
    the toolbar is still the page's. A rect wholly outside the window (a menu the page opened) is not the window's
    chrome. The rect is shrunk by INSET first."""
    rect = _inset(rect)
    if any(_overlap(rect, m) for m in r.get("menus", [])):
        return True
    return (_overlap(rect, r["window"])
            and not any(_overlap(rect, f) for f in r["page"] + r["overlay"] + r["address_bar"]))


#: The most page text read for a judgement (runtime/judge.py shows less): a result is near the top of a page.
PAGE_TEXT_CHARS = 4000


def page_info(pid: int, frame: tuple | None, wid=None) -> dict | None:
    """The web page shown by the window at `frame` (the window the run observes, from the window server): {"url",
    "title", "text"}, the text being the page's static text in order. None when that window holds no web page, or no
    window is at that frame -- never another window's page: read from the app's front window, the check judged the
    user's own Safari window while the run worked in another (deskmind#73, live run). Read for the judgement whether
    a browser task's result is on the page (runtime/judge.py), never to decide anything here."""
    win = _the_window(pid, frame, wid)
    if win is None:
        return None
    stack, seen, page = [win], 0, None
    while stack and seen < MAX_NODES:
        el = stack.pop()
        seen += 1
        if _get(el, "AXRole") == "AXWebArea":
            page = el
            break
        stack.extend(reversed(_get(el, "AXChildren") or []))
    if page is None:
        return None
    url = _get(page, "AXURL")
    url = str(url.absoluteString()) if hasattr(url, "absoluteString") else (str(url) if url else "")
    parts, size, stack, seen = [], 0, [page], 0
    while stack and seen < MAX_NODES and size < PAGE_TEXT_CHARS:
        el = stack.pop()
        seen += 1
        if _get(el, "AXRole") == "AXStaticText":
            v = str(_get(el, "AXValue") or "").strip()
            if v:
                parts.append(v)
                size += len(v) + 1
        stack.extend(reversed(_get(el, "AXChildren") or []))
    return {"url": url, "title": str(_get(win, "AXTitle") or _get(page, "AXTitle") or ""),
            "text": " ".join(parts)[:PAGE_TEXT_CHARS]}


def main(argv=None) -> int:
    """Print regions() for every window of an app on screen, as frames only (no title, no page text), for checking
    against a real window: python -m deskmind_hands.drivers.axchrome [bundle id, default Safari]."""
    import json
    import sys
    from AppKit import NSRunningApplication
    from ApplicationServices import AXUIElementCreateApplication
    bundle = (argv if argv is not None else sys.argv[1:] or ["com.apple.Safari"])[0]
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle) or []
    if not apps:
        print(f"{bundle} is not running")
        return 1
    pid = apps[0].processIdentifier()
    for w in _get(AXUIElementCreateApplication(pid), "AXWindows") or []:
        f = _frame(w)
        if not f or _get(w, "AXMinimized"):
            continue
        wid = _window_id(w)
        r = regions(pid, f, wid)
        rounded = None if r is None else {k: ([round(n) for n in v] if k == "window" else
                                              [[round(n) for n in x] for x in v]) for k, v in r.items()}
        print(json.dumps({"window_id": wid, "frame": [round(n) for n in f], "regions": rounded}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
