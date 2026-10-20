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
