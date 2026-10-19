"""Which application the user's input goes to, so input to DeskMind itself is not "the user changed something".

The executor's epoch moves when the user touched the keyboard or mouse since it was set (an observation and a
consent decided before are stale upstream). The user's click on DeskMind's own consent card is input too, and
counted it made the action it approved stale, re-observed and re-asked without end (PR #48 review, third round).
Whether the window was changed cannot be read off the window (a table's selection is not in the observation), so
the input is told by where it went: a listen-only event tap records each mouse and key event's target process --
the owner of the frontmost window under the pointer, or the frontmost application for a key -- and input to
DeskMind (the app and its helper) is left out. Where no tap can be made (the permission is not there), the caller
falls back to the HID idle time, where every input counts.

Listen-only: nothing is changed or delayed; the tap reads what the window server already delivered.
"""
from __future__ import annotations

import threading
import time

#: DeskMind's own processes: input to them is the user answering DeskMind, not changing their Mac.
OWN_BUNDLES = {"ai.deskmind.app", "ai.deskmind.hands"}


def target_of_point(x: float, y: float, windows: list[dict]) -> int | None:
    """The owner pid of the frontmost on-screen window containing the point, from the window server's list in
    front-to-back order (every layer: DeskMind's card is a floating panel above the ordinary windows). Windows the
    pointer cannot hit (fully transparent) are skipped. None when nothing is there."""
    for w in windows:
        if float(w.get("kCGWindowAlpha", 1.0) or 0.0) <= 0.0:
            continue
        b = w.get("kCGWindowBounds") or {}
        bx, by, bw, bh = (float(b.get("X", 0)), float(b.get("Y", 0)), float(b.get("Width", 0)), float(b.get("Height", 0)))
        if bw <= 0 or bh <= 0:
            continue
        if bx <= x <= bx + bw and by <= y <= by + bh:
            pid = w.get("kCGWindowOwnerPID")
            return int(pid) if pid is not None else None
    return None


class InputWatch:
    """Records when the user last sent input to an application other than DeskMind (`last_external_at`, epoch
    seconds; 0 before any). `start()` makes the tap on its own thread and returns whether it could."""

    def __init__(self, own_bundles: set[str] = OWN_BUNDLES):
        self.own_bundles = {b.lower() for b in own_bundles}
        self.last_external_at = 0.0
        self.last_own_at = 0.0
        self.active = False
        #: Whether the tap sees key events too. A listen-only key tap needs Input Monitoring; when only the mouse
        #: tap could be made, the caller counts every key press (by the HID idle time), the safe side.
        self.keys = False
        self._pid_bundle: dict[int, str] = {}
        self._thread: threading.Thread | None = None
        self._tap = None

    # -- classification (pure, testable) ---------------------------------------------------------------------
    def is_own(self, pid: int | None) -> bool:
        if pid is None:
            return False
        bundle = self._pid_bundle.get(pid)
        if bundle is None:
            bundle = self._bundle_of(pid)
            self._pid_bundle[pid] = bundle
        return bundle.lower() in self.own_bundles

    def note(self, pid: int | None, at: float | None = None) -> None:
        """An input event went to `pid`'s process at `at`."""
        at = at or time.time()
        if self.is_own(pid):
            self.last_own_at = max(self.last_own_at, at)
        else:
            self.last_external_at = max(self.last_external_at, at)

    # -- macOS ---------------------------------------------------------------------------------------------
    @staticmethod
    def _bundle_of(pid: int) -> str:
        try:
            from AppKit import NSRunningApplication
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            return str(app.bundleIdentifier() or "") if app else ""
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _front_pid() -> int | None:
        try:
            from AppKit import NSWorkspace
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            return int(app.processIdentifier()) if app else None
        except Exception:  # noqa: BLE001
            return None

    def _target(self, type_, event) -> int | None:
        import Quartz
        if type_ in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp, Quartz.kCGEventFlagsChanged):
            return self._front_pid()
        loc = Quartz.CGEventGetLocation(event)
        wins = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, 0) or []
        return target_of_point(loc.x, loc.y, list(wins))

    def _callback(self, proxy, type_, event, refcon):
        try:
            import Quartz
            if type_ in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
                if self._tap is not None:
                    Quartz.CGEventTapEnable(self._tap, True)
                return event
            self.note(self._target(type_, event))
        except Exception:  # noqa: BLE001 -- unknown where it went: it counts, the safe side
            self.last_external_at = time.time()
        return event

    def start(self) -> bool:
        """Make the tap and run it on its own thread; False when the system refuses (no Input Monitoring /
        Accessibility for this process), in which case nothing is recorded and `active` stays False."""
        try:
            import Quartz
        except ImportError:
            return False
        mouse = ("kCGEventLeftMouseDown", "kCGEventRightMouseDown", "kCGEventOtherMouseDown", "kCGEventScrollWheel")
        keys = ("kCGEventKeyDown", "kCGEventFlagsChanged")
        tap = None
        for names in (mouse + keys, mouse):
            mask = 0
            for t in names:
                mask |= Quartz.CGEventMaskBit(getattr(Quartz, t))
            tap = Quartz.CGEventTapCreate(Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                                          Quartz.kCGEventTapOptionListenOnly, mask, self._callback, None)
            if tap:
                self.keys = names is not mouse
                break
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
