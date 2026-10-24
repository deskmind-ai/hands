"""Input delivered to one app's window in the background: no activation, the pointer does not move, the user's app
keeps the front and the keyboard.

The route trycua/cua's driver takes (MIT; libs/cua-driver/rust/crates/platform-macos/src/input/skylight.rs and
mouse.rs). Mouse events carry the window-local point and the window routing fields and go to the process with
SLEventPostToPid as well as CGEventPostToPid. Keys go to the process with SLEventPostToPid and an
SLSEventAuthenticationMessage (macOS 15+), without which WindowServer drops synthetic keys to web content.

Keys go to the target app's key window, so before any key is sent the window is made the app's key window *inside
that app* (enter_window_key): focus records sent to the target process only -- defocus its current key window, focus
the target, then the make-key pair -- and the app's focused window read back through accessibility, the only proof
that keys will land there. The user's front app is told nothing and keeps the keyboard. On the way out the app's
previous key window is given back. (The route before, focus_without_raise, told the user's front app that it had lost
focus; whatever the user typed meanwhile went nowhere.)

Measured 10-21 (macOS 27.2): TextEdit with two windows of its own and Chrome take typing, cmd+A and Chinese text
this way into the exact window, front app unchanged. A plain CGEventPostToPid, or AppKit's fake activation, leaked
keys into another window of the same app.

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
#: kCGEventSourceStateID and kCGEventSourceStateHIDSystemState: the key events say they come from the HID system state.
F_SOURCE_STATE, HID_SYSTEM_STATE = 45, 1

#: SLPSPostEventRecordTo's status when this process has no window-server connection it may post focus records with
#: (a bare process not attributed to an app; cua's CUA-1777): every record is dropped, so nothing can be made key.
INVALID_CONNECTION = 1002
#: Pause after a focus record: an app still handling the defocus can drop the focus that follows (cua, LibreOffice).
RECORD_SETTLE_S = 0.12
#: How long the app's focused window is read back after the records, and after the accessibility write.
CONFIRM_S, AX_CONFIRM_S = 0.4, 0.2
#: UTF-16 units per key event when text is typed as a Unicode payload (a CGEvent carries at most 20).
TEXT_CHUNK = 16

_fns: dict | None = None


class WindowKeyRefused(RuntimeError):
    """The window could not be made (or proven) the app's key window; no key was sent. The message says why, `kind`
    in a word (see enter_window_key)."""
    kind = "unconfirmed"


def _load() -> dict | None:
    global _fns
    if _fns is not None:
        return _fns or None
    try:
        sl, cg = ctypes.CDLL(_SKYLIGHT), ctypes.CDLL(_CG)
        objc_lib = ctypes.CDLL(ctypes.util.find_library("objc"))

        def f(name, args, res=None, required=True):
            for lib in (sl, cg):
                fn = getattr(lib, name, None)
                if fn is not None:
                    fn.argtypes, fn.restype = args, res
                    return fn
            if required:
                raise AttributeError(name)
            return None

        vp, u32, i64 = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64
        fns = {
            "set_int": f("SLEventSetIntegerValueField", [vp, u32, i64]),
            "set_loc": f("CGEventSetWindowLocation", [vp, ctypes.c_double, ctypes.c_double]),
            "front": f("_SLPSGetFrontProcess", [vp], ctypes.c_int),
            "post_rec": f("SLPSPostEventRecordTo", [vp, vp], ctypes.c_int),
            "cid": f("CGSMainConnectionID", [], u32),
            "owner": f("SLSGetWindowOwner", [u32, u32, ctypes.POINTER(u32)], ctypes.c_int),
            "psn": f("SLSGetConnectionPSN", [u32, vp], ctypes.c_int),
            # Optional: without them an event goes with CGEventPostToPid, unauthenticated (post_event).
            "post": f("SLEventPostToPid", [ctypes.c_int, vp], required=False),
            "set_auth": f("SLEventSetAuthenticationMessage", [vp, vp], required=False),
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
        objc_lib.object_getClass.restype, objc_lib.object_getClass.argtypes = vp, [vp]
        objc_lib.class_respondsToSelector.restype = ctypes.c_bool
        objc_lib.class_respondsToSelector.argtypes = [vp, vp]
        fns["auth_cls"] = objc_lib.objc_getClass(b"SLSEventAuthenticationMessage")
        fns["auth_sel"] = objc_lib.sel_registerName(b"messageWithEventRecord:pid:version:")
        fns["auth_new"] = ctypes.CFUNCTYPE(vp, vp, vp, vp, ctypes.c_int, ctypes.c_uint)(
            ctypes.cast(objc_lib.objc_msgSend, vp).value)
        # The factory is macOS 15+; the class alone exists on 14, and sending it the selector there would crash.
        fns["auth_ok"] = bool(fns["auth_cls"] and fns["set_auth"] and objc_lib.class_respondsToSelector(
            objc_lib.object_getClass(fns["auth_cls"]), fns["auth_sel"]))
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
    """A focus record: 0x0D at 0x08, the window at 0x3C, 0x01 focus / 0x02 defocus at 0x8A."""
    rec = (ctypes.c_uint8 * 0xF8)()
    rec[0x04], rec[0x08] = 0xF8, 0x0D
    for i in range(4):
        rec[0x3C + i] = (window_id >> (8 * i)) & 0xFF
    rec[0x8A] = kind   # 0x01 focus, 0x02 defocus
    return rec


def _make_key_record(window_id: int, kind: int):
    """One of the make-key pair (kind 0x01, then 0x02) for `window_id` (cua's make_key_window_record)."""
    rec = (ctypes.c_uint8 * 0xF8)()
    rec[0x04], rec[0x08], rec[0x3A] = 0xF8, kind, 0x10
    for i in range(4):
        rec[0x3C + i] = (window_id >> (8 * i)) & 0xFF
    for i in range(0x20, 0x30):
        rec[i] = 0xFF
    return rec


def front_pid() -> int | None:
    """The pid of the user's front app, asked of the window server; None when it cannot be told."""
    fns = _load()
    if fns and fns.get("pid_of_psn"):
        psn, pid = (ctypes.c_uint8 * 8)(), ctypes.c_int(0)
        if fns["front"](psn) == 0 and fns["pid_of_psn"](psn, ctypes.byref(pid)) == 0 and pid.value:
            return pid.value
    return None


def focused_window(pid: int) -> int | None:
    """The window-server id of the app's focused window (AXFocusedWindow): where the app routes the keys it gets."""
    try:
        from ApplicationServices import AXUIElementCreateApplication
        from .axchrome import _get, _window_id
        win = _get(AXUIElementCreateApplication(pid), "AXFocusedWindow")
        return _window_id(win) if win is not None else None
    except Exception:   # noqa: BLE001 -- unread is unknown, never a guess
        return None


def _set_focused_window(pid: int, wid: int) -> bool:
    """Write the app's AXFocusedWindow to its window `wid`; True when the write was accepted."""
    try:
        from ApplicationServices import AXUIElementCreateApplication, AXUIElementSetAttributeValue
        from .axchrome import _get, _window_id
        app = AXUIElementCreateApplication(pid)
        for win in _get(app, "AXWindows") or []:
            if _window_id(win) == wid:
                return AXUIElementSetAttributeValue(app, "AXFocusedWindow", win) == 0
    except Exception:   # noqa: BLE001
        pass
    return False


def window_ids(pid: int) -> list[int] | None:
    """The window-server ids of the app's accessibility windows (one unreadable id counts as unknown); None when they
    cannot be read."""
    try:
        from ApplicationServices import AXUIElementCreateApplication
        from .axchrome import _get, _window_id
        wins = _get(AXUIElementCreateApplication(pid), "AXWindows")
        if wins is None:
            return None
        ids = [_window_id(w) for w in wins]
        return None if any(i is None for i in ids) else ids
    except Exception:   # noqa: BLE001
        return None


def cg_window_ids(pid: int) -> list[int] | None:
    """The app's ordinary (layer-0) windows as the window server lists them, every Space included -- accessibility
    can miss a window on another Space: those on screen or with a title (a document, a page), not the untitled
    off-screen surfaces an app keeps (Chrome's, Finder's). None when the list cannot be read."""
    try:
        import Quartz
        out = []
        for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, 0) or []:
            if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer") == 0 \
                    and (w.get("kCGWindowIsOnscreen") or (w.get("kCGWindowName") or "").strip()):
                out.append(int(w["kCGWindowNumber"]))
        return out
    except Exception:   # noqa: BLE001
        return None


