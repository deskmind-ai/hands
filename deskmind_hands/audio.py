"""Which apps are sending sound to an output right now: evidence that something is playing, read from the system.

A run asked to play a song had it playing at 79 s and could not tell -- the music app's window shows a play/pause
icon that OCR does not read -- so it clicked the player bar again and again to find out (rc.10, 10-21). The system
knows: CoreAudio's process objects (macOS 14.2+) say, per process, whether it is running output
(kAudioProcessPropertyIsRunningOutput). No private framework, no entitlement. The process doing it is often a
helper of the app (a browser's or an embedded web view's audio process), so each is walked up its parent chain to
the application that owns it.

MediaRemote's "now playing" (title, artist) is not used: since macOS 15.4 it answers nothing to an ordinary
process (checked on this Mac, 10-21).
"""
from __future__ import annotations

import ctypes
import struct


def _fourcc(s: str) -> int:
    return struct.unpack(">I", s.encode())[0]


class _Addr(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32), ("mScope", ctypes.c_uint32), ("mElement", ctypes.c_uint32)]


_SYSTEM = 1
_GLOBAL = _fourcc("glob")


def _get(ca, obj: int, selector: str, ctype, count: int = 1) -> list:
    a = _Addr(_fourcc(selector), _GLOBAL, 0)
    buf = (ctype * count)()
    n = ctypes.c_uint32(ctypes.sizeof(buf))
    if ca.AudioObjectGetPropertyData(ctypes.c_uint32(obj), ctypes.byref(a), 0, None, ctypes.byref(n), buf) != 0:
        return []
    return list(buf)[: n.value // ctypes.sizeof(ctype)]


def output_pids() -> list[int]:
    """The pids of the processes sending audio to an output now; [] when it cannot be read."""
    try:
        ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        a = _Addr(_fourcc("prs#"), _GLOBAL, 0)                     # kAudioHardwarePropertyProcessObjectList
        size = ctypes.c_uint32(0)
        if ca.AudioObjectGetPropertyDataSize(ctypes.c_uint32(_SYSTEM), ctypes.byref(a), 0, None, ctypes.byref(size)) != 0:
            return []
        objs = _get(ca, _SYSTEM, "prs#", ctypes.c_uint32, max(size.value // 4, 1))
        out = []
        for o in objs:
            running = _get(ca, o, "piro", ctypes.c_uint32)          # kAudioProcessPropertyIsRunningOutput
            pid = _get(ca, o, "ppid", ctypes.c_int32)                # kAudioProcessPropertyPID
            if running and running[0] and pid and pid[0] > 0:
                out.append(int(pid[0]))
        return out
    except Exception:  # noqa: BLE001 -- not macOS, or older than 14.2
        return []


def _responsible(pid: int) -> int | None:
    """The pid of the application macOS holds responsible for `pid` (a helper's app; responsibility_get_pid_
    responsible_for_pid, in libSystem); None when it cannot be read."""
    try:
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        f = lib.responsibility_get_pid_responsible_for_pid
        f.restype, f.argtypes = ctypes.c_int, [ctypes.c_int]
        r = int(f(int(pid)))
        return r if r > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _app_of(pid: int) -> tuple[str, str] | None:
    """(bundle id, localized name) of the application that owns `pid`: the one macOS holds responsible for it, else
    the process itself or the first ancestor that is an application."""
    try:
        from AppKit import NSRunningApplication
        from .inputwatch import _proc_ppid
    except Exception:  # noqa: BLE001
        return None

    def app(p: int):
        a = NSRunningApplication.runningApplicationWithProcessIdentifier_(p)
        if a is not None and a.bundleIdentifier() and int(a.activationPolicy()) == 0:
            return str(a.bundleIdentifier()), str(a.localizedName() or a.bundleIdentifier())
        return None
    r = _responsible(pid)
    if r and app(r):
        return app(r)
    for _ in range(8):
        found = app(pid)
        if found:
            return found
        parent = _proc_ppid(pid)
        if not parent or parent <= 1 or parent == pid:
            return None
        pid = parent
    return None


def playing_apps() -> list[dict]:
    """[{bundle, app_name}] of the applications sending sound to an output now, each once."""
    seen: dict[str, str] = {}
    for pid in output_pids():
        app = _app_of(pid)
        if app and app[0] not in seen:
            seen[app[0]] = app[1]
    return [{"bundle": b, "app_name": n} for b, n in sorted(seen.items())]
