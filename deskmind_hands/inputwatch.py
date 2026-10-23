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
run_fd8ab281: 4 stale refusals said "to: unknown"). Main counted every such key, and main is the floor: a key whose
target is neither an ordinary app nor DeskMind is left uncounted for the task only on positive evidence that no app of
the task's could have had it (hands#62 review at e8fa010 -- three rounds of inferring "who had the front" site by
site, then a polled record of it, each missed a case; polling never sees every moment):
1. no driver action that can bring an app forward -- a step of the task's, a flash, staging, a launch, each told
   before it starts and ended in a finally (step_began, front_taken) -- was under way at any point of the key's span,
   from FRONT_MARGIN_S before its own time (monotonic: the callback's start less the event's age) to the callback's
   start (the annotated tap sees an event after its target was decided);
2. the front as observed has no unseen time over that span: the app with the keyboard is read every FRONT_POLL_S on
   a thread of the watch's own (front_seen, monotonic), each read proving its app only at one instant between its
   start and its answer, and every two consecutive reads from the last answered by the span's start to the first
   begun after its end are at most FRONT_GAP_S (50 ms) apart from the one's start to the other's answer (the
   callback waits for that last read only while the thread is live);
3. a read at the callback answered (front_now) and is among the apps seen;
4. then the key goes to every app seen, and none is the task's (known where the task's apps are: last_input_for).
Otherwise the key is a doubt (last_doubt_at): it counts as main counted it -- wiped only by the mark main moved at
each action's end (the executor's `_action_end_at`) -- and it also goes, as before, where any evidence names: every
app seen, else first match:
- its own time inside a flash -- the driver had a task's app in front, from when it took the front to when it gave
  it back (FLASHES: front_taken/front_given_back, plus FLASH_MARGIN_S after) -- to every app a flash had in front
  then, and to the step's app;
- to the app with the keyboard (AXFocusedApplication) when the callback runs within KEY_FRESH_S of the event's own
  timestamp, the read answers within it too, and names an ordinary app (or DeskMind);
- else, the event's time inside a step of the task's (STEP_MARGIN_S around it; a step never closed ends at its
  `max_s`): the step's app. Only the fallback: a step spans the driver's wait for the user to pause, and keys typed
  in the terminal then are the user's (hands#62 re-review).
Invariants: nothing learned about a key after its freshness window can un-count it (only make it count); and a key's
record time is never earlier than the moment it becomes visible -- it is stamped last, under the lock the executor's
epoch mark is stamped under (`mark`), so a key looked into while an action moved the mark is never behind it.