def _psn_for(wid: int):
    """The serial number of the process that owns window `wid`, from its window-server connection; None if unknown.
    Not GetProcessForPID: the Process Manager registers the asking process as an app (see _load)."""
    fns = _load()
    if not fns:
        return None
    psn, owner = (ctypes.c_uint8 * 8)(), ctypes.c_uint32(0)
    if fns["owner"](fns["cid"](), wid, ctypes.byref(owner)) != 0 or fns["psn"](owner.value, psn) != 0:
        return None
    return psn


def _post_record(psn, record) -> int:
    return _load()["post_rec"](psn, record)


def _send_records(psn, current: int | None, target: int) -> list[int]:
    """Key status inside one app from `current` to `target`, every record to that app only: defocus `current`, focus
    `target`, the make-key pair for `target`. The statuses, in order; whether the app acted is read back by the
    caller."""
    out = []
    if current is not None and current != target:
        out.append(_post_record(psn, _record(current, 0x02)))
        time.sleep(RECORD_SETTLE_S)
    out.append(_post_record(psn, _record(target, 0x01)))
    time.sleep(RECORD_SETTLE_S)
    for kind in (0x01, 0x02):
        out.append(_post_record(psn, _make_key_record(target, kind)))
    return out


def _await_focus(pid: int, wid: int, budget: float) -> bool:
    """Whether the app's focused window reads as `wid` within `budget` seconds (the records are handled on the app's
    main thread, so the change trails the post)."""
    time.sleep(RECORD_SETTLE_S)
    deadline = time.monotonic() + budget
    while True:
        if focused_window(pid) == wid:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.04)


