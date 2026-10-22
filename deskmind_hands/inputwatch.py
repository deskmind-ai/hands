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
run_fd8ab281: 4 stale refusals said "to: unknown"). A key whose target is neither an ordinary app nor DeskMind
goes, first match:
- its own time inside a flash -- the driver had a task's app in front, from when it took the front to when it gave
  it back (FLASHES: front_taken/front_given_back, plus FLASH_MARGIN_S after) -- to that app: a key that landed in the
  flashed app in the flash's last moment, its front read answered after the front went back, would read the user's
  app (hands#62 review);
- to the app with the keyboard (AXFocusedApplication) when the callback runs within KEY_FRESH_S of the event's own
  timestamp, the read answers within it too, and names an ordinary app (or DeskMind);
- else, the event's time inside a step of the task's (the executor marks each one, `step_began`/`step_ended`, plus
  STEP_MARGIN_S; a step never closed ends at its `max_s`): the step's app -- a late callback or a slow or failed read
  at a flash's edge. Only the fallback: a step spans the driver's wait for the user to pause, and keys typed in the
  terminal then are the user's (hands#62 re-review);
- else it keeps its target: unknown, and it counts.
Invariants: nothing learned about a key after its freshness window can un-count it (only make it count); and a key's
record time is never earlier than the moment it becomes visible -- it is stamped last, under the lock the executor's
epoch mark is stamped under (`mark`), so a key looked into while an action moved the mark is never behind it.

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
#: The longest a step of the task's is taken to last when its end is never told (step_began's default).
STEP_MAX_S = 180.0


#: The driver's flashes, process-wide: [lowercased bundle, took the front at, gave it back at or None, deadline]
#: (epoch seconds), newest last. Told by the driver at the points where it brings a task's app to the front and where
#: it gives the user's app the front back (drivers/peekaboo, the executor's launch), including a front an app took by
#: itself (a launch that ignores -g, a step that came forward): then from when that step began.
FLASHES: list[list] = []
#: After a flash's front was given back: the give-back is confirmed by a read that comes a moment after it.
FLASH_MARGIN_S = 0.1
#: The longest a flash is taken to last when its end is never told.
FLASH_MAX_S = 120.0


def front_taken(bundle: str, began: float | None = None) -> list:
    """A task's app `bundle` has the front since `began` (now when not given); a token for front_given_back. Told
    before the front is given back, so a key whose callback runs after the give-back finds the flash already there."""
    now = time.time()
    began = now if began is None else min(began, now)
    token = [(bundle or "").lower(), began, None, now + FLASH_MAX_S]
    FLASHES[:] = (FLASHES + [token])[-32:]
    return token


def front_given_back(token: list | None) -> None:
    if token is not None and token[2] is None:
        token[2] = time.time()


def flash_at(at: float) -> str:
    """The app a flash had in front at `at` (to FLASH_MARGIN_S after it ended), or ""."""
    for bundle, began, ended, deadline in reversed(list(FLASHES)):
        end = ended if ended is not None else min(time.time(), deadline)
        if bundle and began <= at <= end + FLASH_MARGIN_S:
            return bundle
    return ""


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
        #: held while an input is recorded and while the executor's epoch mark is stamped (note, mark)
        self.lock = threading.Lock()
        self._last_stamp = 0.0

    # -- classification (pure, testable) ---------------------------------------------------------------------
    def is_own(self, pid: int | None) -> bool:
        if not pid:
            return False
        bundle = self._pid_bundle.get(pid)
        if bundle is None:
            bundle = self._bundle_of(pid)
            self._pid_bundle[pid] = bundle
        return bundle.lower() in self.own_bundles

    def note(self, pid: int | None, at: float | None = None, key: bool = False, age: float | None = None,
             t0: float | None = None) -> None:
        """An input event the window server routed to `pid`'s process (None or 0: no target known). `age` is the
        event's age at `t0` (time.monotonic(), sampled once when the callback began; now when not given). `at`: a
        fixed record time, for tests.

        A key whose target is neither an ordinary app nor DeskMind goes, in order:
        - its own time inside a flash (flash_at): to the flashed app, ahead of the front read -- a read that answered
          after the front was given back names the user's app for a key that landed in the flashed one;
        - to the app the fresh front read names, when the callback and the read are within the key's freshness and
          that app is an ordinary app (or DeskMind);
        - else, its own time inside a step of the task's: to the step's app (a late or failed read, a front that is
          no ordinary app -- the flash's edges);
        - else it keeps its target (unknown: it counts).
        The step is the fallback, never ahead of a fresh read: a step spans the driver's wait for the user to pause
        (up to HANDS_FLASH_WAIT_S), and keys the user typed in their terminal then went to the task's app and staled
        every step (hands#62 re-review).

        In three phases (hands#62 external review, twice): 1. every lookup -- whether a pid is DeskMind's or an
        ordinary app (each may ask the system on a cache miss), the flash, the step, the app with the keyboard -- with
        nothing recorded; 2. freshness decided once, after all of them: age + (now - t0) <= KEY_FRESH_S, or what the
        front read found is not taken; 3. the record, under `lock`, its time taken last, inside the lock (`stamp`) --
        the lock the executor holds when it stamps the epoch's mark (`mark`).
        Invariants: nothing learned after a key's freshness window can un-count it (it can only make it count); and a
        key's record time is never earlier than the moment it becomes visible -- a record written after the mark was
        set has a later time, so an action that moved the mark while the key was being looked into never hides it."""
        t0 = time.monotonic() if t0 is None else t0
        wall0 = time.time()

        def kind(p: int) -> str:
            if self.is_own(p):
                return "own"
            return "regular" if p and self.is_regular(p) else "unknown"

        # -- 1. lookups: nothing shared is written (only the lookups' own caches)
        target = kind(pid) if pid else "unknown"
        event_at = (at if at is not None else wall0) - (age if age is not None and age >= 0 else 0.0)
        flash, front, front_kind, step = "", 0, "unknown", ""
        if key and target not in ("own", "regular"):
            flash = self.flash_at(event_at)
            if not flash:
                if age is not None and 0.0 <= age + (time.monotonic() - t0) <= KEY_FRESH_S:
                    front = self.front_now()               # not even asked for a key already past its freshness
                    if front:
                        front_kind = kind(front)
                step = self.step_at(event_at)
        # -- 2. freshness, once, after every lookup
        fresh = age is not None and 0.0 <= age + (time.monotonic() - t0) <= KEY_FRESH_S
        if flash:
            dest = "flash"                                 # the flashed app: it can only make the key count
        elif front and fresh and front_kind in ("regular", "own"):
            dest, pid = front_kind, front
        elif step:
            dest = "step"                                  # the task's app: it can only make the key count
        else:
            dest = target
        bundle = (flash if dest == "flash" else step if dest == "step"
                  else (self._pid_bundle.get(pid) or "").lower() if dest == "regular" else "")
        # -- 3. the record, its time taken last, under the lock the epoch's mark is stamped under
        with self.lock:
            when = at if at is not None else self.stamp()
            if dest == "own":
                self.last_own_at = max(self.last_own_at, when)
                return
            self.last_external_at = max(self.last_external_at, when)
            if bundle:
                self.last_by_bundle[bundle] = max(self.last_by_bundle.get(bundle, 0.0), when)
            else:
                self.last_unknown_at = max(self.last_unknown_at, when)

    def stamp(self) -> float:
        """Now, strictly later than any time stamped before (epoch seconds); called with `lock` held."""
        t = time.time()
        if t <= self._last_stamp:
            t = self._last_stamp + 1e-6
        self._last_stamp = t
        return t

    def mark(self) -> float:
        """A time for the executor's epoch mark: stamped under `lock`, so any input recorded after it is later."""
        with self.lock:
            return self.stamp()

    # -- the task's steps (told by the executor) -------------------------------------------------------------
    def step_began(self, bundle: str, max_s: float = STEP_MAX_S) -> list:
        """A step of the task's in `bundle` begins: its app may be brought forward until `step_ended(token)`, and
        for `max_s` seconds at most -- a step whose end was never told (its caller raised) does not stay open."""
        now = time.time()
        token = [(bundle or "").lower(), now, None, now + max_s]
        self._steps = (self._steps + [token])[-32:]
        return token

    @staticmethod
    def step_ended(token: list) -> None:
        token[2] = time.time()

    @staticmethod
    def flash_at(at: float) -> str:
        """The app a driver's flash had in front at `at` (module flash_at)."""
        return flash_at(at)

    def step_at(self, at: float) -> str:
        """The app of the task's step under way at `at` (within STEP_MARGIN_S of one), or ""."""
        for bundle, began, ended, deadline in reversed(self._steps):
            end = ended if ended is not None else min(time.time(), deadline)
            if bundle and began - STEP_MARGIN_S <= at <= end + STEP_MARGIN_S:
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
            t0 = time.monotonic()                          # sampled once: the key's age is as of t0
            age = self._event_age(Quartz, event) if key else None
            self.note(int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventTargetUnixProcessID) or 0),
                      key=key, age=age, t0=t0)
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
