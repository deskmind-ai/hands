"""Input delivered to one app's window in the background: no activation, the pointer does not move, the user's app
keeps the front.

The route trycua/cua's driver takes (MIT; libs/cua-driver/rust/crates/platform-macos/src/input/skylight.rs and
mouse.rs): the window is given focus without being raised (a pair of SLPSPostEventRecordTo records), each mouse event
carries the window-local point and the window routing fields and goes to the process with SLEventPostToPid as well as
CGEventPostToPid, and each key event carries an SLSEventAuthenticationMessage, without which WindowServer drops
synthetic keys to web content on macOS 14+.

Measured on 09-30: Safari and WebKit pages take clicks, typing and Return this way. The music app (CEF) and the chat
app (Electron) ignore all of it; they still need the brief foreground (see PeekabooDriver._flash).

Private SPI, resolved at first use; `available()` is False when any of it is missing, and callers fall back.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import time

_SKYLIGHT = "/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight"
_CG = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"

# CGEvent integer fields (SkyLight indices): click state, button, subtype, pid filter, window number, click group,
# window under the pointer, window that can handle it.
F_CLICK_STATE, F_BUTTON, F_SUBTYPE, F_PID, F_WINDOW, F_GROUP, F_UNDER, F_HANDLER = 1, 3, 7, 40, 51, 58, 91, 92
SUBTYPE_TOUCH = 3

_fns: dict | None = None


def _load() -> dict | None:
    global _fns
    if _fns is not None:
        return _fns or None
    try:
        sl, cg = ctypes.CDLL(_SKYLIGHT), ctypes.CDLL(_CG)
        objc_lib = ctypes.CDLL(ctypes.util.find_library("objc"))

        def f(name, args, res=None):
            for lib in (sl, cg):
                fn = getattr(lib, name, None)
                if fn is not None:
                    fn.argtypes, fn.restype = args, res
                    return fn
            raise AttributeError(name)

        vp, u32, i64 = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64
        fns = {
            "post": f("SLEventPostToPid", [ctypes.c_int, vp]),
            "set_int": f("SLEventSetIntegerValueField", [vp, u32, i64]),
            "set_loc": f("CGEventSetWindowLocation", [vp, ctypes.c_double, ctypes.c_double]),
            "set_auth": f("SLEventSetAuthenticationMessage", [vp, vp]),
            "front": f("_SLPSGetFrontProcess", [vp], ctypes.c_int),
            "post_rec": f("SLPSPostEventRecordTo", [vp, vp], ctypes.c_int),
            "cid": f("CGSMainConnectionID", [], u32),
            "owner": f("SLSGetWindowOwner", [u32, u32, ctypes.POINTER(u32)], ctypes.c_int),
            "psn": f("SLSGetConnectionPSN", [u32, vp], ctypes.c_int),

        }
        # The front app's pid from its serial number, asked of the window server (its connection, then that
        # connection's pid); optional. Not GetProcessPID: that Process Manager call registers the asking process with
        # LaunchServices as an app, and from then on every process it starts (peekaboo, osascript) leaves a Dock tile
        # for the app responsible for the run -- four in a three-step gym episode, after the first background input.
        try:
            conn_of = f("SLSGetConnectionIDForPSN", [u32, vp, ctypes.POINTER(u32)], ctypes.c_int)
            pid_of_conn = f("SLSConnectionGetPID", [u32, ctypes.POINTER(ctypes.c_int)], ctypes.c_int)
            cid = fns["cid"]

            def pid_of_psn(psn, pid_ref) -> int:
                conn = u32(0)
                return conn_of(cid(), psn, ctypes.byref(conn)) or pid_of_conn(conn.value, pid_ref)
            fns["pid_of_psn"] = pid_of_psn
        except AttributeError:
            fns["pid_of_psn"] = None
        objc_lib.objc_getClass.restype, objc_lib.objc_getClass.argtypes = vp, [ctypes.c_char_p]
        objc_lib.sel_registerName.restype, objc_lib.sel_registerName.argtypes = vp, [ctypes.c_char_p]
        fns["auth_cls"] = objc_lib.objc_getClass(b"SLSEventAuthenticationMessage")
        fns["auth_sel"] = objc_lib.sel_registerName(b"messageWithEventRecord:pid:version:")
        fns["auth_new"] = ctypes.CFUNCTYPE(vp, vp, vp, vp, ctypes.c_int, ctypes.c_uint)(
            ctypes.cast(objc_lib.objc_msgSend, vp).value)
        _fns = fns
    except (OSError, AttributeError):
        _fns = {}
    return _fns or None


def available() -> bool:
    return _load() is not None


def _ptr(event) -> int:
    import objc
    return objc.pyobjc_id(event)


def _record(window_id: int, kind: int):
    rec = (ctypes.c_uint8 * 0xF8)()
    rec[0x04], rec[0x08] = 0xF8, 0x0D
    for i in range(4):
        rec[0x3C + i] = (window_id >> (8 * i)) & 0xFF
    rec[0x8A] = kind   # 0x01 focus, 0x02 defocus
    return rec


def front_window(pid: int) -> int | None:
    """The front ordinary window of `pid`, which has its key focus: the topmost on-screen one."""
    import Quartz
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly
                                               | Quartz.kCGWindowListExcludeDesktopElements, 0) or []:
        if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer") == 0:
            return int(w["kCGWindowNumber"])
    return None


class FocusToken:
    """What focus_without_raise took, to be given back: the user's front app and its key window."""

    def __init__(self, prev_psn, prev_pid: int, prev_wid: int | None, target_psn, target_wid: int):
        self.prev_psn, self.prev_pid, self.prev_wid = prev_psn, prev_pid, prev_wid
        self.target_psn, self.target_wid = target_psn, target_wid