def _refused_1002() -> WindowKeyRefused:
    return _refuse(
        "1002", f"the window server refused the focus records ({INVALID_CONNECTION}: this process has no window-server "
        f"connection of an app -- run hands from the DeskMind app or a terminal app, not as a detached process), so "
        f"no window could be made key and nothing was sent")


def _stopped() -> bool:
    """The app asked the run to stop (grounding.STOP)."""
    try:
        from deskmind_hands.grounding import STOP
        return STOP.is_set()
    except Exception:   # noqa: BLE001
        return False


def user_on(pid: int) -> bool:
    """The user's front app is `pid`, or which app is in front cannot be told: either way its key window may be the
    user's keyboard (fail closed)."""
    front = front_pid()
    return front is None or front == pid


class WindowKey:
    """A window held as its app's key window, in the background; exit() gives the app's previous key window back.
    A context manager: the key window is given back however the block ends."""

    def __init__(self, pid: int, wid: int, previous: int | None, psn, via: str):
        self.pid, self.wid, self.previous, self.psn, self.via = pid, wid, previous, psn, via
        self.closed, self.restored, self.interrupted = False, None, False

    def __enter__(self) -> WindowKey:
        return self

    def __exit__(self, *_exc) -> bool:
        self.exit()
        return False

    def user_took_front(self) -> bool:
        """The user brought the app forward meanwhile (or the front cannot be told): its key window is now the
        user's keyboard as well."""
        return user_on(self.pid)

    def lost(self) -> str | None:
        """Why keys must not go on (the user is on the app, or the window is no longer the app's key window), or
        None. Asked before every chunk of text, every key-down and every chord: a window that closes midway hands key
        to another window of the app -- the user's document."""
        front = front_pid()
        if front is None:
            return "the front app could not be read"
        if front == self.pid:
            return "the user brought the app forward"
        now = focused_window(self.pid)
        if now != self.wid:
            return f"window {self.wid} is no longer the app's key window (now {now})"
        return None

    def note(self) -> str:
        """What the step's result must say about giving the key window back; empty when it went back."""
        if self.interrupted == "unknown":
            return ("the front app could not be read, so restoring the app's previous key window was skipped (no "
                    "record goes to an app the user may be in)")
        if self.interrupted:
            return ("interrupted by the user: they brought the app forward, so restoring its previous key window was "
                    "skipped and it was left as they have it")
        if self.restored is False:
            return f"the app's previous key window ({self.previous}) could not be confirmed given back"
        return ""

    def exit(self) -> bool:
        """The app's previous key window back (the same records, or the accessibility write); True when it reads
        back. Once only. Nothing is sent when the user is on the app (or the front cannot be read): their keyboard
        is in it now."""
        if self.closed:
            return bool(self.restored)
        self.closed = True
        prev = self.previous
        try:
            front = front_pid()
            if front is None or front == self.pid:
                self.interrupted, self.restored = ("unknown" if front is None else "user"), False
                return False
            if prev is None or prev == self.wid:
                # It was key already, or none was: the window is only told it lost focus, as an inactive app's is.
                self.restored = _post_record(self.psn, _record(self.wid, 0x02)) == 0
                return self.restored
            ok = False
            if self.via == "records":
                if INVALID_CONNECTION not in _send_records(self.psn, self.wid, prev):
                    ok = _await_focus(self.pid, prev, CONFIRM_S)
            if not ok:
                ok = _set_focused_window(self.pid, prev) and _await_focus(self.pid, prev, AX_CONFIRM_S)
            self.restored = ok
        except Exception:   # noqa: BLE001 -- giving back must never raise over the step's own result
            self.restored = False
        return bool(self.restored)


