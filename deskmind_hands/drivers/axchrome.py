"""A browser's own text fields, told apart from the page's: the address bar and any other field of a window that holds a
web page but sits outside it. Found through accessibility by structure, not by any browser's name or wording.

Typed into, the address bar sends what is typed to a website: G18b, copying a parts table into a file, typed a row
into Safari's and pressed Return, and the row went to a search engine (09-30, twice). The driver refuses that unless
the goal asks for something on the web (peekaboo.py, _browser_field_refusal).

Measured 09-30: Safari's address bar is found and the fields of the page it shows are not; TextEdit and Preview have
none. Chrome shows no web area to accessibility until a client turns its tree on, so its address bar is not found --
a known gap; the background input hands relies on is WebKit's anyway.
"""
from __future__ import annotations

TEXT_ROLES = {"AXTextField", "AXComboBox", "AXSearchField", "AXTextArea"}
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
    """Screen frames of the text fields that are in a window holding a web page (an AXWebArea) but not inside it."""
    from ApplicationServices import AXUIElementCreateApplication
    out: list = []
    for win in _get(AXUIElementCreateApplication(pid), "AXWindows") or []:
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
                    fields.append(f)
            stack.extend((c, in_page) for c in (_get(el, "AXChildren") or []))
        if has_page:
            out += fields
    return out


def at(frames, x: float, y: float) -> bool:
    return any(fx <= x <= fx + fw and fy <= y <= fy + fh for fx, fy, fw, fh in frames)
