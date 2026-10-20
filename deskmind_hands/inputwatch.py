"""Which application the user's input went to, so input to DeskMind itself is not "the user changed something".

The executor's epoch moves when the user touched the keyboard or mouse since it was set (an observation and a
consent decided before are stale upstream). The user's click on DeskMind's own consent card is input too, and
counted it made the action it approved stale, re-observed and re-asked without end (PR #48 review, third round).
Whether the window was changed cannot be read off the window (a table's selection is not in the observation), so
the input is told by where it went -- as the window server routed it, not by a guess from window rectangles: a
card that lets clicks through (ignoresMouseEvents) is under the pointer but not the receiver (fourth round).

A listen-only tap at the annotated session point (kCGAnnotatedSessionEventTap) sees each event after the window
server has decided its target process (kCGEventTargetUnixProcessID); input whose target is DeskMind (the app and
its helper) is left out of `last_external_at`. An event with no target known counts: the safe side.

Key events reach a tap only with Input Monitoring; CGEventTapCreate may drop the key bits of a mask and still
return a tap, so whether keys are watched is read from the permission itself (CGPreflightListenEventAccess),
never from the tap having been made. Where no tap can be made, the caller falls back to the HID idle time, where
every input counts. Listen-only: nothing is changed or delayed.
"""
from __future__ import annotations

import threading
import time

import os
import struct

#: Children this process started (pid -> when last seen running): recorded at spawn by `track_children()`, so an
#: event a short-lived child posted is known as ours even after the child is gone. Kept OWN_KEEP_S after exit.
OWN_CHILDREN: dict[int, float] = {}
OWN_KEEP_S = 10.0


def _proc_ppid(pid: int) -> int | None:
    """The parent pid of `pid` (libproc's proc_pidinfo, PROC_PIDTBSDINFO), None when it is gone or unreadable."""
    try:
        import ctypes
        lib = _proc_ppid.lib = getattr(_proc_ppid, "lib", None) or ctypes.CDLL("/usr/lib/libproc.dylib")
        buf = ctypes.create_string_buffer(256)
        n = lib.proc_pidinfo(int(pid), 3, ctypes.c_uint64(0), buf, 256)
        if n < 20:
            return None
        return struct.unpack_from("I", buf.raw, 16)[0]     # struct proc_bsdinfo: pbi_ppid is the fifth uint32
    except Exception:  # noqa: BLE001 -- not macOS, or no libproc
        return None


def _alive(pid: int) -> bool:
    return _proc_ppid(pid) is not None


def _descends_from(pid: int, ancestor: int, depth: int = 16) -> bool:
    for _ in range(depth):
        if pid == ancestor:
            return True
        parent = _proc_ppid(pid)
        if parent is None or parent <= 1 or parent == pid:
            return False
        pid = parent
    return False


def track_children() -> None:
    """Record the pid of every child this process starts from now on (subprocess.Popen), for posted_by_us."""
    import subprocess
    if getattr(subprocess.Popen, "_deskmind_tracked", False):
        return

    class _Tracked(subprocess.Popen):
        _deskmind_tracked = True

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            OWN_CHILDREN[self.pid] = time.time()

    subprocess.Popen = _Tracked


#: DeskMind's own processes: input to them is the user answering DeskMind, not changing their Mac.
OWN_BUNDLES = {"ai.deskmind.app", "ai.deskmind.hands"}