class NoWindowKey:
    """No window made key (the app is the user's front app: no records go to it); exit gives nothing back."""
    via, restored, interrupted = "none (the app is in front)", True, False

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def exit(self) -> bool:
        return True

    def note(self) -> str:
        return ""


def _refuse(kind: str, why: str) -> WindowKeyRefused:
    exc = WindowKeyRefused(why)
    exc.kind = kind
    return exc


def enter_window_key(pid: int, wid: int) -> WindowKey:
    """Make window `wid` the key window of its app `pid` without activating or raising it, proven by reading the
    app's focused window back; raise WindowKeyRefused (nothing left sent) when it cannot be. Its `kind` says why:
    "front" (the app is the user's front app, or the front cannot be read -- no record is sent to it), "stopped",
    "1002", "unavailable", "previous" (the app's key window cannot be read while it has other windows, so it could
    not be given back), "unconfirmed".

    The records go out at most twice (WindowServer delivers them asynchronously; the second pass catches an app still
    handling the first), then the accessibility write of AXFocusedWindow. Checked again right before returning: the
    user not on the app, and no stop."""
    fns = _load()
    if not fns:
        raise _refuse("unavailable", "the window server's focus calls are missing on this macOS, so no window can "
                                     "be made key in the background; nothing was sent")
    if user_on(pid):
        raise _refuse("front", "the app is the user's front app (or the front app cannot be read): making its window "
                               "key would move the user's own keyboard focus; nothing was sent")
    if _stopped():
        raise _refuse("stopped", "stopped by the app; nothing was sent")
    if fns.get("auth_ok"):
        auth_safe()             # the self-test's child, before the window is held key, not inside the first key
    psn = _psn_for(wid)
    if psn is None:
        raise _refuse("unavailable", f"window {wid} has no owner the window server can name (closed?); nothing was "
                                     f"sent")
    previous = None
    for attempt in range(3):
        if attempt:
            time.sleep(0.1)
        previous = focused_window(pid)
        if previous is not None:
            break
        ids = window_ids(pid)
        if ids is not None and [i for i in ids if i != wid] == []:
            break               # the target is its only window: nothing to give back but a defocus
    else:
        raise _refuse("previous", f"the app's key window cannot be read while it has other windows, so it could not "
                                  f"be given back afterwards; nothing was sent")
    via = None
    for attempt in range(2):
        if _stopped() or user_on(pid):
            break
        current = previous if attempt == 0 else focused_window(pid)
        statuses = _send_records(psn, current, wid)
        if INVALID_CONNECTION in statuses:
            raise _refused_1002()
        if all(s == 0 for s in statuses) and _await_focus(pid, wid, CONFIRM_S):
            via = "records"
            break
    if via is None and not _stopped() and not user_on(pid) and _set_focused_window(pid, wid) \
            and _await_focus(pid, wid, AX_CONFIRM_S):
        via = "ax"
    key = WindowKey(pid, wid, previous, psn, via or "records")
    # Again right before success (the tries take 0.4-2 s): the user may have come to the app, or the run stopped.
    if user_on(pid):
        key.exit()          # sends nothing: the user is on it
        raise _refuse("front", "the user brought the app forward while its window was being made key; no keys were "
                               "sent")
    if _stopped():
        key.exit()
        raise _refuse("stopped", "stopped by the app while the window was being made key; no keys were sent")
    if via is None:
        now = focused_window(pid)
        if now != previous:
            key.exit()      # whatever the tries moved, put back
        raise _refuse("unconfirmed", f"window {wid} could not be confirmed as the app's key window (its focused "
                                     f"window reads {now}), so no keys were sent: they could have gone to another "
                                     f"window of the same app")
    return key