def focus_without_raise(pid: int, window_id: int) -> FocusToken | None:
    """Key focus to `window_id` without bringing its app forward: the front app is told it lost focus and the target
    that it has it, and neither is raised. The front app keeps the front but not the keyboard -- what the user types
    goes nowhere -- so the focus must be given back (give_focus_back) as soon as the input is delivered."""
    fns = _load()
    if not fns:
        return None
    prev, target = (ctypes.c_uint8 * 8)(), (ctypes.c_uint8 * 8)()
    if fns["front"](prev) != 0:
        return None
    owner = ctypes.c_uint32(0)
    if fns["owner"](fns["cid"](), window_id, ctypes.byref(owner)) != 0 or fns["psn"](owner.value, target) != 0:
        return None
    prev_pid = ctypes.c_int(0)
    if fns["pid_of_psn"] is not None:
        fns["pid_of_psn"](prev, ctypes.byref(prev_pid))
    token = FocusToken(prev, prev_pid.value, front_window(prev_pid.value), target, window_id)
    a = fns["post_rec"](prev, _record(window_id, 0x02))
    b = fns["post_rec"](target, _record(window_id, 0x01))
    time.sleep(0.05)   # AppKit updates its key window before the events arrive
    return token if a == 0 and b == 0 else None


def give_focus_back(token: FocusToken | None) -> bool:
    """The keyboard back to the app that had it before focus_without_raise, on its own front window."""
    fns = _load()
    if not fns or token is None or token.prev_wid is None:
        return False
    a = fns["post_rec"](token.target_psn, _record(token.target_wid, 0x02))
    b = fns["post_rec"](token.prev_psn, _record(token.prev_wid, 0x01))
    return a == 0 and b == 0


def focus_app(pid: int) -> bool:
    """Key focus to `pid`'s front window, e.g. to repair a focus left elsewhere; nothing is raised."""
    fns = _load()
    wid = front_window(pid)
    if not fns or wid is None:
        return False
    psn = (ctypes.c_uint8 * 8)()
    owner = ctypes.c_uint32(0)
    if fns["owner"](fns["cid"](), wid, ctypes.byref(owner)) != 0 or fns["psn"](owner.value, psn) != 0:
        return False
    return fns["post_rec"](psn, _record(wid, 0x01)) == 0


def _authenticate(fns: dict, ev_ptr: int, pid: int) -> bool:
    for offset in (24, 32, 16):   # the event record inside __CGEvent, as cua probes it
        record = ctypes.c_void_p.from_address(ev_ptr + offset).value
        if record:
            break
    else:
        return False
    msg = fns["auth_new"](fns["auth_cls"], fns["auth_sel"], record, pid, 0)
    if not msg:
        return False
    fns["set_auth"](ev_ptr, msg)
    return True


class BackgroundQuartz:
    """Quartz, except that CGEventPost goes to one app's window in the background.

    Drop-in for the `Q` the foreground helpers take (PeekabooDriver._post_click, _post_key, pasting): the same event
    sequences, delivered to `pid` with the window-local point and routing fields (mouse) or an authentication message
    (keys), instead of to whatever is under the pointer or in front. Pointer warps and cursor association are no-ops:
    the user's pointer is not touched.
    """

    def __init__(self, pid: int, window_id: int, origin: tuple[float, float]):
        import Quartz
        self._q, self.pid, self.wid, self.origin = Quartz, pid, window_id, origin
        self._group = time.time_ns() & 0x7FFFFFFF
        self._fns = _load()

    def __getattr__(self, name):
        return getattr(self._q, name)

    def CGWarpMouseCursorPosition(self, *_):   # noqa: N802 -- Quartz's name
        return 0

    def CGAssociateMouseAndMouseCursorPosition(self, *_):   # noqa: N802
        return 0

    def CGEventPost(self, _tap, event):   # noqa: N802
        q, fns, p = self._q, self._fns, _ptr(event)
        kind = q.CGEventGetType(event)
        if kind in (q.kCGEventKeyDown, q.kCGEventKeyUp, q.kCGEventFlagsChanged):
            fns["set_int"](p, F_PID, self.pid)
            _authenticate(fns, p, self.pid)
            fns["post"](self.pid, p)
            return
        loc = q.CGEventGetLocation(event)
        fns["set_loc"](p, loc.x - self.origin[0], loc.y - self.origin[1])
        state = q.CGEventGetIntegerValueField(event, q.kCGMouseEventClickState)
        if kind in (q.kCGEventLeftMouseDown, q.kCGEventLeftMouseUp) and not state:
            state = 1
        for field, value in ((F_CLICK_STATE, state), (F_BUTTON, 0), (F_SUBTYPE, SUBTYPE_TOUCH),
                             (F_WINDOW, self.wid), (F_GROUP, self._group), (F_UNDER, self.wid),
                             (F_HANDLER, self.wid), (F_PID, self.pid)):
            fns["set_int"](p, field, value)
        fns["post"](self.pid, p)
        q.CGEventPostToPid(self.pid, event)   # both transports, as cua sends a click
