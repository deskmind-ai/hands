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

A key event's target is not always an ordinary app: a key pressed with Command held comes with the Dock as its
target -- it may act on the app switcher (Command-Tab), which changes only which app is in front, no change of
anything a task looks at -- and some come with none. While the owner worked in a terminal and switched apps, each such
key was input to an unknown target and staled the steps of a task working in a music app (rc.13, 10-22,
run_fd8ab281: 4 stale refusals said "to: unknown"). Such a key (target neither an ordinary app nor DeskMind) is input
to the app that had the keyboard then, as plain typing is, under three guards, each on the safe side:
- during a step of the task's (the executor marks each one, `step_began`/`step_ended`: the step may bring the task's
  app forward, a flash), plus STEP_MARGIN_S around it, the key counts for the task's app, whoever has the keyboard
  when the callback runs -- it can run late, waiting for the interpreter while the executor works;
- otherwise the app with the keyboard is read only when the callback runs within KEY_FRESH_S of the event's own
  timestamp; a late callback would read a front that has moved on, so the key keeps its target and counts;
- when the app with the keyboard cannot be read (no answer within FRONT_DEADLINE_S, accessibility refused), or is
  itself no ordinary app (Spotlight's field), the key keeps its target and counts.

Key events reach a tap only with Input Monitoring; CGEventTapCreate may drop the key bits of a mask and still
return a tap, so whether keys are watched is read from the permission itself (CGPreflightListenEventAccess),
never from the tap having been made. Where no tap can be made, the caller falls back to the HID idle time, where
every input counts. Listen-only: nothing is changed or delayed.
"""
from __future__ import annotations

import queue
import threading
import time

import os
import struct

#: Children this process started: pid -> [process start time, when it exited or None]. Recorded at spawn and at
#: reaping by `track_children()`, so an event a child posted is known as ours while it runs and for OWN_KEEP_S after
#: it exits -- its last events can reach the tap after it is gone (hands #54 review: kept from spawn, a child that ran
#: longer than that fell off the list the moment it exited).
OWN_CHILDREN: dict[int, list] = {}
OWN_KEEP_S = 10.0


def _proc_info(pid: int) -> tuple[int, float] | None:
    """(parent pid, start time) of `pid` from libproc's proc_pidinfo (PROC_PIDTBSDINFO); None when it is gone or
    unreadable. The start time tells a reused pid from the process that had it."""
    try:
        import ctypes
        lib = _proc_info.lib = getattr(_proc_info, "lib", None) or ctypes.CDLL("/usr/lib/libproc.dylib")
        buf = ctypes.create_string_buffer(256)
        n = lib.proc_pidinfo(int(pid), 3, ctypes.c_uint64(0), buf, 256)
        if n < 136:
            return None
        ppid = struct.unpack_from("I", buf.raw, 16)[0]                 # struct proc_bsdinfo: pbi_ppid
        sec, usec = struct.unpack_from("QQ", buf.raw, 120)             # pbi_start_tvsec, pbi_start_tvusec
        return ppid, sec + usec / 1e6
    except Exception:  # noqa: BLE001 -- not macOS, or no libproc
        return None


def libproc_available() -> bool:
    """Whether process details can be read at all (macOS's libproc); off macOS, children are known by pid alone."""
    if not hasattr(libproc_available, "ok"):
        try:
            import ctypes
            ctypes.CDLL("/usr/lib/libproc.dylib")
            libproc_available.ok = True
        except Exception:  # noqa: BLE001
            libproc_available.ok = False
    return libproc_available.ok


def _proc_ppid(pid: int) -> int | None:
    info = _proc_info(pid)
    return info[0] if info else None


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
    """Record every child this process starts from now on (subprocess.Popen): its start time when it starts, the
    time it exits when it is reaped."""
    import subprocess
    if getattr(subprocess.Popen, "_deskmind_tracked", False):
        return

    class _Tracked(subprocess.Popen):
        _deskmind_tracked = True

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            info = _proc_info(self.pid)
            # gone before its start time could be read: not recorded -- a later process given the same pid would
            # otherwise pass for ours (hands #54 review); its events, if any, then count as the user's (the safe side).
            # Where no process details can be read at all (not macOS), known by its pid alone.
            if info is not None:
                OWN_CHILDREN[self.pid] = [info[1], None]
            elif not libproc_available():
                OWN_CHILDREN[self.pid] = [None, None]

        def _note_exit(self):
            if self.returncode is not None and self.pid in OWN_CHILDREN and OWN_CHILDREN[self.pid][1] is None:
                OWN_CHILDREN[self.pid][1] = time.time()

        def wait(self, *a, **k):
            try:
                return super().wait(*a, **k)
            finally:
                self._note_exit()

        def poll(self):
            r = super().poll()
            self._note_exit()
            return r

    subprocess.Popen = _Tracked


#: DeskMind's own processes: input to them is the user answering DeskMind, not changing their Mac.
OWN_BUNDLES = {"ai.deskmind.app", "ai.deskmind.hands"}

#: How old a key event may be when its callback runs for the app with the keyboard to be read for it then.
KEY_FRESH_S = 0.05
#: Around a step of the task's (before its start, after its end): a key needing an app counts for the step's app. The
#: end side covers the front given back late (the driver's LATE_FRONT_S watch) and the event clock's rounding.
STEP_MARGIN_S = 0.5
#: How long the tap's callback waits for the accessibility server to name the app with the keyboard; after a read
#: that failed or did not answer, none is tried for FRONT_BACKOFF_S (each key then keeps its target and counts).
FRONT_DEADLINE_S = 0.1
FRONT_BACKOFF_S = 0.5


def _uptime_ns() -> int:
    """Nanoseconds since boot, not counting sleep: mach_absolute_time's clock, which CGEventGetTimestamp counts on."""
    clock = getattr(time, "CLOCK_UPTIME_RAW", None)
    return time.clock_gettime_ns(clock) if clock is not None else time.monotonic_ns()


def _mach_ticks() -> tuple[int, float] | None:
    """(mach_absolute_time now, nanoseconds per tick); None off macOS."""
    try:
        import ctypes
        lib = _mach_ticks.lib = getattr(_mach_ticks, "lib", None) or ctypes.CDLL("/usr/lib/libSystem.dylib")
        if not hasattr(_mach_ticks, "scale"):
            class _Timebase(ctypes.Structure):
                _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]
            tb = _Timebase()
            lib.mach_timebase_info(ctypes.byref(tb))
            lib.mach_absolute_time.restype = ctypes.c_uint64
            _mach_ticks.scale = tb.numer / tb.denom if tb.denom else 1.0
        return int(lib.mach_absolute_time()), _mach_ticks.scale
    except Exception:  # noqa: BLE001
        return None


def event_age(ts: int) -> float | None:
    """Seconds since an event stamped `ts` (CGEventGetTimestamp) happened; None when it cannot be told. The stamp is
    documented as nanoseconds since boot, and on Apple silicon is reported to be in mach_absolute_time ticks (one
    tick is 41.67 ns there): read both ways, the reading that gives a plausible age (under a minute, not ahead of
    now) is the one, and with neither the age is not known -- the safe side (the key keeps its target)."""
    if not ts or ts <= 0:
        return None
    ages = [(_uptime_ns() - ts) / 1e9]
    ticks = _mach_ticks()
    if ticks is not None:
        ages.append((ticks[0] - ts) * ticks[1] / 1e9)
    for age in ages:
        if -0.01 <= age <= 60.0:
            return max(age, 0.0)
    return None


class InputWatch:
    """Records when the user last sent input to an application other than DeskMind (`last_external_at`, epoch
    seconds; 0 before any), and to which (`last_by_bundle`, `last_input_for`). `start()` makes the tap on its own thread and returns whether it could; `keys` says
    whether key events are among what it sees (the Input Monitoring permission), else the caller counts every key
    press by the HID idle time."""

    def __init__(self, own_bundles: set[str] = OWN_BUNDLES, front_pid=None):
        self.own_bundles = {b.lower() for b in own_bundles}
        #: the pid of the app that has the keyboard now (0 or None: not known), read as a key event arrives
        self.front_pid = front_pid or self._front_pid
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
        self._own_pids: set[tuple] = set()
        self._not_ours: set[tuple] = set()
        self._thread: threading.Thread | None = None
        self._tap = None
        #: the task's steps, newest last: [lowercased bundle, began at, ended at or None] (epoch seconds)
        self._steps: list[list] = []
        #: the reader of the app with the keyboard, on its own thread (front_now)
        self._front_q: queue.Queue | None = None
        self._front_pending: threading.Event | None = None
        self._front_backoff_until = 0.0
        self._front_lock = threading.Lock()

    # -- classification (pure, testable) ---------------------------------------------------------------------
    def is_own(self, pid: int | None) -> bool:
        if not pid:
            return False
        bundle = self._pid_bundle.get(pid)
        if bundle is None:
            bundle = self._bundle_of(pid)
            self._pid_bundle[pid] = bundle
        return bundle.lower() in self.own_bundles

    def note(self, pid: int | None, at: float | None = None, key: bool = False, age: float | None = None) -> None:
        """An input event the window server routed to `pid`'s process (None or 0: no target known) at `at`. A key
        event (`key`) whose target is neither an ordinary app nor DeskMind: inside a step of the task's, input to the
        step's app; else, when the event is `age` seconds old at most KEY_FRESH_S, input to the app that has the
        keyboard now; else its target is kept (see the module's docstring)."""
        at = at or time.time()
        if key and not self.is_own(pid) and not (pid and self.is_regular(pid)):
            step = self.step_at(at)
            if step:
                self.last_external_at = max(self.last_external_at, at)
                self.last_by_bundle[step] = max(self.last_by_bundle.get(step, 0.0), at)
                return
            if age is not None and 0.0 <= age <= KEY_FRESH_S:
                pid = self.front_now() or pid
        if self.is_own(pid):
            self.last_own_at = max(self.last_own_at, at)
            return
        self.last_external_at = max(self.last_external_at, at)
        bundle = (self._pid_bundle.get(pid) or "").lower() if pid and self.is_regular(pid) else ""
        if bundle:
            self.last_by_bundle[bundle] = max(self.last_by_bundle.get(bundle, 0.0), at)
        else:
            self.last_unknown_at = max(self.last_unknown_at, at)

    # -- the task's steps (told by the executor) -------------------------------------------------------------
    def step_began(self, bundle: str) -> list:
        """A step of the task's in `bundle` begins: its app may be brought forward until `step_ended(token)`."""
        token = [(bundle or "").lower(), time.time(), None]
        self._steps = (self._steps + [token])[-32:]
        return token

    @staticmethod
    def step_ended(token: list) -> None:
        token[2] = time.time()

    def step_at(self, at: float) -> str:
        """The app of the task's step under way at `at` (within STEP_MARGIN_S of one), or ""."""
        for bundle, began, ended in reversed(self._steps):
            if bundle and began - STEP_MARGIN_S <= at <= (ended if ended is not None else time.time()) + STEP_MARGIN_S:
                return bundle
        return ""

    # -- the app with the keyboard ---------------------------------------------------------------------------
    def front_now(self) -> int:
        """The pid of the app that has the keyboard now; 0 when it cannot be known (the key then keeps its target).
        Read on a thread of its own, waited for at most FRONT_DEADLINE_S: a read that hangs never holds the tap,
        and no process-wide accessibility setting is changed for it (a messaging timeout set on the system-wide
        element is every AX call's in this process -- the address-bar guard's reads included). While one read is
        still out, or for FRONT_BACKOFF_S after one failed, none is tried."""
        if time.monotonic() < self._front_backoff_until:
            return 0
        with self._front_lock:
            if self._front_pending is not None and not self._front_pending.is_set():
                return 0                                   # the last read has not come back
            if self._front_q is None:
                self._front_q = queue.Queue()
                threading.Thread(target=self._front_worker, name="input-watch-front", daemon=True).start()
            done, box = threading.Event(), {}
            self._front_pending = done
        self._front_q.put((done, box))
        pid = box.get("pid", 0) if done.wait(FRONT_DEADLINE_S) else 0
        if not pid:
            self._front_backoff_until = time.monotonic() + FRONT_BACKOFF_S
        return pid

    def _front_worker(self) -> None:
        while True:
            done, box = self._front_q.get()
            try:
                box["pid"] = int(self.front_pid() or 0)
            except Exception:  # noqa: BLE001 -- not known: the safe side
                box["pid"] = 0
            finally:
                done.set()

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
        pid, or the window server's). A child started here is ours while it runs and for OWN_KEEP_S after it exits
        (OWN_CHILDREN); any other pid is ours when its parent chain, read with proc_pidinfo, reaches this process.
        Answers are remembered by (pid, start time), so a pid the system gives to someone else later is asked anew."""
        if not source_pid:
            return False
        pid = int(source_pid)
        if pid == os.getpid():
            return True
        now = time.time()
        for p, (_, exited) in list(OWN_CHILDREN.items()):
            if exited is not None and now - exited > OWN_KEEP_S:
                OWN_CHILDREN.pop(p, None)
        info = _proc_info(pid)
        mine = OWN_CHILDREN.get(pid)
        if mine is not None:
            started, exited = mine
            if info is None:                       # gone: ours if it went within the last OWN_KEEP_S (or unreaped)
                return exited is None or now - exited <= OWN_KEEP_S
            if started is None or abs(info[1] - started) < 0.01:
                return True                        # the child itself, still running
        if info is None:
            return False                           # gone, and not a child we know: not ours
        key = (pid, round(info[1], 3))
        if key in self._own_pids:
            return True
        if key in self._not_ours:
            return False
        ours = _descends_from(pid, os.getpid())
        (self._own_pids if ours else self._not_ours).add(key)
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
    def _front_pid() -> int:
        """The app that has the keyboard now, asked of the accessibility server (the system-wide element's
        AXFocusedApplication); called on front_now's thread, which bounds the wait -- no messaging timeout is set
        here, as one set on the system-wide element is the whole process's. Not NSWorkspace's frontmostApplication:
        in a process whose main thread runs no event loop (this one) it keeps the app that was in front when it was
        first read, which a probe on 10-22 showed while the keyboard moved between four apps. The accessibility
        server answers only a process that has spoken to the window server (before that: kAXErrorCannotComplete,
        every time), so a cheap window list is asked for first. 0 when it cannot be read."""
        try:
            import ApplicationServices as AS
            import Quartz
            if not getattr(InputWatch, "_window_server_seen", False):
                Quartz.CGWindowListCreate(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
                InputWatch._window_server_seen = True
            err, app = AS.AXUIElementCopyAttributeValue(AS.AXUIElementCreateSystemWide(), "AXFocusedApplication", None)
            if err != 0 or app is None:
                return 0
            err, pid = AS.AXUIElementGetPid(app, None)
            return int(pid) if err == 0 else 0
        except Exception:  # noqa: BLE001 -- not macOS, or no accessibility: not known
            return 0

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
            key = type_ in (Quartz.kCGEventKeyDown, Quartz.kCGEventFlagsChanged)
            age = self._event_age(Quartz, event) if key else None
            self.note(int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventTargetUnixProcessID) or 0),
                      at=time.time() - age if age is not None and age >= 0 else None, key=key, age=age)
        except Exception:  # noqa: BLE001 -- unknown where it went: it counts, the safe side (an unknown target)
            self.note(None)
        return event

    @staticmethod
    def _event_age(Quartz, event) -> float | None:
        """How long ago the event happened (seconds, by its own timestamp); None when that cannot be read."""
        try:
            return event_age(int(Quartz.CGEventGetTimestamp(event) or 0))
        except Exception:  # noqa: BLE001
            return None

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