def give_focus_back(key: WindowKey | NoWindowKey | None) -> bool:
    """The app's previous key window back (WindowKey.exit); the name the background routes call it by."""
    return key.exit() if key is not None else False


#: Whether reading the event record out of a CGEvent and building its authentication message works on this macOS,
#: found once per process by a child that does exactly that (None: not yet asked). The record is read at fixed
#: offsets inside __CGEvent (cua's layout, verified live on macOS 27.2, 10-21); a layout that moved would make
#: objc_msgSend crash the process -- the child's, which then only turns authentication off.
_auth_safe: bool | None = None

AUTH_SELF_TEST = r"""
import os, Quartz
from deskmind_hands.drivers import skylight as s
fns = s._load()
ev = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
print("ok" if fns and s._authenticate_unchecked(fns, s._ptr(ev), os.getpid()) else "no")
"""


def auth_safe() -> bool:
    """Whether the authenticated key route may be used here (AUTH_SELF_TEST passed in a child, once per process)."""
    global _auth_safe
    if _auth_safe is None:
        import os
        import subprocess
        import sys
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in (root, os.environ.get("PYTHONPATH")) if p)}
        try:
            r = subprocess.run([sys.executable, "-c", AUTH_SELF_TEST], capture_output=True, text=True, timeout=20,
                               env=env)
            _auth_safe = r.returncode == 0 and r.stdout.strip() == "ok"
        except (OSError, subprocess.SubprocessError):
            _auth_safe = False
    return _auth_safe


def _authenticate(fns: dict, ev_ptr: int, pid: int) -> bool:
    """Attach the authentication message, if the self-test passed here; else the key goes unauthenticated."""
    if not fns.get("auth_ok") or not auth_safe():
        return False
    return _authenticate_unchecked(fns, ev_ptr, pid)


def _authenticate_unchecked(fns: dict, ev_ptr: int, pid: int) -> bool:
    if not fns.get("auth_ok"):
        return False
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


def post_key_event(pid: int, event, auth: bool = True) -> str:
    """One key event to `pid`: SLEventPostToPid with an authentication message (macOS 15+; `auth`), else
    CGEventPostToPid when SkyLight's post is missing. The route taken, for the trace."""
    import Quartz
    from .base import counted
    fns = _load()
    if not fns or fns.get("post") is None:
        counted(lambda: Quartz.CGEventPostToPid(pid, event))
        return "cgevent"
    p = _ptr(event)
    fns["set_int"](p, F_PID, pid)
    fns["set_int"](p, F_SOURCE_STATE, HID_SYSTEM_STATE)
    signed = auth and _authenticate(fns, p, pid)
    counted(lambda: fns["post"](pid, p))
    return "skylight+auth" if signed else "skylight"


def text_chunks(text: str, size: int = TEXT_CHUNK) -> list[str]:
    """`text` cut into runs of at most `size` UTF-16 units, never inside a character; a newline and a tab are runs
    of their own (typed as the Return and Tab keys, not as a payload: a payload newline does not submit)."""
    out, cur, n = [], "", 0
    for ch in text:
        w = len(ch.encode("utf-16-le")) // 2
        if ch in "\n\r\t":
            if cur:
                out.append(cur)
            out.append(ch)
            cur, n = "", 0
            continue
        if n + w > size and cur:
            out.append(cur)
            cur, n = "", 0
        cur, n = cur + ch, n + w
    if cur:
        out.append(cur)
    return out