Key events reach a tap only with Input Monitoring; CGEventTapCreate may drop the key bits of a mask and still
return a tap, so whether keys are watched is read from the permission itself (CGPreflightListenEventAccess),
never from the tap having been made. Where no tap can be made, the caller falls back to the HID idle time, where
every input counts. Listen-only: nothing is changed or delayed.
"""
from __future__ import annotations

import collections
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
#: The front, observed (InputWatch.front_seen, the poller): how often the app with the keyboard is read, and how
#: long before a key's own time an app in front still counts for it (the event clock against the read's, and the key
#: routed a moment before the front read changes). A read costs ~0.2 ms (500 reads at 20 ms on 10-23: median 0.2 ms,
#: p99 9.7 ms, ~0.4% of a core).
FRONT_POLL_S = 0.02
FRONT_MARGIN_S = 0.1
#: The widest two consecutive reads may be apart, from the one's start to the other's answer, for nothing between
#: them to go unseen. A read proves the front only at some instant within [start, answer], so two reads prove nothing
#: about up to that much time between them. 50 ms is well under what a person needs to bring another app forward, type
#: into it and go back -- three deliberate acts (a click or Command-Tab, a key, another switch), each a reaction of
#: ~150 ms or more, and even a fast typist's keys are ~80-100 ms apart -- so no app the user typed into can sit between
#: two such reads unseen. Normal reads are ~20 ms apart (FRONT_POLL_S plus a read's ~0.2 ms, p99 ~10 ms); a slow read
#: (hands#62 review at f0f7186: a read begun at 20 ms answered "terminal" at 140 ms, the user in TextEdit at 125-130
#: ms) makes a wider window, and a key over it counts, as on main.
FRONT_GAP_S = 0.05
#: How long the record is kept (reads, at ~50 a second: ~80 s); a key older than that counts, as on main.
FRONT_READS = 4096


#: The driver's flashes, process-wide: [lowercased bundle, took the front at, gave it back at or None, deadline]
#: (epoch seconds), newest last. Told by the driver before it brings a task's app to the front, and before anything
#: that may bring one forward by itself starts (staging, the executor's launch: then for the task's apps, whether or
#: not one comes), and ended after it gives the user's app the front back (drivers/peekaboo, the executor's launch):
#: told only as the front went back, a key whose callback ran while the app was in front found no flash (hands#62
#: review at af55263). A front an app took by itself during a step is told when it is found, from the step's start.
FLASHES: list[list] = []
#: After a flash's front was given back: the give-back is confirmed by a read that comes a moment after it.
FLASH_MARGIN_S = 0.1
#: The longest a flash is taken to last when its end is never told.
FLASH_MAX_S = 120.0


def _token(bundle: str, began: float | None, max_s: float) -> list:
    """[lowercased bundle, began, ended or None, deadline] in epoch seconds, then the same three on the monotonic
    clock: the wall clock is what callers and records speak, the monotonic one is what a span is judged on (a wall
    clock stepped back must not end a driver's action early: hands#62 review at e8fa010)."""
    now, mono = time.time(), time.monotonic()
    began = now if began is None else min(began, now)
    return [(bundle or "").lower(), began, None, now + max_s, mono - (now - began), None, mono + max_s]


def _ended(token: list) -> None:
    if token[2] is None:
        token[2] = time.time()
    if len(token) > 5 and token[5] is None:
        token[5] = time.monotonic()


def _span(token: list, margin: float = 0.0) -> tuple[float, float]:
    """The token's span on the monotonic clock: to its end (plus `margin`), or while open to now, at most its
    deadline."""
    began, ended, deadline = token[4], token[5], token[6]
    end = ended + margin if ended is not None else min(time.monotonic(), deadline)
    return began, end


def front_taken(bundle: str, began: float | None = None) -> list:
    """A task's app `bundle` has the front since `began` (now when not given); a token for front_given_back. Told
    before the front is taken, so a key whose callback runs while the app is in front finds the flash, and ended after
    the front is given back, so one whose callback runs after the give-back finds it too."""
    token = _token(bundle, began, FLASH_MAX_S)
    FLASHES[:] = (FLASHES + [token])[-32:]
    return token


def front_given_back(token: list | None) -> None:
    if token is not None:
        _ended(token)


def flashes_between(a: float, b: float) -> bool:
    """Whether a flash (the driver's, staging's, a launch's) was open at some point in [a, b], monotonic seconds."""
    for token in list(FLASHES):
        lo, hi = _span(token, FLASH_MARGIN_S)
        if lo <= b and hi >= a:
            return True
    return False


def flash_at(at: float) -> list[str]:
    """Every app a flash had in front at `at` (to FLASH_MARGIN_S after it ended), newest first; [] for none. All of
    them, not the newest only: a flash told from a step's start for the app found in front at its end (Finder) also
    covers the step's own inner flash (Notes), and a key in it belongs to either (hands#62 re-review at c0c5a4f)."""
    out = []
    for bundle, began, ended, deadline, *_ in reversed(list(FLASHES)):
        end = ended if ended is not None else min(time.time(), deadline)
        if bundle and began <= at <= end + FLASH_MARGIN_S and bundle not in out:
            out.append(bundle)
    return out


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
        #: pid -> its process's start time when it was last looked up (_check_pid)
        self._pid_started: dict[int, float] = {}
        #: pids looked up while their start time could not be read: not trusted once it can be (_check_pid)
        self._pid_unsure: set[int] = set()
        #: a Command-Tab (or Command-`) app switcher open: from its first Tab until Command is let go (_switcher)
        self._switching = False
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
        #: the front as observed: reads (pid (0: could not be read), begun, answered), monotonic seconds, oldest first;
        #: each proves its pid had the keyboard at some instant between its start and its answer (front_seen)
        self._front_reads: collections.deque = collections.deque(maxlen=FRONT_READS)
        self._front_seen = threading.Condition()
        self._poll_stop: threading.Event | None = None
        self._poll_thread: threading.Thread | None = None
        self._read_began = 0.0
        self._loop = None
        #: the system disabled the tap at least once (timeout, user input): keys may have gone unseen (_callback)
        self.keys_lost = False
        #: when a key with no app target was last counted only because nothing proved where it went (note's rule):
        #: counted as main counted it -- an input to an unknown target, wiped by the mark main set at an action's
        #: end (last_input_where's doubt_after)
        self.last_doubt_at = 0.0

    # -- classification (pure, testable) ---------------------------------------------------------------------
    def _check_pid(self, pid: int) -> None:
        """Forget what is known of `pid` when it now belongs to another process (its start time changed): the watch
        lives as long as the process (executor.shared_watch), and a pid reused for the task's app kept the old app's
        bundle (hands#62 delta review at f0f7186). The start time, as posted_by_us tells a reused pid.
        A pid whose start time cannot be read is asked anew at every look, and what was learnt of it then is not
        trusted once the start time can be read (hands#62 delta review at 45e67fc: the terminal's pid, cached with no
        start time, reused by the task's app, kept the terminal's bundle)."""
        info = _proc_info(pid)
        started = info[1] if info else None
        known = self._pid_started.get(pid)
        if started is None or pid in self._pid_unsure or (known is not None and started != known):
            self._pid_bundle.pop(pid, None)
            self._pid_regular.pop(pid, None)
            self._pid_started.pop(pid, None)
        if started is None:
            self._pid_unsure.add(pid)
        else:
            self._pid_unsure.discard(pid)
            self._pid_started[pid] = started

    def is_own(self, pid: int | None) -> bool:
        if not pid:
            return False
        self._check_pid(pid)
        bundle = self._pid_bundle.get(pid)
        if bundle is None:
            bundle = self._bundle_of(pid)
            self._pid_bundle[pid] = bundle
        return bundle.lower() in self.own_bundles

    def note(self, pid: int | None, at: float | None = None, key: bool = False, age: float | None = None,
             t0: float | None = None, switcher: bool = False) -> None:
        """An input event the window server routed to `pid`'s process (None or 0: no target known). `age` is the
        event's age at `t0` (time.monotonic(), sampled once when the callback began; now when not given). `at`: a
        fixed record time, for tests.

        A key whose target is neither an ordinary app nor DeskMind: main counted every such key (an unknown target),
        and that is the floor. It is left uncounted for the task only on positive evidence that no app of the task's
        could have had it (hands#62 review at e8fa010; polling never sees every moment, so the record alone cannot
        prove where a key went):
        1. no driver action that can bring an app forward -- a step, a flash, staging, a launch (in_flight) -- was
           under way at any point of the key's span, from FRONT_MARGIN_S before its own time to the callback's start
           (the annotated tap sees an event after its target was decided);
        2. the observed front has no unseen time over that span (fronts_between);
        3. a read of the app with the keyboard at the callback answered (front_now), and it is among the apps seen;
        4. every app seen is then recorded for, and the task's apps are not among them -- decided where the task's
           apps are known (last_input_for).
        With that evidence the key goes to every app seen (a front unreadable or no ordinary app: unknown, it counts;
        DeskMind alone: DeskMind's). Without it, it is recorded as a doubt (last_doubt_at), counted as main counted
        it -- wiped only by the mark main moved at each action's end -- and also, as before, to every app the
        evidence there is names: a flash open at its time, the apps seen, a fresh read, else the step under way at
        its time (each counted whatever the mark). The key's own time is the monotonic t0 - age, never the wall clock
        at a later moment (the tap's thread held between t0 and here placed it late).

        In three phases (hands#62 external review, twice): 1. every lookup, nothing recorded; 2. the decision; 3. the
        record, under `lock`, its time taken last, inside the lock (`stamp`) -- the lock the executor holds when it
        stamps the epoch's mark (`mark`). Invariants: nothing learned after a key's freshness window can un-count it;
        and a key's record time is never earlier than the moment it becomes visible."""
        t0 = time.monotonic() if t0 is None else t0
        wall0, mono0 = time.time(), time.monotonic()

        def kind(p: int) -> str:
            if self.is_own(p):
                return "own"
            return "regular" if p and self.is_regular(p) else "unknown"

        def bundle(p: int) -> str:
            known = self._pid_bundle.get(p)                # not kept for a pid whose start time is unreadable
            return (known if known is not None else self._bundle_of(p) or "").lower()

        # -- 1. lookups: nothing shared is written (only the lookups' own caches)
        target = kind(pid) if pid else "unknown"
        # the rule's scope: a key with no target, or the Dock's (a Command chord). A key the window server sent to
        # another process that is no ordinary app -- a launcher's panel, Spotlight -- counts as on main: a system
        # surface can act on any app (PR #49; hands#62 delta review at f0f7186)
        if key and target == "unknown" and pid and bundle(pid) != "com.apple.dock":
            key = False
        known_age = age is not None and age >= 0
        ev_mono = t0 - age if known_age else None
        # the key's own time on the wall clock now (flashes and steps are told in epoch seconds): from t0, so a
        # callback held before this point does not move it
        event_at = (at if at is not None else wall0 - (mono0 - t0)) - (age if known_age else 0.0)
        doubt, seen, read, step, flashes, flashed_first = False, None, 0, "", [], False
        if key and target not in ("own", "regular"):
            flashes = list(self.flash_at(event_at))
            flashed_first = bool(flashes)                  # a flash found before the read: the read names no app
            span = (ev_mono - FRONT_MARGIN_S, t0) if ev_mono is not None else None
            seen = self.fronts_between(*span) if span is not None else None
            read = self.front_now()
            step = self.step_at(event_at)
            busy = span is None or self.in_flight(*span)
            flashes += [b for b in self.flash_at(event_at) if b not in flashes]    # the last lookup (50f176a)
            # a key in the app switcher (Command-Tab, then Q or H on the app picked) acts on an app the front does not
            # name: it counts as on main
            doubt = busy or seen is None or not read or switcher
        # -- 2. the decision
        fresh = age is not None and 0.0 <= age + (time.monotonic() - t0) <= KEY_FRESH_S
        unknown = False
        bundles: list[str] = []
        if key and target not in ("own", "regular"):
            kinds = {p: kind(p) for p in (seen or [])}
            read_kind = kind(read) if read else "unknown"
            if seen is not None and read and read not in seen:
                doubt = True                               # the read at the callback is not among the apps seen
            if doubt:
                # nothing proves where it went: counted as main counted it, and for every app the evidence names --
                # the apps seen over its time, and as before the record: a flash open then (with the step's app and
                # a fresh read's), else a fresh read's app, else the step's (the step only as the fallback: it spans
                # the driver's wait for a still Mac, hands#62 re-review). A read past the key's freshness names no app.
                read_named = ([bundle(read)] if read and fresh and read_kind == "regular" and not flashed_first
                              else [])
                if flashes:
                    bundles = flashes + ([step] if step else []) + read_named
                elif read and fresh and read_kind in ("regular", "own"):
                    bundles = read_named
                else:
                    bundles = [step] if step else []
                bundles += [bundle(p) for p in kinds if kinds[p] == "regular"]
                unknown = any(k == "unknown" for k in kinds.values())
                dest = "doubt"
            else:
                kinds[read] = read_kind
                bundles = [bundle(p) for p in kinds if kinds[p] == "regular"]
                unknown = any(k == "unknown" for k in kinds.values())
            if doubt:
                pass
            elif flashes or bundles or unknown:
                bundles = flashes + bundles
                dest = "seen"
            else:
                dest = "own"                               # DeskMind alone in front, on the evidence
        elif target == "regular":
            dest, bundles = "regular", [bundle(pid)]
        else:
            dest = target
        bundles = [b for b in dict.fromkeys(bundles) if b]
        # -- 3. the record, its time taken last, under the lock the epoch's mark is stamped under
        with self.lock:
            when = at if at is not None else self.stamp()
            if dest == "own":
                self.last_own_at = max(self.last_own_at, when)
                return
            self.last_external_at = max(self.last_external_at, when)
            for b in bundles:
                self.last_by_bundle[b] = max(self.last_by_bundle.get(b, 0.0), when)
            if dest == "doubt":
                self.last_doubt_at = max(self.last_doubt_at, when)
                if unknown:
                    self.last_unknown_at = max(self.last_unknown_at, when)
            elif (not bundles and dest != "doubt") or unknown:
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
        token = _token(bundle, None, max_s)
        self._steps = (self._steps + [token])[-32:]
        return token

    @staticmethod
    def step_ended(token: list) -> None:
        _ended(token)

    def in_flight(self, a: float, b: float) -> bool:
        """Whether a driver action that can bring an app forward was under way at some point in [a, b] (monotonic
        seconds): a step of the task's, a flash, staging, a launch -- each told before it starts and ended in a
        finally (step_began, front_taken)."""
        if flashes_between(a, b):
            return True
        for token in list(self._steps):
            lo, hi = _span(token)
            if lo <= b and hi >= a:
                return True
        return False

    @staticmethod
    def flash_at(at: float) -> list[str]:
        """The apps the driver's flashes had in front at `at` (module flash_at)."""
        return flash_at(at)

    def step_at(self, at: float) -> str:
        """The app of the task's step under way at `at` (within STEP_MARGIN_S of one), or ""."""
        for bundle, began, ended, deadline, *_ in reversed(self._steps):
            end = ended if ended is not None else min(time.time(), deadline)
            if bundle and began - STEP_MARGIN_S <= at <= end + STEP_MARGIN_S:
                return bundle
        return ""

    # -- the front, observed ---------------------------------------------------------------------------------
    def front_seen(self, pid: int, began: float | None = None, ended: float | None = None) -> None:
        """A read of the app with the keyboard: `pid` (0: it could not be read), the read begun at `began` and
        answered at `ended` (monotonic seconds; now when not given). It proves `pid` had the keyboard at one unknown
        instant between the two, nothing more: not from its start, not until the next read (hands#62 review at
        f0f7186: stamped at its start, a slow read that answered "terminal" ended TextEdit's time too early)."""
        began = time.monotonic() if began is None else began
        ended = began if ended is None else max(ended, began)
        with self._front_seen:
            reads = self._front_reads
            if reads and began < reads[-1][1]:
                return                                     # out of order: never moves the record back
            reads.append((pid, began, ended))
            self._front_seen.notify_all()

    def _poller_live(self) -> bool:
        """The poller runs and its last read began within FRONT_DEADLINE_S: one that hangs, or a thread that died, is
        not waited for (hands#62 review at e8fa010: the tap's thread was held 100 ms a key)."""
        t = self._poll_thread
        return (t is not None and t.is_alive() and self._poll_stop is not None and not self._poll_stop.is_set()
                and time.monotonic() - self._read_began <= FRONT_DEADLINE_S)

    def fronts_between(self, a: float, b: float, wait_s: float | None = None) -> list[int] | None:
        """Every pid the reads saw from the last read answered by `a` to the first read begun at or after `b`
        (monotonic seconds) -- all the apps that can have had the keyboard over [a, b] for longer than FRONT_GAP_S --
        or None when the reads cannot say that: no read answered by `a`, none begun at or after `b` (waited for,
        `wait_s`, FRONT_DEADLINE_S by default, only while the poller is live), or two consecutive reads between them
        further apart than FRONT_GAP_S from the one's start to the other's answer (a slow read, a stalled poller)."""
        wait_s = FRONT_DEADLINE_S if wait_s is None else wait_s
        until = time.monotonic() + wait_s
        with self._front_seen:
            while (not self._front_reads or self._front_reads[-1][1] < b) and self._poller_live():
                left = until - time.monotonic()
                if left <= 0:
                    break
                self._front_seen.wait(min(left, FRONT_POLL_S))
            reads = list(self._front_reads)
        first = max((k for k, r in enumerate(reads) if r[2] <= a), default=None)
        if first is None:
            return None
        last = next((k for k in range(first + 1, len(reads)) if reads[k][1] >= b), None)
        if last is None:
            return None
        out: list[int] = []
        for k in range(first, last + 1):
            if k > first and reads[k][2] - reads[k - 1][1] > FRONT_GAP_S:
                return None                                # time over the span no read can vouch for
            if reads[k][0] not in out:
                out.append(reads[k][0])
        return out

    def polling(self) -> bool:
        return self._poll_stop is not None and not self._poll_stop.is_set()

    def watch_front(self) -> None:
        """Read the app with the keyboard every FRONT_POLL_S on a thread of its own, into front_seen, until
        stop_front(). A read that hangs leaves a gap, and a key over a gap is counted as main counts it (note)."""
        if self.polling() and self._poll_thread is not None and self._poll_thread.is_alive():
            return
        stop = self._poll_stop = threading.Event()

        def poll():
            while not stop.is_set():
                began = self._read_began = time.monotonic()
                try:
                    pid = int(self.front_pid() or 0)
                except Exception:  # noqa: BLE001 -- not known: recorded as such, and it counts
                    pid = 0
                self.front_seen(pid, began, time.monotonic())
                stop.wait(FRONT_POLL_S)
        self._read_began = time.monotonic()
        self._poll_thread = threading.Thread(target=poll, name="input-watch-front-poll", daemon=True)
        self._poll_thread.start()

    def stop_front(self) -> None:
        if self._poll_stop is not None:
            self._poll_stop.set()
        with self._front_seen:
            self._front_seen.notify_all()

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

    def last_input_for(self, bundles: set[str], doubt_after: float | None = None) -> float:
        """When input last went to one of `bundles` (lowercased), or anywhere unknown: the safe side."""
        return self.last_input_where(bundles, doubt_after)[0]

    def last_input_where(self, bundles: set[str], doubt_after: float | None = None) -> tuple[float, str]:
        """(when, where) of the latest input to one of `bundles` or to an unknown target: where is the bundle id, or
        "unknown" (a system surface, a drag, an event that could not be read). The same in every run: the bundles in
        sorted order, and at the same time a task app ahead of "unknown" (a key counted for several apps at once)."""
        best = (self.last_unknown_at, "unknown")
        # a key counted as main counted it (note's rule): main's mark, moved at each action's end, wiped one recorded
        # before it -- `doubt_after` is that mark (None: it always counts)
        if self.last_doubt_at > best[0] and (doubt_after is None or self.last_doubt_at > doubt_after):
            best = (self.last_doubt_at, "unknown")
        for b in sorted({x.lower() for x in bundles}):
            t = self.last_by_bundle.get(b, 0.0)
            if t > best[0] or (t and t == best[0] and best[1] == "unknown"):
                best = (t, b)
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
        self._check_pid(pid)
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
                # the system turned the tap off for a while: the keys pressed then were never seen, so what this
                # watch says of keys is not trusted from here on -- the executor counts key presses by the HID idle
                # time, as main did -- and the next begin makes a new watch (healthy); turned back on meanwhile
                self.keys_lost = True
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
            switcher = self._switcher(Quartz, type_, event) if key else False
            self.note(int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventTargetUnixProcessID) or 0),
                      key=key, age=age, t0=t0, switcher=switcher)
        except Exception:  # noqa: BLE001 -- unknown where it went: it counts, the safe side (an unknown target)
            self.note(None)
        return event

    #: Tab and ` (the key above Tab on a US keyboard): with Command held they open the app switcher
    SWITCHER_KEYCODES = (48, 50)
    COMMAND_FLAG = 1 << 20                                 # kCGEventFlagMaskCommand

    def _switcher(self, Quartz, type_, event) -> bool:
        """Whether this key belongs to an app switcher session: from a Command-Tab, Command-` or Command-Shift-Tab
        keydown until Command is let go, the release included. In the switcher the user picks an app and acts on it
        -- Q quits it, H hides it -- while the front stays where it was, and each key comes with the Dock as its
        target (hands#62 delta review at 45e67fc). Such keys count as on main. Where the event cannot be read, no."""
        try:
            flags = int(Quartz.CGEventGetFlags(event) or 0)
            command = bool(flags & self.COMMAND_FLAG)
            if type_ == Quartz.kCGEventKeyDown:
                code = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode) or 0)
                if command and code in self.SWITCHER_KEYCODES:
                    self._switching = True
                elif not command:
                    self._switching = False
                return self._switching
            if self._switching and not command:            # Command let go: the switcher closes on this one
                self._switching = False
                return True
            return self._switching
        except Exception:  # noqa: BLE001
            return self._switching

    @staticmethod
    def _event_age(Quartz, event) -> float | None:
        """How long ago the event happened (seconds, by its own timestamp); None when that cannot be read."""
        try:
            return event_age(int(Quartz.CGEventGetTimestamp(event) or 0))
        except Exception:  # noqa: BLE001
            return None

    @property
    def keys_trusted(self) -> bool:
        """Keys are watched, and the tap has seen every one since it started (the system never disabled it)."""
        return bool(self.keys and not self.keys_lost)

    def healthy(self) -> bool:
        """Whether this watch still sees what it saw when it started: keys watched exactly when Input Monitoring is
        granted now (in both directions), the tap enabled and never disabled by the system, the front's poller alive
        where keys are watched. A shared watch that is not is made anew (executor.shared_watch; hands#62 review at
        714c989: a permission revoked left a watch that saw no keys and still said it did)."""
        if not self.active or self.keys_lost or self.keys != self.keys_permitted():
            return False
        try:
            import Quartz
            if self._tap is None or not Quartz.CGEventTapIsEnabled(self._tap):
                return False
        except Exception:  # noqa: BLE001 -- cannot be told: made anew
            return False
        t = self._poll_thread
        return not self.keys or (t is not None and t.is_alive() and not (self._poll_stop and self._poll_stop.is_set()))

    #: How long start() waits for the tap's thread to be running before it gives up.
    START_WAIT_S = 2.0

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
        ready, gone, life = threading.Event(), threading.Event(), threading.Lock()

        def run():
            # A thread starved past START_WAIT_S finds the start given up and the tap's port invalidated: a run loop
            # source made from that port is None, and adding None to a run loop crashed the process (hands#62 delta
            # review at e31a20b, exit 139). So nothing is made once given up, a None source is never added, and the
            # hand-over -- given up or not, enabled, active, ready -- is one step under `life`, as the give-up is,
            # so `active` never turns true after stop().
            if gone.is_set():
                return
            src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
            if src is None:
                return
            loop = Quartz.CFRunLoopGetCurrent()
            with life:
                if gone.is_set():
                    return
                self._loop = loop
                Quartz.CFRunLoopAddSource(loop, src, Quartz.kCFRunLoopCommonModes)
                Quartz.CGEventTapEnable(tap, True)
                self.active = True
                ready.set()
            Quartz.CFRunLoopRun()

        self._thread = threading.Thread(target=run, name="input-watch", daemon=True)
        self._thread.start()
        if not ready.wait(self.START_WAIT_S):
            # not running in time: given up, and nothing of it left alive -- each executor tries again now, and an
            # unreferenced live tap and poller were left once per try (hands#62 delta review at 45e67fc)
            with life:
                if not ready.is_set():                     # it may have got there since the wait gave up
                    gone.set()
                    self.stop()
                    return False
        if self.active and self.keys:
            self.watch_front()                             # only keys need the front: a click has its target
        return self.active

    def stop(self) -> None:
        """Stop the poller, disable and invalidate the tap, and stop its run loop: nothing of this watch stays alive."""
        self.stop_front()
        self.active = False
        try:
            import Quartz
            if self._tap is not None:
                Quartz.CGEventTapEnable(self._tap, False)
                Quartz.CFMachPortInvalidate(self._tap)
            if self._loop is not None:
                Quartz.CFRunLoopStop(self._loop)
        except Exception:  # noqa: BLE001
            pass