class InputWatch:
    """Records when the user last sent input to an application other than DeskMind (`last_external_at`, epoch
    seconds; 0 before any), and to which (`last_by_bundle`, `last_input_for`). `start()` makes the tap on its own thread and returns whether it could; `keys` says
    whether key events are among what it sees (the Input Monitoring permission), else the caller counts every key
    press by the HID idle time."""

    def __init__(self, own_bundles: set[str] = OWN_BUNDLES):
        self.own_bundles = {b.lower() for b in own_bundles}
        self.last_external_at = 0.0
        self.last_own_at = 0.0
        #: lowercased bundle id -> when input last went to it; input whose target is unknown, apart
        self.last_by_bundle: dict[str, float] = {}
        self.last_unknown_at = 0.0
        self.active = False
        self.keys = False
        self._pid_bundle: dict[int, str] = {}
        #: pid -> whether it is an ordinary app (NSApplicationActivationPolicyRegular). Input to a system surface --
        #: a notification banner, the Dock, Spotlight, a launcher -- can bring another window of any app forward,
        #: so it counts as input to an unknown target (PR #49 review: a Messages banner swapped the conversation an
        #: approved "type and Return" went into).
        self._pid_regular: dict[int, bool] = {}
        self._dragging = False
        #: this process and its children (Peekaboo, the key-event child): events they post are the driver's own
        #: input, not the user's -- a driver click that reached the tap after the action had ended was taken for the
        #: user's input to the task's app and staled the next step (rc.9, 10-20). Refreshed every few seconds.
        self._own_pids: set[int] = set()
        self._not_ours: set[int] = set()
        self._thread: threading.Thread | None = None
        self._tap = None

    # -- classification (pure, testable) ---------------------------------------------------------------------
    def is_own(self, pid: int | None) -> bool:
        if not pid:
            return False
        bundle = self._pid_bundle.get(pid)
        if bundle is None:
            bundle = self._bundle_of(pid)
            self._pid_bundle[pid] = bundle
        return bundle.lower() in self.own_bundles

    def note(self, pid: int | None, at: float | None = None) -> None:
        """An input event the window server routed to `pid`'s process (None or 0: no target known) at `at`."""
        at = at or time.time()
        if self.is_own(pid):
            self.last_own_at = max(self.last_own_at, at)
            return
        self.last_external_at = max(self.last_external_at, at)
        bundle = (self._pid_bundle.get(pid) or "").lower() if pid and self.is_regular(pid) else ""
        if bundle:
            self.last_by_bundle[bundle] = max(self.last_by_bundle.get(bundle, 0.0), at)
        else:
            self.last_unknown_at = max(self.last_unknown_at, at)

    def last_input_for(self, bundles: set[str]) -> float:
        """When input last went to one of `bundles` (lowercased), or anywhere unknown: the safe side."""
        return self.last_input_where(bundles)[0]

    def last_input_where(self, bundles: set[str]) -> tuple[float, str]:
        """(when, where) of the latest input to one of `bundles` or to an unknown target: where is the bundle id, or
        "unknown" (a system surface, a drag, an event that could not be read)."""
        best = (self.last_unknown_at, "unknown")
        for b in bundles:
            t = self.last_by_bundle.get(b.lower(), 0.0)
            if t > best[0]:
                best = (t, b.lower())
        return best

    def posted_by_us(self, source_pid: int | None) -> bool:
        """Whether an event was posted by this process or one of its descendants (a hardware event has no source
        pid, or the window server's). A child is known three ways, none of them slow enough for the tap's callback:
        its pid was recorded when it was started here (`own`, kept a while after it exits, since its last events
        can reach the tap after it is gone); or its parent chain, read with proc_pidinfo, reaches this process; and
        what was found is remembered (PR #53 review: a ps every 3 s missed short-lived peekaboo CLI children)."""
        if not source_pid:
            return False
        pid = int(source_pid)
        now = time.time()
        for p, t in list(OWN_CHILDREN.items()):
            if now - t > OWN_KEEP_S and not _alive(p):
                OWN_CHILDREN.pop(p, None)
        if pid in OWN_CHILDREN or pid in self._own_pids:
            return True
        if pid in self._not_ours:
            return False
        ours = _descends_from(pid, os.getpid())
        (self._own_pids if ours else self._not_ours).add(pid)
        return ours

    def is_regular(self, pid: int) -> bool:
        if pid not in self._pid_regular:
            self._pid_regular[pid] = self._regular_of(pid)
        return self._pid_regular[pid]

    # -- macOS ---------------------------------------------------------------------------------------------
    @staticmethod
    def _regular_of(pid: int) -> bool:
        try:
            from AppKit import NSRunningApplication
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            return bool(app) and int(app.activationPolicy()) == 0     # NSApplicationActivationPolicyRegular
        except Exception:  # noqa: BLE001 -- not known to be an ordinary app: the safe side
            return False

    @staticmethod
    def _bundle_of(pid: int) -> str:
        try:
            from AppKit import NSRunningApplication
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            return str(app.bundleIdentifier() or "") if app else ""
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def keys_permitted() -> bool:
        """Whether this process may listen to key events (Input Monitoring)."""
        try:
            import Quartz
            return bool(Quartz.CGPreflightListenEventAccess())
        except Exception:  # noqa: BLE001 -- no such call: assume not
            return False

    def _callback(self, proxy, type_, event, refcon):
        try:
            import Quartz
            if type_ in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
                if self._tap is not None:
                    Quartz.CGEventTapEnable(self._tap, True)
                return event
            # a drag goes to whatever is under the pointer when it is let go (a file dragged from Finder into the
            # task's window), and the press is the source's: the whole drag, and its release, is input to an
            # unknown target (PR #49 review)
            if self.posted_by_us(int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUnixProcessID) or 0)):
                return event                              # the driver's own click or key, not the user's
            drags = (Quartz.kCGEventLeftMouseDragged, Quartz.kCGEventRightMouseDragged, Quartz.kCGEventOtherMouseDragged)
            ups = (Quartz.kCGEventLeftMouseUp, Quartz.kCGEventRightMouseUp, Quartz.kCGEventOtherMouseUp)
            if type_ in drags:
                self._dragging = True
                self.note(None)
                return event
            if type_ in ups:
                if self._dragging:
                    self._dragging = False
                    self.note(None)
                return event
            self.note(int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventTargetUnixProcessID) or 0))
        except Exception:  # noqa: BLE001 -- unknown where it went: it counts, the safe side (an unknown target)
            self.note(None)
        return event

    def start(self) -> bool:
        """Make the tap and run it on its own thread; False when the system refuses, in which case nothing is
        recorded and `active` stays False."""
        try:
            import Quartz
        except ImportError:
            return False
        self.keys = self.keys_permitted()
        names = ["kCGEventLeftMouseDown", "kCGEventRightMouseDown", "kCGEventOtherMouseDown", "kCGEventScrollWheel",
                 "kCGEventLeftMouseDragged", "kCGEventRightMouseDragged", "kCGEventOtherMouseDragged",
                 "kCGEventLeftMouseUp", "kCGEventRightMouseUp", "kCGEventOtherMouseUp"]
        if self.keys:
            names += ["kCGEventKeyDown", "kCGEventFlagsChanged"]
        mask = 0
        for t in names:
            mask |= Quartz.CGEventMaskBit(getattr(Quartz, t))
        tap = Quartz.CGEventTapCreate(Quartz.kCGAnnotatedSessionEventTap, Quartz.kCGHeadInsertEventTap,
                                      Quartz.kCGEventTapOptionListenOnly, mask, self._callback, None)
        if not tap:
            return False
        self._tap = tap
        ready = threading.Event()

        def run():
            src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
            loop = Quartz.CFRunLoopGetCurrent()
            Quartz.CFRunLoopAddSource(loop, src, Quartz.kCFRunLoopCommonModes)
            Quartz.CGEventTapEnable(tap, True)
            self.active = True
            ready.set()
            Quartz.CFRunLoopRun()

        self._thread = threading.Thread(target=run, name="input-watch", daemon=True)
        self._thread.start()
        ready.wait(2.0)
        return self.active