class BackgroundQuartz:
    """Quartz, except that CGEventPost goes to one app's window in the background.

    Drop-in for the `Q` the foreground helpers take (PeekabooDriver._post_click, _post_key, pasting): the same event
    sequences, delivered to `pid` with the window-local point and routing fields (mouse) or an authentication message
    (keys), instead of to whatever is under the pointer or in front. Pointer warps and cursor association are no-ops:
    the user's pointer is not touched. Keys reach the window only while it is the app's key window (enter_window_key).
    """

    #: Counts its own posts (base.counted), after its key check: a key it refuses was never sent.
    counts_own_posts = True

    def __init__(self, pid: int, window_id: int, origin: tuple[float, float], key=None):
        import Quartz
        self._q, self.pid, self.wid, self.origin = Quartz, pid, window_id, origin
        self._group = time.time_ns() & 0x7FFFFFFF
        self._fns = _load()
        self.key_auth = True
        #: What holds the window key (WindowKey), or NoWindowKey (the app is the user's front app: no key at all).
        self.key = key

    def _key_allowed(self) -> None:
        """Raise WindowKeyRefused unless a key-down may go now: never without a WindowKey (to the user's front app
        it would land in their key window), and only while the window is still the app's key window and the user
        is not on the app (WindowKey.lost)."""
        if not isinstance(self.key, WindowKey):
            raise _refuse("front", "the app is the user's front app: a key would land in its key window, the "
                                   "user's; none was sent")
        why = self.key.lost()
        if why:
            raise _refuse("front" if "front" in why else "lost", f"{why}; the key was not sent")

    def __getattr__(self, name):
        return getattr(self._q, name)

    def CGWarpMouseCursorPosition(self, *_):   # noqa: N802 -- Quartz's name
        return 0

    def CGAssociateMouseAndMouseCursorPosition(self, *_):   # noqa: N802
        return 0

    def post_checked(self, event, gate) -> None:
        """A key-down whose window and focus the caller checked right before it, with nothing that can wait between
        (PeekabooDriver._press_checked, by a background Gate bound to this WindowKey): posted without asking the
        window again -- key.lost() reads the app's focused
        window through accessibility, which can wait as long as its timeout, and asked here it put that wait between
        the caller's focus check and the key (review of hands#66 @92fbf45). Still never without a WindowKey. Any other
        event: as CGEventPost."""
        q = self._q
        if not isinstance(self.key, WindowKey) or getattr(gate, "window_key", None) is not self.key:
            # only a background gate bound to this window's key stands for the window check: anything else is
            # posted with it (review of hands#66 @8086268: a check without the window let cmd+V go after the user
            # came back to the app)
            self.CGEventPost(0, event)
            return
        if q.CGEventGetType(event) in (q.kCGEventKeyDown, q.kCGEventKeyUp, q.kCGEventFlagsChanged):
            if not isinstance(self.key, WindowKey):
                raise _refuse("front", "the app is the user's front app: a key would land in its key window, the "
                                       "user's; none was sent")
            post_key_event(self.pid, event, auth=self.key_auth)     # counted there (base.counted)
            return
        self.CGEventPost(0, event)

    def CGEventPost(self, _tap, event):   # noqa: N802
        q, fns = self._q, self._fns
        kind = q.CGEventGetType(event)
        if kind in (q.kCGEventKeyDown, q.kCGEventKeyUp, q.kCGEventFlagsChanged):
            if kind != q.kCGEventKeyUp:
                self._key_allowed()     # a key-up always goes: a key-down already sent is never left held
            post_key_event(self.pid, event, auth=self.key_auth)     # counted there (base.counted)
            return
        from .base import counted
        if not fns:
            counted(lambda: q.CGEventPostToPid(self.pid, event))
            return
        p = _ptr(event)
        loc = q.CGEventGetLocation(event)
        fns["set_loc"](p, loc.x - self.origin[0], loc.y - self.origin[1])
        state = q.CGEventGetIntegerValueField(event, q.kCGMouseEventClickState)
        if kind in (q.kCGEventLeftMouseDown, q.kCGEventLeftMouseUp) and not state:
            state = 1
        for field, value in ((F_CLICK_STATE, state), (F_BUTTON, 0), (F_SUBTYPE, SUBTYPE_TOUCH),
                             (F_WINDOW, self.wid), (F_GROUP, self._group), (F_UNDER, self.wid),
                             (F_HANDLER, self.wid), (F_PID, self.pid)):
            fns["set_int"](p, field, value)
        if fns.get("post") is not None:
            counted(lambda: fns["post"](self.pid, p))
        counted(lambda: q.CGEventPostToPid(self.pid, event))   # both transports, as cua sends a click
