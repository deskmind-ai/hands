"""Where the user's input went: input to DeskMind is the user answering DeskMind, not changing their Mac."""
from __future__ import annotations

import os
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from deskmind_hands.inputwatch import InputWatch


def stable_pids(case: unittest.TestCase, *pids: int) -> None:
    """The made-up `pids` a test injects into a watch's caches: processes whose start time does not change
    (InputWatch._check_pid forgets a pid whose start time is unreadable or changed); every other pid as the system
    says."""
    from deskmind_hands import inputwatch as iw
    real = iw._proc_info
    patch = mock.patch.object(iw, "_proc_info", lambda pid: (1, 1.0) if pid in pids else real(pid))
    patch.start()
    case.addCleanup(patch.stop)


class Targets(unittest.TestCase):
    def test_input_the_window_server_routed_to_deskmind_is_not_the_users_change(self):
        stable_pids(self, 11, 12, 22)
        w = InputWatch()
        w._pid_bundle = {11: "ai.deskmind.app", 12: "ai.deskmind.hands", 22: "com.apple.TextEdit"}
        w._pid_regular = {11: True, 12: False, 22: True}
        w.note(11, at=100.0)
        w.note(12, at=101.0)
        self.assertEqual(w.last_external_at, 0.0)          # the user answered DeskMind's card: no change
        self.assertEqual(w.last_own_at, 101.0)
        w.note(22, at=102.0)                               # a click that went through the card to the document
        self.assertEqual(w.last_external_at, 102.0)
        w.note(0, at=103.0)                                # no target known: it counts
        self.assertEqual(w.last_external_at, 103.0)
        w.note(None, at=104.0)
        self.assertEqual(w.last_external_at, 104.0)
        # by application: the textedit click, and the unknown targets on the safe side
        self.assertEqual(w.last_input_for({"com.apple.textedit"}), 104.0)   # the unknown one is later
        self.assertEqual(w.last_by_bundle, {"com.apple.textedit": 102.0})
        self.assertEqual(w.last_input_for(set()), 104.0)

    def test_keys_are_watched_only_with_the_permission_not_because_a_tap_was_made(self):
        """CGEventTapCreate may drop a mask's key bits and still return a tap (Apple's documentation), so the
        permission itself decides, and without it every key press counts by the HID idle time."""
        import sys
        import types
        # a stand-in for the macOS Quartz module, so this runs where there is none (Linux CI): inputwatch imports
        # it inside start()
        q = types.ModuleType("Quartz")
        consts = {"kCGEventLeftMouseDown": 1, "kCGEventRightMouseDown": 3, "kCGEventOtherMouseDown": 25, "kCGEventScrollWheel": 22,
                  "kCGEventKeyDown": 10, "kCGEventFlagsChanged": 12,
                  "kCGEventLeftMouseDragged": 6, "kCGEventRightMouseDragged": 7, "kCGEventOtherMouseDragged": 27,
                  "kCGEventLeftMouseUp": 2, "kCGEventRightMouseUp": 4, "kCGEventOtherMouseUp": 26, "kCGAnnotatedSessionEventTap": 2, "kCGHeadInsertEventTap": 0,
                  "kCGEventTapOptionListenOnly": 1}
        for k, v in consts.items():
            setattr(q, k, v)
        q.CGEventMaskBit = lambda b: 1 << b
        q.CGPreflightListenEventAccess = lambda: False
        q.CGEventTapCreate = mock.Mock(return_value=object())
        w = InputWatch()
        with mock.patch.dict(sys.modules, {"Quartz": q}), \
             mock.patch.object(InputWatch, "_callback"), \
             mock.patch("threading.Thread") as thread:
            thread.return_value.start = lambda: None
            w.start()
        self.assertFalse(w.keys)
        make = q.CGEventTapCreate
        mask = make.call_args[0][3]
        self.assertFalse(mask & (1 << consts["kCGEventKeyDown"]))            # not even asked for
        self.assertTrue(mask & (1 << consts["kCGEventLeftMouseDown"]))
        self.assertEqual(make.call_args[0][0], consts["kCGAnnotatedSessionEventTap"])   # after the window server's routing
        # with the permission, keys are asked for
        q.CGPreflightListenEventAccess = lambda: True
        w2 = InputWatch()
        with mock.patch.dict(sys.modules, {"Quartz": q}), \
             mock.patch.object(InputWatch, "_callback"), \
             mock.patch("threading.Thread") as thread:
            thread.return_value.start = lambda: None
            w2.start()
        self.assertTrue(w2.keys)
        self.assertTrue(make.call_args[0][3] & (1 << consts["kCGEventKeyDown"]))

    def test_input_to_a_system_surface_counts_as_an_unknown_target(self):
        """A notification banner, the Dock, Spotlight: not ordinary apps, and a click there can bring another window
        of any app forward -- input to an unknown target (PR #49 review)."""
        stable_pids(self, 30, 31)
        w = InputWatch()
        w._pid_bundle = {30: "com.apple.notificationcenterui", 31: "com.googlecode.iterm2"}
        w._pid_regular = {30: False, 31: True}
        w.note(31, at=10.0)                               # an ordinary app the task does not operate
        self.assertEqual(w.last_input_for({"com.apple.textedit"}), 0.0)
        w.note(30, at=11.0)                               # the banner
        self.assertEqual(w.last_input_for({"com.apple.textedit"}), 11.0)

    def test_a_drag_and_its_release_count_as_input_to_an_unknown_target(self):
        """A file dragged from Finder into the task's window: the press is Finder's, the drop is not reported to
        anyone -- the drag is input to an unknown target (PR #49 review)."""
        import sys
        import types
        q = types.ModuleType("Quartz")
        for k, v in {"kCGEventTapDisabledByTimeout": -2, "kCGEventTapDisabledByUserInput": -1,
                     "kCGEventLeftMouseDragged": 6, "kCGEventRightMouseDragged": 7, "kCGEventOtherMouseDragged": 27,
                     "kCGEventLeftMouseUp": 2, "kCGEventRightMouseUp": 4, "kCGEventOtherMouseUp": 26,
                     "kCGEventTargetUnixProcessID": 40, "kCGEventSourceUnixProcessID": 41}.items():
            setattr(q, k, v)
        q.CGEventGetIntegerValueField = lambda e, f: 0
        w = InputWatch()
        with mock.patch.dict(sys.modules, {"Quartz": q}):
            w._callback(None, 2, object(), None)          # a plain release: nothing
            self.assertEqual(w.last_unknown_at, 0.0)
            w._callback(None, 6, object(), None)          # dragging
            t = w.last_unknown_at
            self.assertGreater(t, 0.0)
            w._callback(None, 2, object(), None)          # the drop
            self.assertGreaterEqual(w.last_unknown_at, t)
            self.assertFalse(w._dragging)

    def test_events_the_driver_posts_are_not_the_users_input(self):
        """A click or key the driver (this process, or its Peekaboo child) posts reaches the tap like the user's; it
        is skipped by its source pid (rc.9, 10-20: a driver click counted as the user's staled the next step)."""
        import os
        import sys
        import types
        from deskmind_hands import inputwatch as iw
        w = InputWatch()
        self.assertTrue(w.posted_by_us(os.getpid()))
        self.assertFalse(w.posted_by_us(0))                # a hardware event
        iw.OWN_CHILDREN[999222] = [None, None]             # pretend: a child of ours, gone before being reaped
        self.addCleanup(iw.OWN_CHILDREN.pop, 999222, None)
        self.assertTrue(w.posted_by_us(999222))
        self.assertFalse(w.posted_by_us(999333))           # gone, and never ours
        q = types.ModuleType("Quartz")
        for k, v in {"kCGEventTapDisabledByTimeout": -2, "kCGEventTapDisabledByUserInput": -1,
                     "kCGEventLeftMouseDragged": 6, "kCGEventRightMouseDragged": 7, "kCGEventOtherMouseDragged": 27,
                     "kCGEventLeftMouseUp": 2, "kCGEventRightMouseUp": 4, "kCGEventOtherMouseUp": 26,
                     "kCGEventKeyDown": 10, "kCGEventFlagsChanged": 12,
                     "kCGEventTargetUnixProcessID": 40, "kCGEventSourceUnixProcessID": 41}.items():
            setattr(q, k, v)
        fields = {}
        q.CGEventGetIntegerValueField = lambda e, f: fields.get(f, 0)
        stable_pids(self, 500)
        w._pid_bundle, w._pid_regular = {500: "com.apple.Safari"}, {500: True}
        with mock.patch.dict(sys.modules, {"Quartz": q}):
            fields.update({41: 999222, 40: 500})           # the driver's click into Safari
            w._callback(None, 1, object(), None)
            self.assertEqual(w.last_by_bundle, {})
            fields.update({41: 0})                          # the user's
            w._callback(None, 1, object(), None)
            self.assertIn("com.apple.safari", w.last_by_bundle)

    def test_a_short_lived_child_is_known_by_its_pid_even_after_it_exits(self):
        """A peekaboo CLI child posts its click and is gone before the event reaches the tap: recorded at spawn, it is
        still ours; a grandchild is ours by its parent chain (PR #53 review)."""
        import subprocess
        import sys as _sys
        from deskmind_hands import inputwatch as iw
        iw.track_children()
        w = InputWatch()
        p = subprocess.Popen([_sys.executable, "-c", "pass"])
        p.wait()
        self.assertTrue(w.posted_by_us(p.pid))            # exited, still known
        # a child that ran longer than OWN_KEEP_S: still ours right after it exits, then not
        import time as _t
        with mock.patch.object(iw, "OWN_KEEP_S", 0.5):
            long = subprocess.Popen([_sys.executable, "-c", "import time; time.sleep(0.8)"])
            long.wait()
            self.assertTrue(w.posted_by_us(long.pid))
            _t.sleep(0.6)
            self.assertFalse(w.posted_by_us(long.pid))
        if not iw.libproc_available():
            return                                        # the parent chain is read with macOS's libproc
        q = subprocess.Popen([_sys.executable, "-c", "import subprocess,sys,time; c=subprocess.Popen([sys.executable,'-c','import time; time.sleep(2)']); print(c.pid, flush=True); c.wait()"],
                             stdout=subprocess.PIPE, text=True)
        grandchild = int(q.stdout.readline())
        self.assertTrue(w.posted_by_us(grandchild))       # alive, its parent chain reaches us
        q.wait()
        self.assertFalse(w.posted_by_us(1))               # launchd is not ours

    def test_a_reused_pid_is_asked_anew(self):
        """Remembered by (pid, start time): the same pid given to another process later is not taken for ours."""
        from deskmind_hands import inputwatch as iw
        w = InputWatch()
        with mock.patch.object(iw, "_proc_info", return_value=(os.getpid(), 100.0)), \
             mock.patch.object(iw, "_descends_from", return_value=True):
            self.assertTrue(w.posted_by_us(424242))
        with mock.patch.object(iw, "_proc_info", return_value=(1, 200.0)), \
             mock.patch.object(iw, "_descends_from", return_value=False):
            self.assertFalse(w.posted_by_us(424242))       # a new process with that pid

    def test_an_event_that_cannot_be_read_counts_as_input_to_an_unknown_target(self):
        """The callback's failure path is the safe side for every reader, last_input_for included (PR #49 review)."""
        w = InputWatch()
        w._callback(None, 1, object(), None)              # not a CGEvent: reading its target raises
        self.assertGreater(w.last_unknown_at, 0.0)
        self.assertEqual(w.last_input_for({"com.apple.textedit"}), w.last_unknown_at)

    def test_the_executor_uses_the_watch_when_it_runs_and_the_idle_time_otherwise(self):
        from deskmind_hands.executor import HandsExecutor
        ex = HandsExecutor.__new__(HandsExecutor)
        ex._watch_tried, ex._watch = True, None
        ex.drivers, ex.current = {"com.apple.TextEdit": object()}, {}
        w = InputWatch()
        w.active, w.keys, w.last_external_at = True, True, 123.0
        w.last_by_bundle = {"com.apple.textedit": 123.0}
        ex._watch = w
        self.assertEqual(ex._last_input_at(), 123.0)
        w.keys = False                                      # no Input Monitoring: key presses count by the idle time too
        self.assertGreaterEqual(ex._last_input_at(), 123.0)
        w.active = False                                    # no tap: back to the idle time, every input counts
        self.assertNotEqual(ex._last_input_at(), 123.0)



NE = "com.netease.163music"
ITERM, MUSIC, DESKMIND, DOCK = 31, 32, 33, 34   # pids of iTerm2, NetEase Music, DeskMind, the Dock in these tests


def _quartz(fields: dict):
    """A stand-in for macOS's Quartz with what the tap's callback reads: event types, and the event's fields."""
    import types
    q = types.ModuleType("Quartz")
    for k, v in {"kCGEventTapDisabledByTimeout": -2, "kCGEventTapDisabledByUserInput": -1,
                 "kCGEventLeftMouseDown": 1, "kCGEventKeyDown": 10, "kCGEventFlagsChanged": 12,
                 "kCGEventLeftMouseDragged": 6, "kCGEventRightMouseDragged": 7, "kCGEventOtherMouseDragged": 27,
                 "kCGEventLeftMouseUp": 2, "kCGEventRightMouseUp": 4, "kCGEventOtherMouseUp": 26,
                 "kCGEventTargetUnixProcessID": 40, "kCGEventSourceUnixProcessID": 41,
                 "kCGKeyboardEventKeycode": 9}.items():
        setattr(q, k, v)
    q.CGEventGetIntegerValueField = lambda e, f: fields.get(f, 0)
    q.CGEventGetFlags = lambda e: fields.get("flags", 0)   # the modifiers held (Command: 1 << 20)
    # the event's own timestamp (mach_absolute_time's clock, in nanoseconds here): fresh unless a test says otherwise
    q.CGEventGetTimestamp = lambda e: fields.get("ts") or _uptime_ns()
    return q


def _uptime_ns() -> int:
    clock = getattr(time, "CLOCK_UPTIME_RAW", None)
    return time.clock_gettime_ns(clock) if clock is not None else time.monotonic_ns()


class _KeysCase(unittest.TestCase):
    """A watch fed through the tap's callback, a fake source for the app with the keyboard, and an executor whose
    task operates NetEase Music."""

    def setUp(self):
        import sys
        from deskmind_hands.executor import HandsExecutor
        self.fields = {}                                   # no target, no source: a hardware key with no target pid
        patcher = mock.patch.dict(sys.modules, {"Quartz": _quartz(self.fields)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.front = {"pid": ITERM}
        w = self.w = InputWatch()
        w.front_pid = lambda: self.front["pid"]            # the app in front, as the system would say
        w._pid_bundle = {ITERM: "com.googlecode.iterm2", MUSIC: NE, DESKMIND: "ai.deskmind.app", DOCK: "com.apple.dock"}
        w._pid_regular = {ITERM: True, MUSIC: True, DESKMIND: True, DOCK: False}
        w.active, w.keys = True, True
        ex = self.ex = HandsExecutor.__new__(HandsExecutor)
        ex._watch_tried, ex._watch = True, w
        ex.drivers, ex.current = {NE: object()}, {}        # the task operates NetEase Music
        # the driver's flashes are process-wide: none from another test (a launch now tells one from its start, and
        # one ended a moment ago still covers its margin)
        from deskmind_hands import inputwatch as iw
        # the test's pids are made up: each a process with a start time that does not change (_check_pid)
        stable = mock.patch.object(iw, "_proc_info", lambda pid: (1, 1.0))
        stable.start()
        self.addCleanup(stable.stop)
        iw.FLASHES.clear()
        self.addCleanup(iw.FLASHES.clear)

    def key(self):
        self.w._callback(None, 10, object(), None)

    def observe_front(self, settle: float = 0.15):
        """The watch's poller reading the mocked front, run long enough for a key's span to be seen: a key with no
        app target is left uncounted for the task only on that evidence (InputWatch.note; hands#62 review at
        e8fa010)."""
        self.w.watch_front()
        self.addCleanup(self.w.stop_front)
        threading.Event().wait(settle)

    def counted_unknown(self) -> bool:
        """Counted for no app in particular: an unknown target, or a key counted as main counted it (a doubt)."""
        return max(self.w.last_unknown_at, self.w.last_doubt_at) > 0.0


class KeysGoToTheAppInFront(_KeysCase):
    """A key event can reach the tap with no target, or with one that is no ordinary app (a Command-key press comes
    with the Dock as its target); keyboard input goes to the key window of the app in front, so it is input to the
    app that had the keyboard when it arrived (rc.13, 10-22: the owner working in iTerm2 while a task worked in
    NetEase Music came out as input to an unknown target and staled 4 steps of run_fd8ab281)."""

    def test_keys_with_no_target_while_another_app_is_in_front_do_not_stale_the_task(self):
        self.front["pid"] = ITERM                          # typing in the terminal
        self.observe_front()                               # the evidence it went there (none: it counts, as on main)
        self.key()
        self.w._callback(None, 12, object(), None)        # a modifier, too
        self.fields[40] = DOCK                             # Command-Tab and the like: the Dock is the key's target
        self.key()
        self.assertEqual(self.ex._last_input_at(), 0.0)    # noqa: SLF001
        self.assertEqual(self.w.last_unknown_at, 0.0)
        self.assertIn("com.googlecode.iterm2", self.w.last_by_bundle)

    def test_keys_during_the_flash_with_the_task_app_in_front_stale_it(self):
        self.front["pid"] = MUSIC                          # DeskMind brought the task's app forward for a step
        self.key()
        at = self.ex._last_input_at()                      # noqa: SLF001
        self.assertEqual(self.w.last_unknown_at, 0.0)
        self.assertGreater(at, 0.0)
        self.assertEqual(self.w.last_input_where({NE}), (at, NE))   # what epoch_reason.to says
        # the front is read when the key arrives, not later: the flash ending does not take the key back
        self.front["pid"] = ITERM
        self.assertEqual(self.ex._last_input_at(), at)     # noqa: SLF001

    def test_with_the_front_unknown_too_a_key_with_no_target_counts(self):
        for target in (0, DOCK):
            for unknown in (lambda: 0, lambda: None, mock.Mock(side_effect=RuntimeError("no accessibility"))):
                self.fields[40] = target
                self.w.front_pid = unknown
                self.w.last_unknown_at = self.w.last_doubt_at = self.w._front_backoff_until = 0.0
                self.key()
                self.assertTrue(self.counted_unknown())
                self.assertEqual(self.w.last_input_where({NE})[1], "unknown")
                self.assertGreater(self.ex._last_input_at(), 0.0)   # noqa: SLF001
        # the app with the keyboard is itself no ordinary app (Spotlight's field, say): unknown, it counts
        self.front["pid"] = DOCK
        self.w.front_pid = lambda: self.front["pid"]
        self.w.last_unknown_at = self.w.last_doubt_at = self.w._front_backoff_until = 0.0
        self.fields[40] = 0
        self.key()
        self.assertTrue(self.counted_unknown())
        self.w.last_unknown_at = self.w.last_doubt_at = self.w._front_backoff_until = 0.0
        self.observe_front()                               # seen, too: a front that is no ordinary app counts
        self.key()
        self.assertGreater(self.w.last_unknown_at, 0.0)

    def test_input_to_deskmind_stays_excluded(self):
        self.front["pid"] = DESKMIND                       # the user answering DeskMind's card from the keyboard
        self.observe_front()                               # with no front seen, a key with no target counts (main)
        self.key()
        self.fields[40] = DESKMIND                         # ... or a key the window server routed to it
        self.key()
        self.assertEqual(self.ex._last_input_at(), 0.0)    # noqa: SLF001
        self.assertEqual(self.w.last_external_at, 0.0)
        self.assertGreater(self.w.last_own_at, 0.0)

    def test_a_key_with_a_target_keeps_it_and_a_mouse_event_never_asks_the_front(self):
        self.front["pid"] = MUSIC
        self.fields[40] = ITERM                            # the window server named the key's target: that one
        self.key()
        self.assertEqual(self.ex._last_input_at(), 0.0)    # noqa: SLF001
        self.fields[40] = 0
        self.w._callback(None, 1, object(), None)          # a click with no target: unknown, as before
        self.assertGreater(self.w.last_unknown_at, 0.0)
        self.assertNotIn(NE, self.w.last_by_bundle)

    def test_the_epoch_reason_names_the_app_the_key_was_resolved_to(self):
        from test_executor import TE, W12, _executor, _win
        import time as _t
        ex, _ = _executor()
        del ex._last_input_at
        self.w._pid_bundle[MUSIC] = TE                     # here the task's app is TextEdit
        ex._watch, ex._watch_tried = self.w, True
        ex._windows = lambda every=False: [_win("12")]     # noqa: SLF001
        ex._front = lambda: "文本编辑"                       # noqa: SLF001
        ex.observe(W12)
        e0 = ex.snapshot()["epoch"]
        self.front["pid"] = ITERM
        self.observe_front()
        self.key()
        self.assertEqual(ex.snapshot()["epoch"], e0)       # typing in the terminal: no change
        _t.sleep(0.01)
        self.front["pid"] = MUSIC
        self.key()
        s = ex.snapshot()
        self.assertEqual(s["epoch"], e0 + 1)
        self.assertEqual((s["epoch_reason"]["by"], s["epoch_reason"]["to"]), ("input", TE.lower()))

class KeysAtTheFlashsEdge(_KeysCase):
    """The tap's callback runs when the interpreter lets it, which can be well after the key: while the executor
    works through a flash, up to about half a second (hands#62 review). A front read then is not the front the key
    met. Each test here fails on the PR's first head (0568e82)."""

    def test_a_late_callback_does_not_read_the_front_for_the_key(self):
        """A key typed in the task's app during a flash, its callback run after the front went back to iTerm2: the
        key counts, as before the PR, rather than being put on iTerm2. The front is read at the callback (evidence
        for the span, since e8fa010's review), but a read past the key's freshness names no app."""
        self.w.front_pid = lambda: ITERM
        self.fields[40] = DOCK
        self.fields["ts"] = _uptime_ns() - 300_000_000      # the key happened 0.3 s before its callback
        self.key()
        self.assertTrue(self.counted_unknown())
        self.assertNotIn("com.googlecode.iterm2", self.w.last_by_bundle)
        self.assertGreater(self.ex._last_input_at(), 0.0)   # noqa: SLF001 -- the task is staled

    def test_a_late_key_inside_a_step_counts_for_the_steps_app_whoever_is_in_front_later(self):
        """The step is the fallback: a fresh read names the app (the terminal); a late callback, inside the step,
        counts for the step's app (the front may have gone back since the flash)."""
        step = self.w.step_began(NE)                       # the executor: a step in NetEase Music begins (a flash)
        self.front["pid"] = ITERM
        self.fields[40] = DOCK
        self.key()                                         # fresh, during the step: the read names the terminal ...
        self.assertIn("com.googlecode.iterm2", self.w.last_by_bundle)
        self.assertGreater(self.ex._last_input_at(), 0.0)  # noqa: SLF001 -- ... but a step is under way: as on main
        self.w.step_ended(step)
        self.ex._action_end_at = self.w.mark()             # where main moved its mark at the action's end ...
        self.assertEqual(self.ex._last_input_at(), 0.0)    # noqa: SLF001 -- ... wiped there, as on main
        self.w.last_by_bundle.clear()
        self.fields["ts"] = _uptime_ns() - 200_000_000      # typed 0.2 s ago, inside the step; its callback runs late
        self.key()
        self.assertIn(NE, self.w.last_by_bundle)
        self.assertGreater(self.ex._last_input_at(), 0.0)   # noqa: SLF001
        # long after the step (and its margin), the front seen: the front decides again
        for i in (1, 2, 4, 5):
            step[i] -= 10
        self.w.last_by_bundle.clear()
        self.w.last_doubt_at = 0.0
        self.fields.pop("ts")
        self.observe_front()
        self.key()
        self.assertEqual(self.ex._last_input_at(), 0.0)    # noqa: SLF001
        self.assertIn("com.googlecode.iterm2", self.w.last_by_bundle)

    def test_the_executor_marks_each_step_for_the_watch(self):
        from test_executor import TE, W12, _executor, _win
        ex, stub = _executor()
        ex._watch, ex._watch_tried = self.w, True
        ex._windows = lambda every=False: [_win("12")]     # noqa: SLF001 -- no desktop
        ex._front = lambda: "文本编辑"                       # noqa: SLF001
        seen = []
        real = stub.execute
        stub.execute = lambda a: seen.append(self.w.step_at(time.time())) or real(a)
        ex.observe(W12)
        ex.perform({"kind": "click", "window": W12, "target": "B2"})
        self.assertEqual(seen, [TE.lower()])
        self.assertIsNotNone(self.w._steps[-1][2])        # and its end


class _EpochCase(_KeysCase):
    """A task operating TextEdit (pid MUSIC here) through the real executor, over the watch."""

    def _task(self):
        from test_executor import W12, _executor, _win
        ex, stub = _executor()
        del ex._last_input_at                              # the real one, over this watch
        self.w._pid_bundle[MUSIC] = "com.apple.TextEdit"
        ex._watch, ex._watch_tried = self.w, True
        ex._windows = lambda every=False: [_win("12")]     # noqa: SLF001 -- no desktop
        ex._front = lambda: "文本编辑"                       # noqa: SLF001
        ex.observe(W12)
        ex.snapshot()
        return ex, stub, W12


class LateKeysAndTheEpoch(_EpochCase):
    """The epoch's mark is compared with when input was seen. A key is recorded at its callback's time -- its own time
    only decides where it went -- and the mark an action leaves is not moved past the user's input when the driver's
    own events are told by their source (hands#62 re-review, at 809aa34). Each test here fails at 809aa34."""

    def test_a_key_typed_near_an_actions_end_whose_callback_runs_after_it_stales_the_next_step(self):
        ex, _, W12 = self._task()
        ex.perform({"kind": "click", "window": W12, "target": "B2"})
        e = ex.epoch
        self.fields[40] = MUSIC                            # typed into the task's own app ...
        self.fields["ts"] = _uptime_ns() - 200_000_000      # ... 0.2 s ago, before the action ended; seen only now
        self.key()
        s = ex.snapshot()
        self.assertEqual(s["epoch"], e + 1)
        self.assertEqual(s["epoch_reason"]["by"], "input")

    def test_a_key_moved_onto_the_steps_app_is_not_wiped_by_the_steps_end(self):
        ex, stub, W12 = self._task()
        real = stub.execute

        def during(a):
            self.fields[40] = DOCK                         # a Command key in the flash, its callback late ...
            self.fields["ts"] = _uptime_ns() - 200_000_000
            self.front["pid"] = ITERM                      # ... after the front went back: the step's app
            self.key()
            return real(a)
        stub.execute = during
        ex.perform({"kind": "click", "window": W12, "target": "B2"})
        e = ex.epoch
        s = ex.snapshot()
        self.assertEqual(s["epoch"], e + 1)
        self.assertEqual(s["epoch_reason"]["to"], "com.apple.textedit")

    def test_the_users_key_during_an_action_stales_the_next_step_and_the_drivers_own_does_not(self):
        ex, stub, W12 = self._task()
        real = stub.execute

        def driver_types(a):
            self.fields.update({40: MUSIC, 41: os.getpid()})   # the driver's own key into the task's app
            self.key()
            return real(a)
        stub.execute = driver_types
        ex.perform({"kind": "click", "window": W12, "target": "B2"})
        e = ex.epoch
        self.assertEqual(ex.snapshot()["epoch"], e)       # ours: no change

        def user_types(a):
            self.fields.update({40: MUSIC, 41: 0})         # the user edits the task's window mid-action
            self.key()
            return real(a)
        stub.execute = user_types
        ex.perform({"kind": "click", "window": W12, "target": "B2"})
        e = ex.epoch
        s = ex.snapshot()
        self.assertEqual(s["epoch"], e + 1)
        self.assertEqual((s["epoch_reason"]["by"], s["epoch_reason"]["to"]), ("input", "com.apple.textedit"))

    def test_without_keys_watched_by_source_the_actions_mark_still_moves(self):
        """Without Input Monitoring, key presses count by the HID idle time, which cannot tell the driver's own from
        the user's: the mark an action leaves moves past them, as before."""
        ex, _, W12 = self._task()
        self.w.keys = False
        with mock.patch("deskmind_hands.executor.Quartz") as q:
            q.CGEventSourceSecondsSinceLastEventType.return_value = 0.0   # the driver's own keys, just now
            ex.perform({"kind": "click", "window": W12, "target": "B2"})
            e = ex.epoch
            q.CGEventSourceSecondsSinceLastEventType.return_value = 0.5
            self.assertEqual(ex.snapshot()["epoch"], e)


class WhatTheLookupLearns(_EpochCase):
    """What is learned about a key while the app with the keyboard is looked up can only make it count, never
    un-count it (hands#62 external review, at 7ed10d5). Each test here fails at 7ed10d5."""

    def test_a_key_is_recorded_after_its_lookup_not_before_an_epoch_that_moved_meanwhile(self):
        ex, _, _ = self._task()

        def slow_front():
            ex._advance()                                  # an action moves the epoch's mark while the read is out
            time.sleep(0.01)
            return MUSIC                                   # the task's own app had the keyboard
        self.w.front_pid = slow_front
        self.fields[40] = DOCK
        self.key()
        e = ex.epoch
        s = ex.snapshot()
        self.assertEqual(s["epoch"], e + 1)                # the key is behind no mark: it stales the next step
        self.assertEqual((s["epoch_reason"]["by"], s["epoch_reason"]["to"]), ("input", "com.apple.textedit"))

    def test_an_answer_that_comes_after_the_keys_freshness_is_not_taken(self):
        from deskmind_hands import inputwatch as iw
        ex, _, _ = self._task()
        e = ex.epoch

        def slow_front():
            time.sleep(iw.KEY_FRESH_S + 0.02)              # within the read's deadline, past the key's freshness
            return ITERM                                   # the terminal took the keyboard after the key landed
        self.w.front_pid = slow_front
        self.fields[40] = DOCK
        self.key()
        self.assertNotIn("com.googlecode.iterm2", self.w.last_by_bundle)
        self.assertTrue(self.counted_unknown())            # kept its target: unknown, and it counts
        self.assertEqual(ex.snapshot()["epoch"], e + 1)

class LookupDelaysProperty(_EpochCase):
    """A property over every way the lookups for a key can be slow (hands#62 external review, at d6a29ed): each
    lookup -- whether a pid is DeskMind's (own), whether it is an ordinary app (regular), the app with the keyboard
    (front), the step under way (step) -- alone and in every combination, delayed past the key's freshness window,
    with an action moving the epoch's mark from another thread in the middle of each delay. Whatever the delays:
    (1) the key stales the next snapshot (with no front observed nothing proves it went elsewhere; at d6a29ed this
    said "or is provably DeskMind's own, known from a fresh read", which the review at e8fa010 took away);
    (2) no key is put on an app other than the task's once its freshness window has passed.
    Fails at d6a29ed for the reviewer's two cases among others: a slow first lookup of the front's pid with the task's
    app in front (recorded behind the mark), and a slow first lookup of the Dock target with iTerm2 in front later
    (put on iTerm2 after the key's freshness). With flashes (hands#62 review at cbef166): a key that landed in the
    flashed app in the flash's last moment, the read answering after the front went back (iTerm2), must stale too --
    whatever is delayed, the flash lookup (flash) included. Those cases fail at cbef166."""

    LOOKUPS = ("own", "regular", "front", "step", "flash")

    def _key_with(self, delayed: frozenset, front_app: int, in_step: bool, in_flash: bool = False) -> tuple:
        import itertools  # noqa: F401
        from deskmind_hands import inputwatch as iw
        ex, _, _ = self._task()
        w = self.w
        delay = iw.KEY_FRESH_S + 0.01                      # past the freshness window, within the read's deadline
        bundles, regulars = dict(w._pid_bundle), dict(w._pid_regular)
        w._pid_bundle, w._pid_regular = {}, {}             # every pid is asked of the "system" the first time
        advanced = []

        def slow(name, fn):
            def lookup(*a):
                if name in delayed:
                    time.sleep(delay / 2)
                    t = threading.Thread(target=lambda: (ex._advance(), advanced.append(name)))
                    t.start()
                    t.join()                               # an action moved the mark while this lookup was out
                    time.sleep(delay / 2)
                return fn(*a)
            return lookup
        w._bundle_of = slow("own", lambda pid: bundles.get(pid, ""))
        w._regular_of = slow("regular", lambda pid: regulars.get(pid, False))
        w.front_pid = slow("front", lambda: front_app)
        w.step_at = slow("step", w.step_at)
        w.flash_at = slow("flash", getattr(w, "flash_at", lambda at: []))
        if in_step:
            w.step_began("com.apple.TextEdit")
        self.fields[40] = DOCK                             # a key whose target is no ordinary app
        self.fields.pop("ts", None)                        # fresh when its callback begins
        iw.__dict__.get("FLASHES", []).clear()             # no flash from an earlier case
        if in_flash:
            # the driver had TextEdit in front until 10 ms ago; the key landed in it 30 ms ago (fresh), and the
            # front read now answers the user's app (front_app), the front given back
            taken = getattr(iw, "front_taken", None)
            if taken is not None:
                self.addCleanup(iw.FLASHES.clear)
                token = taken("com.apple.TextEdit", time.time() - 0.3)
                token[2] = time.time() - 0.01
            self.fields["ts"] = _uptime_ns() - 30_000_000
        self.key()
        e = ex.epoch
        stales = ex.snapshot()["epoch"] == e + 1
        return stales, advanced, w

    def test_whatever_the_lookups_cost_a_key_stales_or_is_provably_deskminds_and_never_moves_away_late(self):
        import itertools
        cases = []
        for n in range(len(self.LOOKUPS) + 1):
            for delayed in itertools.combinations(self.LOOKUPS, n):
                for front_app, in_step, in_flash in ((MUSIC, False, False), (DESKMIND, False, False),
                                                     (ITERM, False, False), (ITERM, True, False), (ITERM, False, True)):
                    cases.append((frozenset(delayed), front_app, in_step, in_flash))
        for delayed, front_app, in_step, in_flash in cases:
            with self.subTest(delayed=sorted(delayed), front={MUSIC: "task", DESKMIND: "deskmind", ITERM: "iterm2"}[front_app],
                              in_step=in_step, in_flash=in_flash):
                self.setUp()
                stales, advanced, w = self._key_with(delayed, front_app, in_step, in_flash)
                late = bool(advanced)                      # a delay was met: the key is past its freshness window
                own = w.last_own_at > 0 and w.last_external_at == 0
                on_other = "com.googlecode.iterm2" in w.last_by_bundle
                if late or in_flash:
                    self.assertFalse(own or on_other, "attributed away from the task's app")
                # no front observed here: a fresh read alone (DeskMind's, the terminal's) is no proof that the key
                # went elsewhere since the review at e8fa010 -- it counts, as on main (ObservedFront: with the proof)
                self.assertTrue(stales, "the key did not stale the next snapshot")

class WaitAndBackground(_EpochCase):
    """A step spans the driver's wait for the user to pause (up to HANDS_FLASH_WAIT_S; 1800 s in the app), and the
    driver's background typing and shortcuts, which do not wait at all: the keys the user types in their terminal then
    are theirs, read off a fresh front, not the step's (hands#62 re-reviews: the rc.13 loop again). The background key
    window leaves the user's app both the front and the keyboard (skylight, live test 10-21), so nothing rewrites a key
    while it is held. The first test fails at d6a29ed, the second at cbef166."""

    def test_keys_typed_in_the_terminal_while_the_driver_waits_do_not_stale_the_task(self):
        ex, stub, W12 = self._task()
        real = stub.execute

        def waits_for_the_user(a):
            self.front["pid"] = ITERM                      # the user keeps working in the terminal
            self.fields.pop("ts", None)                    # callbacks fresh
            self.fields[40] = 0
            self.key()                                     # a plain key with no target
            self.fields[40] = DOCK
            self.key()                                     # a Command chord: the Dock its target
            return real(a)
        stub.execute = waits_for_the_user
        ex.perform({"kind": "click", "window": W12, "target": "B2"})
        e = ex.epoch
        self.assertEqual(ex.snapshot()["epoch"], e)        # not staled
        self.assertIn("com.googlecode.iterm2", self.w.last_by_bundle)

    def test_typing_in_the_terminal_while_the_driver_types_in_the_background_does_not_stale_the_task(self):
        from deskmind_hands.drivers import skylight
        ex, stub, W12 = self._task()
        real = stub.execute
        sl = mock.patch.multiple(skylight, _load=mock.Mock(return_value={"post": object()}),
                                 user_on=mock.Mock(return_value=False), _stopped=mock.Mock(return_value=False),
                                 _psn_for=mock.Mock(return_value=object()), focused_window=mock.Mock(return_value=7),
                                 _send_records=mock.Mock(return_value=[0]), _await_focus=mock.Mock(return_value=True),
                                 front_pid=mock.Mock(return_value=None))

        def types_in_the_background(a):
            with sl:
                with skylight.enter_window_key(MUSIC, 8):  # the task's window held key, the app not activated
                    self.front["pid"] = ITERM              # the user's app keeps the front and the keyboard
                    self.fields.pop("ts", None)
                    for target in (ITERM, 0, DOCK):        # typing, a key with no target, a Command chord
                        self.fields[40] = target
                        self.key()
            return real(a)
        stub.execute = types_in_the_background
        ex.perform({"kind": "type", "window": W12, "target": "T1", "text": "x"})
        e = ex.epoch
        self.assertEqual(ex.snapshot()["epoch"], e)        # not staled
        self.assertIn("com.googlecode.iterm2", self.w.last_by_bundle)


class FlashesTold(_EpochCase):
    """The driver tells the watch each flash -- from when it took the front to when it gave it back -- at the points
    where it brings a task's app forward and gives the user's app the front back, the flash told before the give-back
    so a key whose callback runs after it finds it (hands#62 review at cbef166). Each test here fails at cbef166."""

    def setUp(self):
        super().setUp()
        from deskmind_hands import inputwatch as iw
        self.iw = iw
        self.addCleanup(lambda: iw.__dict__.get("FLASHES", []).clear())

    def flashes(self, at: float) -> list:
        """The apps a flash had in front at `at` (flash_at: a list since c0c5a4f's re-review, the newest app before)."""
        r = self.iw.flash_at(at)
        return ([r] if r else []) if isinstance(r, str) else list(r)

    def test_a_key_in_the_flashs_last_moment_counts_though_the_read_answers_the_users_app(self):
        ex, _, _ = self._task()
        e = ex.epoch
        token = self.iw.front_taken("com.apple.TextEdit", time.time() - 0.5)
        self.front["pid"] = ITERM                          # given back: the read now names the terminal
        self.fields[40] = DOCK
        self.fields["ts"] = _uptime_ns() - 5_000_000       # a Command chord 5 ms before the give-back ended
        self.iw.front_given_back(token)
        self.key()
        s = ex.snapshot()
        self.assertEqual(s["epoch"], e + 1)
        self.assertEqual(s["epoch_reason"]["to"], "com.apple.textedit")

    def test_a_flash_told_while_the_front_read_is_out_is_found(self):
        """The callback began before the driver told the flash; the read went out; the driver told the flash and gave
        the front back; the read answered the terminal, still fresh. The flash check is the last lookup, so the key
        counts for the flashed app (hands#62 review at 50f176a)."""
        ex, _, _ = self._task()
        e = ex.epoch

        def read_meanwhile():
            token = self.iw.front_taken("com.apple.TextEdit", time.time() - 0.3)   # the driver, during the read
            self.iw.front_given_back(token)
            return ITERM                                   # the front given back: the terminal
        self.w.front_pid = read_meanwhile
        self.fields[40] = DOCK
        self.fields.pop("ts", None)                        # fresh
        self.key()
        s = ex.snapshot()
        self.assertEqual(s["epoch"], e + 1)
        self.assertEqual(s["epoch_reason"]["to"], "com.apple.textedit")

    def test_a_key_put_on_a_flash_counts_for_the_flashed_app_the_steps_app_and_the_read_app(self):
        """Where there is doubt the key counts everywhere it may have gone: a flash told while the read was out (the
        read named the music app, fresh), a step of TextEdit's open -- the key is recorded for all three."""
        self.w._pid_bundle[MUSIC] = NE
        step = self.w.step_began("com.apple.TextEdit")

        def read_meanwhile():
            self.iw.front_given_back(self.iw.front_taken("com.apple.finder", time.time() - 0.3))
            return MUSIC
        self.w.front_pid = read_meanwhile
        self.fields[40] = DOCK
        self.fields.pop("ts", None)
        self.key()
        self.w.step_ended(step)
        self.assertEqual({"com.apple.finder", "com.apple.textedit", NE.lower()} - set(self.w.last_by_bundle), set())
        self.assertEqual(self.w.last_unknown_at, 0.0)

    def test_where_the_input_went_is_the_same_in_every_run(self):
        """A key counted for several apps at once has one time: the epoch's reason names the same app each run --
        the first in sorted order, a task app ahead of "unknown" (hands#62 re-review at f0520a8)."""
        self.w.last_unknown_at = 5.0
        self.w.last_by_bundle = {"com.b": 5.0, "com.a": 5.0}
        for order in (["com.b", "com.a"], ["com.a", "com.b"]):
            self.assertEqual(self.w.last_input_where(order), (5.0, "com.a"))
        self.w.last_by_bundle = {"com.a": 4.0}
        self.assertEqual(self.w.last_input_where({"com.a"}), (5.0, "unknown"))   # a later unknown still wins

    def test_the_drivers_flash_is_told_before_the_front_goes_back_and_ends_after(self):
        """Through the driver's real _flash (the test_receipt_driver fixture): the flash is there while the front
        goes back, from when it was taken, and ends once it has."""
        import os as _os
        import types
        from test_receipt_driver import BrowserForeground
        q = types.ModuleType("Quartz")
        q.CGEventGetLocation = lambda e: (0, 0)
        q.CGEventCreate = lambda x: None
        q.CGWarpMouseCursorPosition = lambda p: None
        q.CGAssociateMouseAndMouseCursorPosition = lambda b: None
        d = BrowserForeground._driver(None, True)
        d._own_window_in_front = lambda bundle: True
        seen = []
        stub_back = d._give_front_back

        inside = []

        def give_back(prev, ours=None, late_s=0.0):
            inside.append(time.time())
            seen.append(self.flashes(inside[-1]))      # while the front goes back
            stub_back(prev, ours, late_s)
        d._give_front_back = give_back
        with mock.patch.dict(_os.environ, {"HANDS_FLASH_APPS": "com.google.Chrome"}), \
             mock.patch.dict(sys.modules, {"Quartz": q}), \
             mock.patch("deskmind_hands.drivers.peekaboo._ascii_input_source",
                        return_value=types.SimpleNamespace(restore=lambda: None)), \
             mock.patch("deskmind_hands.drivers.peekaboo.time.sleep"):
            ok, _ = d._flash("com.google.Chrome", lambda Q: "typed")
        self.assertTrue(ok)
        self.assertEqual(len(seen), 1)
        self.assertIn("com.google.chrome", seen[0])
        self.assertIn("com.google.chrome", self.flashes(inside[0]))    # afterwards too: the interval
        self.assertEqual(self.flashes(time.time() + 1.0), [])          # ended

    def _outer(self, fronts: list[tuple[str, str]], idle: float = 60.0, app: str = NE, foreground: bool = False):
        """A driver for NetEase Music (an app no name list knows) whose step ran while the front went from the first
        of `fronts` to the second (name, bundle id), through the real _ours."""
        from deskmind_hands.actions import Action, ActionKind, Binding
        from deskmind_hands.drivers.base import ExecResult
        from deskmind_hands.drivers.peekaboo import PeekabooDriver
        d = PeekabooDriver.__new__(PeekabooDriver)
        d.app, d._mcp, d._foreground_actions = app, object(), 0
        apps = iter(fronts)
        d._front_app = lambda: next(apps)
        d._hid_idle = lambda: idle
        d._task_windows = lambda: []
        d.activated = []
        d._osa = lambda script: d.activated.append(script) or (True, "")
        t0 = time.time()
        d._execute_inner = lambda a: (time.sleep(0.05), ExecResult(True, "done"))[1]
        d._execute_outer(Action(kind=ActionKind.CLICK, foreground=foreground, binding=Binding(element_id="B2")))
        return d, t0

    def test_a_step_whose_app_came_forward_by_itself_is_told_from_the_steps_start(self):
        """By bundle id, with the real _ours: the music app's name (网易云音乐) never matched its bundle id, so it went
        down the stranger's branch and no flash was told (hands#62 review at 50f176a)."""
        for foreground, idle in ((False, 60.0), (True, 60.0), (False, 0.01)):
            # told whatever main's give-back does with the front (given back, or left: a foreground step, a user
            # who touched the Mac): counting and giving back are separate decisions
            self.iw.FLASHES.clear()
            d, t0 = self._outer([("iTerm2", "com.googlecode.iterm2"), ("网易云音乐", NE)], idle=idle, foreground=foreground)
            self.assertIn(NE.lower(), self.flashes(t0 + 0.02))

    def test_a_name_list_app_in_front_at_a_steps_end_is_told_with_the_steps_own_app(self):
        """Finder (a name-list app) in front at the end of a Notes step: told for Finder and for Notes alike, from the
        step's start -- where there is doubt a key counts for both (hands#62 re-review at c0c5a4f)."""
        d, t0 = self._outer([("iTerm2", "com.googlecode.iterm2"), ("访达", "com.apple.finder")], app="com.apple.Notes")
        self.assertEqual(set(self.flashes(t0 + 0.02)), {"com.apple.finder", "com.apple.notes"})

    def test_a_flash_told_over_the_steps_inner_flash_does_not_hide_it(self):
        """The inner flash (the step's own app, TextEdit here) told first, the Finder front found at the step's end
        told after from the step's start: a Command key at the inner time stales the TextEdit task."""
        ex, _, _ = self._task()
        e = ex.epoch
        t = time.time()
        inner = self.iw.front_taken("com.apple.TextEdit", t - 0.5)
        inner[2] = t - 0.1
        self.iw.front_given_back(self.iw.front_taken("com.apple.finder", t - 1.0))
        self.front["pid"] = ITERM
        self.fields[40] = DOCK
        self.fields["ts"] = _uptime_ns() - int((time.time() - (t - 0.3)) * 1e9)   # the key at the inner flash's time
        self.key()
        s = ex.snapshot()
        self.assertEqual(s["epoch"], e + 1)
        self.assertEqual(s["epoch_reason"]["to"], "com.apple.textedit")

    def test_another_app_that_came_forward_during_a_step_is_told_too(self):
        """The stranger's branch hands the front back too; the app it took it from may be one the task operates
        through another driver, so it is told as well."""
        d, t0 = self._outer([("iTerm2", "com.googlecode.iterm2"), ("音乐", "com.apple.Music")])
        self.assertIn("com.apple.music", self.flashes(t0 + 0.02))

    def test_the_chat_flash_is_told(self):
        import os as _os
        import types
        from deskmind_hands.drivers import peekaboo as pk
        chat = "com.example.chat"                           # the chat app is configured per install (apps.py)
        self.addCleanup(setattr, pk, "CHAT_APP", pk.CHAT_APP)
        pk.CHAT_APP = chat
        d = pk.PeekabooDriver.__new__(pk.PeekabooDriver)
        d.app, d._foreground_actions, d._chat_sends = pk.CHAT_APP, 0, 0
        d._labels_by_id = {"B1": "打开"}
        d._rects = {"B1": types.SimpleNamespace(x=10, y=10, w=20, h=10)}
        d._origin = types.SimpleNamespace(x=0, y=0)
        d._moved_since_seen = lambda: None
        d._add_wait = lambda s: None
        fronts = iter(["com.googlecode.iterm2", pk.CHAT_APP])
        d._front_bundle = lambda: next(fronts, pk.CHAT_APP)
        seen = []
        d._give_front_back = lambda prev, ours=None, late_s=0.0: seen.append(self.flashes(time.time()))
        q = types.ModuleType("Quartz")
        for k in ("kCGEventMouseMoved", "kCGEventLeftMouseDown", "kCGEventLeftMouseUp", "kCGMouseButtonLeft", "kCGHIDEventTap"):
            setattr(q, k, 0)
        q.CGEventGetLocation = lambda e: (0, 0)
        q.CGEventCreate = lambda x: None
        q.CGEventPost = lambda tap, e: None
        q.CGEventCreateMouseEvent = lambda *a: None
        q.CGWarpMouseCursorPosition = lambda p: None
        q.CGAssociateMouseAndMouseCursorPosition = lambda b: None
        with mock.patch.dict(_os.environ, {"HANDS_CHAT_FLASH": "1"}), \
             mock.patch.dict(sys.modules, {"Quartz": q}), \
             mock.patch.object(pk, "hid_idle_seconds", return_value=60.0), \
             mock.patch.object(pk.subprocess, "run"), \
             mock.patch.object(pk.time, "sleep"):
            res = d._chat_flash_click("B1")
        self.assertTrue(res.ok, res.detail)
        self.assertEqual(len(seen), 1)
        self.assertIn(chat, seen[0])                        # told before the front went back
        self.assertIsNotNone(self.iw.FLASHES[-1][2])        # and ended after

    def _stage(self, app: str, stage_apps: list[str], fronts: list[tuple[str, str]], mcp: bool = True,
               during=None):
        """Staging of `app`'s driver while the front went from the first of `fronts` to the second; `during` runs
        while staging is under way (as it closes the windows an earlier run left)."""
        import os as _os
        import tempfile
        from deskmind_hands.drivers import peekaboo as pk
        scratch = Path(tempfile.mkdtemp())
        (scratch / "ws").mkdir()
        with mock.patch.dict(_os.environ, {"HANDS_PERMISSIONS_VERIFIED": "1"}):
            d = pk.PeekabooDriver(app=app, binary=sys.executable, scratch=scratch)
        d._mcp = object() if mcp else None
        d._stage_apps = list(stage_apps)
        it = iter(fronts)
        d._front_app = lambda: next(it)
        d.activated = []
        d._osa = lambda script: d.activated.append(script) or (True, "")
        d._run = lambda key, _extra=None, **fmt: (False, {}, "not here")
        d.clipboard = lambda: ""
        d._set_clipboard = lambda value: None
        if during is not None:
            d.reset_apps = ["com.example.left"]
            d._close_all_windows = lambda bundle: during() or 0
        t = time.time()
        with mock.patch.object(pk, "gui_session", return_value="active"):
            try:
                d._start(scratch / "ws")
            except Exception:  # noqa: BLE001 -- what staging does after the front check is not this test's
                pass
        return d, t

    def test_a_front_taken_while_staging_is_told(self):
        """One of the task's apps (by bundle id: the music app, which no name list knows) came forward while staging:
        told to the watch from staging's start -- whatever main's give-back does with it (by name: left here)."""
        d, t = self._stage(NE, [], [("iTerm2", "com.googlecode.iterm2"), ("网易云音乐", NE)])
        self.assertIn(NE.lower(), [f[0] for f in self.iw.FLASHES])
        self.assertTrue(all(f[2] is not None for f in self.iw.FLASHES))      # ended
        self.assertEqual(d.activated, [])                   # main's give-back: by name, not the music app's

    def test_a_staging_app_known_only_by_name_is_told(self):
        """app_bundle gives the name back for an app it does not know: the staging set then holds a name, which the
        front's name is compared with as well as its bundle id."""
        d, t = self._stage("com.example.other", ["网易云音乐"], [("iTerm2", "com.googlecode.iterm2"), ("网易云音乐", NE)])
        self.assertIn(NE.lower(), [f[0] for f in self.iw.FLASHES])
        self.assertIn('tell application "iTerm2" to activate', d.activated)   # main's: the name is in the set

    def test_a_cold_launch_that_took_the_front_is_told(self):
        from deskmind_hands import executor as exe
        from test_executor import _executor
        ex, _ = _executor()
        ex._watch, ex._watch_tried = self.w, True
        ex._known_windows = lambda: set()                  # noqa: SLF001
        fronts = iter(["com.googlecode.iterm2", NE] + ["com.googlecode.iterm2"] * 5)
        ex._front_bundle_id = lambda: next(fronts)         # noqa: SLF001 -- the app took the front on launch
        ex._make_frontmost = lambda b: True                # noqa: SLF001
        inside = []
        ex._launch_windows = lambda *a: inside.append(time.time()) or []   # noqa: SLF001 -- a moment in the launch
        ex._settled = mock.Mock(side_effect=RuntimeError("stop here"))   # noqa: SLF001
        with mock.patch.object(exe, "resolve_app", return_value=NE), \
             mock.patch.object(exe, "running_apps", return_value=[]), \
             mock.patch.object(exe, "installed_apps", return_value=[]), \
             mock.patch.object(exe.subprocess, "run"):
            with self.assertRaises(RuntimeError):
                ex._open_window(NE, None)                  # noqa: SLF001
        self.assertIn(NE.lower(), self.flashes(inside[0]))   # not t + 1 ms: a loaded machine starts the launch later


class FlashesToldBeforeTheFront(_EpochCase):
    """Every place that takes the front, or may (a flash, the chat app's click, staging, a cold launch), tells the
    input watch before it does, and ends the flash after the front has gone back, on every way out. Told only as the
    front went back, a key whose callback ran while the app was in front -- perform() still running -- found no flash,
    and with the front meanwhile in another app the read named that app: the key was put on it, never moved, and the
    task did not go stale (hands#62 review at af55263: epoch 2 -> 2 through the real _flash). The tests named
    `..._while_..._counts_for_...` fail at af55263."""

    def setUp(self):
        super().setUp()
        from deskmind_hands import inputwatch as iw
        self.iw = iw
        iw.FLASHES.clear()
        self.addCleanup(iw.FLASHES.clear)

    flashes = FlashesTold.flashes
    _stage = FlashesTold._stage

    def _quartz_for(self, **extra):
        """The callback's Quartz (the event's fields) with what the driver's path reads besides."""
        q = _quartz(self.fields)
        q.CGEventGetLocation = lambda e: (0, 0)
        q.CGEventCreate = lambda x: None
        q.CGWarpMouseCursorPosition = lambda p: None
        q.CGAssociateMouseAndMouseCursorPosition = lambda b: None
        for k, v in extra.items():
            setattr(q, k, v)
        return q

    def _users_key_elsewhere(self):
        """A Command key the user typed into the app in front (the Dock its target), its callback run now, while the
        front read already names the terminal: the user has moved there meanwhile."""
        self.front["pid"] = ITERM
        self.fields[40] = DOCK
        self.fields.pop("ts", None)                        # fresh: the read is taken
        self.key()

    def _flash_driver(self):
        from test_receipt_driver import BrowserForeground
        d = BrowserForeground._driver(None, True)          # the user's terminal in front; nothing waits
        d._own_window_in_front = lambda bundle: True
        return d

    def _run_flash(self, d, bundle: str, perform):
        import os as _os
        from deskmind_hands.drivers import peekaboo as pk
        with mock.patch.dict(_os.environ, {"HANDS_FLASH_APPS": bundle}), \
             mock.patch.dict(sys.modules, {"Quartz": self._quartz_for()}), \
             mock.patch.object(pk, "_ascii_input_source", return_value=types.SimpleNamespace(restore=lambda: None)), \
             mock.patch.object(pk.time, "sleep"):
            return d._flash(bundle, perform)

    def test_a_key_while_the_flash_performs_counts_for_the_flashed_app_though_the_front_moved(self):
        """The review's case, through the real _flash: the key lands in TextEdit while perform() runs, its callback
        runs then, and the read names the terminal -- the task operating TextEdit goes stale, for TextEdit."""
        ex, _, _ = self._task()
        e = ex.epoch

        def perform(Q):
            self._users_key_elsewhere()
            return "typed"
        ok, why = self._run_flash(self._flash_driver(), "com.apple.TextEdit", perform)
        self.assertTrue(ok, why)
        s = ex.snapshot()
        self.assertEqual(s["epoch"], e + 1)
        self.assertEqual(s["epoch_reason"]["to"], "com.apple.textedit")

    def test_the_flash_is_told_before_the_app_is_brought_forward_and_ended_on_every_way_out(self):
        """Told before _bring_forward; ended after the give-back whether the step was sent, the app was not raised,
        did not arrive, or perform() raised."""
        told = []

        def raises(Q):
            raise RuntimeError("perform failed")
        for case in ("sent", "not raised", "not arrived", "raised"):
            self.iw.FLASHES.clear()
            d = self._flash_driver()
            real_bring = d._bring_forward

            def bring(bundle, browser, real_bring=real_bring, case=case):
                told.append((case, self.flashes(time.time())))
                if case == "not raised":
                    return False
                if case == "not arrived":
                    return True                                # asked forward; the front stays the terminal's
                return real_bring(bundle, browser)
            d._bring_forward = bring
            if case == "raised":
                with self.assertRaises(RuntimeError):
                    self._run_flash(d, NE, raises)
            else:
                ok, why = self._run_flash(d, NE, lambda Q: "typed")
                self.assertEqual(ok, case == "sent", why)
            self.assertIn(NE.lower(), told[-1][1], case)       # told before the app was asked forward
            self.assertEqual([f[0] for f in self.iw.FLASHES], [NE.lower()], case)
            self.assertIsNotNone(self.iw.FLASHES[-1][2], case)  # ended

    def test_a_retried_turn_ends_its_flash_before_the_next_turn_tells_its_own(self):
        """The user touched the Mac while the app came forward: nothing is sent, the front goes back, the turn is
        retried -- each turn's flash ended before the next one is told."""
        d = self._flash_driver()
        d._takeover = lambda: False
        idle = iter([100.0, -1.0, 100.0, 100.0])           # still; touched since the check; still again
        d._hid_idle = lambda: next(idle, 100.0)
        d._announce_wait = lambda waited, bundle, what: True
        ok, why = self._run_flash(d, NE, lambda Q: "typed")
        self.assertTrue(ok, why)
        turns = [f for f in self.iw.FLASHES if f[0] == NE.lower()]
        self.assertEqual(len(turns), 2)
        self.assertIsNotNone(turns[0][2])
        self.assertLessEqual(turns[0][2], turns[1][1])     # the first ended before the second was told
        self.assertIsNotNone(turns[1][2])

    def _chat_driver(self):
        import types as _types
        from deskmind_hands.drivers import peekaboo as pk
        chat = "com.example.chat"
        self.addCleanup(setattr, pk, "CHAT_APP", pk.CHAT_APP)
        pk.CHAT_APP = chat
        d = pk.PeekabooDriver.__new__(pk.PeekabooDriver)
        d.app, d._foreground_actions, d._chat_sends = chat, 0, 0
        d._labels_by_id = {"B1": "打开"}
        d._rects = {"B1": _types.SimpleNamespace(x=10, y=10, w=20, h=10)}
        d._origin = _types.SimpleNamespace(x=0, y=0)
        d._moved_since_seen = lambda: None
        d._add_wait = lambda s: None
        fronts = iter(["com.googlecode.iterm2", chat])
        d._front_bundle = lambda: next(fronts, chat)
        d._give_front_back = lambda prev, ours=None, late_s=0.0: None
        return d, chat

    def _run_chat(self, d, post):
        import os as _os
        from deskmind_hands.drivers import peekaboo as pk
        q = self._quartz_for(CGEventPost=post, CGEventCreateMouseEvent=lambda *a: None)
        for k in ("kCGEventMouseMoved", "kCGMouseButtonLeft", "kCGHIDEventTap"):
            setattr(q, k, 0)
        with mock.patch.dict(_os.environ, {"HANDS_CHAT_FLASH": "1"}), \
             mock.patch.dict(sys.modules, {"Quartz": q}), \
             mock.patch.object(pk, "hid_idle_seconds", return_value=60.0), \
             mock.patch.object(pk.subprocess, "run"), \
             mock.patch.object(pk.time, "sleep"):
            return d._chat_flash_click("B1")

    def test_a_key_while_the_chat_app_is_clicked_counts_for_it_though_the_front_moved(self):
        d, chat = self._chat_driver()
        keyed = []

        def post(tap, e):
            if not keyed:
                keyed.append(1)
                self._users_key_elsewhere()                # while the chat app is in front for the click
        res = self._run_chat(d, post)
        self.assertTrue(res.ok, res.detail)
        self.assertIn(chat, self.w.last_by_bundle)
        self.assertEqual(self.w.last_unknown_at, 0.0)
        self.assertIsNotNone(self.iw.FLASHES[-1][2])       # ended

    def test_the_chat_flash_is_ended_when_the_chat_app_never_came_forward(self):
        d, chat = self._chat_driver()
        d._front_bundle = lambda: "com.googlecode.iterm2"  # it never arrives
        res = self._run_chat(d, lambda tap, e: None)
        self.assertFalse(res.ok)
        self.assertEqual([f[0] for f in self.iw.FLASHES], [chat])
        self.assertIsNotNone(self.iw.FLASHES[-1][2])

    def test_a_key_while_staging_counts_for_the_tasks_app_though_the_front_moved(self):
        """Staging launched the music app, which took the front; the user's key landed in it while staging went on,
        then they moved to the terminal, so the front read after staging names the terminal and no front taken is
        found there."""
        d, t = self._stage(NE, [], [("iTerm2", "com.googlecode.iterm2"), ("iTerm2", "com.googlecode.iterm2")],
                           during=self._users_key_elsewhere)
        self.assertIn(NE.lower(), self.w.last_by_bundle)
        self.assertEqual(self.w.last_unknown_at, 0.0)
        self.assertTrue(self.iw.FLASHES and all(f[2] is not None for f in self.iw.FLASHES))   # ended
        self.assertEqual(d.activated, [])                   # main's give-back: nothing taken, nothing given back

    def test_the_staging_flash_is_ended_when_staging_raises(self):
        def raises():
            raise RuntimeError("staging failed")
        self._stage(NE, [], [("iTerm2", "com.googlecode.iterm2")], during=raises)
        self.assertIn(NE.lower(), [f[0] for f in self.iw.FLASHES])
        self.assertTrue(all(f[2] is not None for f in self.iw.FLASHES))

    def test_a_key_while_a_cold_launch_runs_counts_for_the_launched_app_though_the_front_moved(self):
        from deskmind_hands import executor as exe
        from test_executor import _executor
        ex, _ = _executor()
        ex._watch, ex._watch_tried = self.w, True
        ex._known_windows = lambda: set()                  # noqa: SLF001
        ex._front_bundle_id = lambda: "com.googlecode.iterm2"   # noqa: SLF001 -- in front before, and again after
        ex._make_frontmost = lambda b: True                # noqa: SLF001

        def windows(*a):
            self._users_key_elsewhere()                    # the app took the front at launch; the user's key in it
            return []
        ex._launch_windows = windows                       # noqa: SLF001
        ex._settled = mock.Mock(side_effect=RuntimeError("stop here"))   # noqa: SLF001
        with mock.patch.object(exe, "resolve_app", return_value=NE), \
             mock.patch.object(exe, "running_apps", return_value=[]), \
             mock.patch.object(exe, "installed_apps", return_value=[]), \
             mock.patch.object(exe.subprocess, "run"):
            with self.assertRaises(RuntimeError):
                ex._open_window(NE, None)                  # noqa: SLF001
        self.assertIn(NE.lower(), self.w.last_by_bundle)
        self.assertTrue(all(f[2] is not None for f in self.iw.FLASHES))   # ended


class StepsClose(_KeysCase):
    """A step mark that is never closed would put every later key with no app target on that step's app (hands#62
    re-review, at 809aa34). Each test here fails at 809aa34."""

    def test_a_launch_that_raises_still_closes_its_step(self):
        import subprocess
        from deskmind_hands import executor as exe
        from test_executor import _executor
        ex, _ = _executor()
        ex._watch, ex._watch_tried = self.w, True
        ex._known_windows = lambda: set()                  # noqa: SLF001
        ex._front_bundle_id = lambda: "com.googlecode.iterm2"   # noqa: SLF001
        with mock.patch.object(exe, "resolve_app", return_value=NE), \
             mock.patch.object(exe, "running_apps", return_value=[]), \
             mock.patch.object(exe, "installed_apps", return_value=[]), \
             mock.patch.object(exe.subprocess, "run", side_effect=subprocess.TimeoutExpired("open", 20)):
            with self.assertRaises(subprocess.TimeoutExpired):
                ex._open_window(NE, None)                  # noqa: SLF001
        self.assertTrue(self.w._steps)
        self.assertIsNotNone(self.w._steps[-1][2])        # closed
        time.sleep(0.01)
        self.assertEqual(self.w.step_at(time.time() + STEP_MARGIN), "")

    def test_a_step_never_closed_ends_at_its_deadline(self):
        from deskmind_hands import inputwatch as iw
        token = self.w.step_began(NE)
        for i in (1, 3, 4, 6):
            token[i] -= iw.STEP_MAX_S + 10                 # began long ago (and its deadline with it), on both clocks
        self.assertEqual(self.w.step_at(time.time()), "")
        self.assertFalse(self.w.in_flight(time.monotonic() - 1, time.monotonic()))   # its end never told: over
        self.observe_front()
        self.fields[40] = DOCK
        self.key()                                         # a Command key in the terminal now
        self.assertEqual(self.ex._last_input_at(), 0.0)    # noqa: SLF001 -- not put on that step's app
        self.assertIn("com.googlecode.iterm2", self.w.last_by_bundle)


STEP_MARGIN = 0.6

class FrontRead(unittest.TestCase):
    """Who has the keyboard is asked of the accessibility server without changing anything process-wide, within a
    deadline, and not again for a moment after it failed (hands#62 review)."""

    def test_the_watch_never_sets_the_process_wide_ax_messaging_timeout(self):
        """AXUIElementSetMessagingTimeout on the system-wide element is every AX call's in the process
        (AXUIElement.h): at 0.25 s the address-bar guard's reads of a stalled browser came back empty."""
        calls = []
        ax = types.ModuleType("ApplicationServices")
        sw, app = object(), object()
        ax.AXUIElementCreateSystemWide = lambda: calls.append("CreateSystemWide") or sw
        ax.AXUIElementSetMessagingTimeout = lambda el, t: calls.append(("SetMessagingTimeout", el, t)) or 0
        ax.AXUIElementCopyAttributeValue = lambda el, attr, _: calls.append(("Copy", attr)) or (0, app)
        ax.AXUIElementGetPid = lambda el, _: (0, ITERM)
        fields = {40: DOCK}
        q = _quartz(fields)
        q.kCGWindowListOptionOnScreenOnly, q.kCGNullWindowID = 1, 0
        q.CGWindowListCreate = lambda *a: calls.append("CGWindowListCreate") or ()
        for attr in ("_system_wide", "_window_server_seen"):
            if attr in InputWatch.__dict__:
                self.addCleanup(setattr, InputWatch, attr, InputWatch.__dict__[attr])
                delattr(InputWatch, attr)
        stable_pids(self, ITERM, DOCK)
        w = InputWatch()
        w._pid_bundle = {ITERM: "com.googlecode.iterm2", DOCK: "com.apple.dock"}
        w._pid_regular = {ITERM: True, DOCK: False}
        with mock.patch.dict(sys.modules, {"Quartz": q, "ApplicationServices": ax}):
            w._callback(None, 10, object(), None)
            w._callback(None, 10, object(), None)
        self.assertIn(("Copy", "AXFocusedApplication"), calls)         # the read was made
        self.assertIn("com.googlecode.iterm2", w.last_by_bundle)
        self.assertFalse([c for c in calls if c[0] == "SetMessagingTimeout"], calls)

    def test_a_read_that_hangs_or_fails_is_bounded_and_not_repeated_for_every_key(self):
        from deskmind_hands import inputwatch as iw
        w = InputWatch()
        release = threading.Event()
        self.addCleanup(release.set)
        hung = []
        w.front_pid = lambda: hung.append(1) or release.wait(5) and ITERM
        t0 = time.monotonic()
        self.assertEqual(w.front_now(), 0)                  # no answer within the deadline: not known
        self.assertLess(time.monotonic() - t0, iw.FRONT_DEADLINE_S + 0.2)
        w._front_backoff_until = 0.0
        self.assertEqual(w.front_now(), 0)                  # still out: not asked again
        self.assertEqual(len(hung), 1)
        release.set()
        time.sleep(0.05)
        failing = mock.Mock(return_value=0)
        w.front_pid = failing
        w._front_backoff_until = 0.0
        for _ in range(20):                                 # a burst of keys while the read fails
            self.assertEqual(w.front_now(), 0)
        self.assertEqual(failing.call_count, 1)             # once, then the backoff
        w._front_backoff_until = time.monotonic() - 1      # the backoff over
        w.front_pid = lambda: ITERM
        self.assertEqual(w.front_now(), ITERM)

    def test_the_event_clock_is_read_either_way_and_an_implausible_stamp_is_no_age(self):
        from deskmind_hands import inputwatch as iw
        self.assertAlmostEqual(iw.event_age(_uptime_ns() - 30_000_000), 0.03, delta=0.02)
        ticks = iw._mach_ticks()
        if ticks is not None:                               # macOS: a stamp in mach_absolute_time ticks
            now, scale = ticks
            self.assertAlmostEqual(iw.event_age(now - int(0.03e9 / scale)), 0.03, delta=0.02)
        self.assertIsNone(iw.event_age(0))
        self.assertIsNone(iw.event_age(_uptime_ns() + 10**9))   # ahead of now

if __name__ == "__main__":
    unittest.main()


class MainWatch(InputWatch):
    """Main's attribution (deskmind-ai/hands main at b9a8674, InputWatch.note verbatim): an input goes to its target
    when that is an ordinary app, to DeskMind when it is DeskMind, and otherwise to an unknown target, which counts."""

    def note(self, pid, at=None, key=False, age=None, t0=None, switcher=False):   # noqa: ARG002 -- main's had pid, at
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


CHAT_PID, NE_PID = 35, 36
CHAT = "com.example.chat"
TE = "com.apple.TextEdit"
APPS = {ITERM: ("iTerm2", "com.googlecode.iterm2"), MUSIC: ("文本编辑", TE), CHAT_PID: ("Chat", CHAT),
        NE_PID: ("网易云音乐", NE)}


class MainIsTheFloor(_EpochCase):
    """The same scenarios through the real executor -> driver path (HandsExecutor.perform -> PeekabooDriver.execute
    -> _execute_outer, the real _flash / chat click / staging / launch inside it; the system's calls mocked: the app
    in front is one value, read by the driver and by the watch's poller alike), once with main's attribution and once
    with the branch's. Main, for this table: main's InputWatch.note (MainWatch) and main's epoch mark, moved at every
    action's end (_input_by_source False) -- the executor and driver code is this branch's, which main's attribution
    ignores apart from those two. Wherever main stales, the branch stales, but for the one exception the hands#62 fix
    exists for: a key with no app target (a Command chord) typed in the user's own app (the terminal) between steps,
    when no app of the task's was in front around it (rc.13, 10-22; owner 10-20). Built after the hands#62 review at
    9def177; `review_*` fails there (the branch did not stale)."""

    #: (scenario, main stales, branch stales, the allowed exception)
    TABLE = [
        ("review_textedit_forward_by_itself_during_a_step", True, True, False),
        ("late_flash_registration_key_during_perform", False, True, False),
        ("chat_click_key_during_the_click", False, True, False),
        ("staging_key_while_the_task_app_had_the_front", True, True, False),
        ("cold_launch_key_while_the_app_had_the_front", True, True, False),
        ("terminal_typing_while_the_flash_waits_for_idle", False, False, False),
        ("terminal_typing_between_steps", True, False, True),
        ("command_key_in_the_task_app_between_steps", True, True, False),
        ("plain_key_into_the_task_app_between_steps", True, True, False),
        ("late_callback_of_a_key_typed_in_the_task_app", True, True, False),
        # the review at e8fa010
        ("brief_front_blip_during_a_step", True, True, False),
        ("routed_after_a_read_past_the_keys_time", True, True, False),
        ("wall_clock_stepped_back_key_in_the_task_app", True, True, False),
        ("hung_poller_key_in_the_task_app", True, True, False),
        ("dead_poller_key_in_the_task_app", True, True, False),
        ("key_without_a_timestamp_in_the_task_app", True, True, False),
        ("slow_front_reads_key_in_the_task_app", True, True, False),
        ("hung_poller_terminal_typing_between_steps", True, True, False),
        # Codex at f0f7186
        ("slow_read_answers_the_terminal_after_a_switch_and_back", True, True, False),
        # the delta review at 45e67fc: the app switcher acts on the app it picked, the terminal still in front
        ("switcher_then_q_on_the_task_app", True, True, False),
        ("plain_command_chord_in_the_terminal_between_steps", True, False, True),
    ]

    # -- the mocked system ---------------------------------------------------------------------------------------
    def _watch(self, main: bool):
        w = (MainWatch if main else InputWatch)()
        w.front_pid = lambda: self.front["pid"]
        w._pid_bundle = {ITERM: "com.googlecode.iterm2", MUSIC: TE, DESKMIND: "ai.deskmind.app",
                         DOCK: "com.apple.dock", CHAT_PID: CHAT, NE_PID: NE}
        w._pid_regular = {ITERM: True, MUSIC: True, DESKMIND: True, DOCK: False, CHAT_PID: True, NE_PID: True}
        w.active, w.keys = True, True
        self.w = w
        if not main:
            getattr(w, "watch_front", lambda: None)()       # the poller (absent before the observed front)
            self.addCleanup(getattr(w, "stop_front", lambda: None))
        return w

    def focus(self, pid: int, settle: bool = True):
        """The app with the keyboard is now `pid`; with `settle`, it stays so long enough to be read a few times."""
        self.front["pid"] = pid
        if settle:
            threading.Event().wait(0.08)

    def cmd_key(self, at: float | None = None, target: int = DOCK):
        """The user's key at `at` (now when not given): a Command chord, the Dock its target, unless `target`."""
        self.fields[40] = target
        if at is None:
            self.fields.pop("ts", None)
        else:
            self.fields["ts"] = _uptime_ns() - int(max(0.0, time.time() - at) * 1e9)
        self.key()

    def _driver(self, app: str = TE):
        from deskmind_hands.drivers import peekaboo as pk
        d = pk.PeekabooDriver.__new__(pk.PeekabooDriver)
        d.app, d._mcp, d._foreground_actions, d._chat_sends = app, object(), 0, 0
        d._front_app = lambda: APPS.get(self.front["pid"], ("", ""))
        d._front_bundle = lambda: APPS.get(self.front["pid"], ("", ""))[1]
        d._task_windows = lambda: []
        d._hid_idle = lambda: 100.0
        d._osa = lambda script: (True, "")
        d._takeover = lambda: True
        d._resumed = lambda: None
        d._background_input = lambda bundle: False
        d.BROWSERS = set()
        d._browser_front_sid = lambda bundle: None
        d._window_id = None
        by_bundle = {b.lower(): pid for pid, (_, b) in APPS.items()}

        def bring(bundle, browser):
            self.front["pid"] = by_bundle[bundle.lower()]
            return True
        d._bring_forward = bring

        def give_back(prev, ours=None, late_s=0.0):        # the give-back itself is not what this table tests
            if APPS.get(self.front["pid"], ("", ""))[1].lower() == (ours or "").lower():
                self.front["pid"] = by_bundle.get(prev.lower(), self.front["pid"])
        d._give_front_back = give_back
        return d

    def _system(self, **quartz):
        import os as _os
        from deskmind_hands.drivers import peekaboo as pk
        q = _quartz(self.fields)
        for k, v in {"CGEventGetLocation": lambda e: (0, 0), "CGEventCreate": lambda x: None,
                     "CGWarpMouseCursorPosition": lambda p: None,
                     "CGAssociateMouseAndMouseCursorPosition": lambda b: None,
                     "CGEventPost": lambda tap, e: None, "CGEventCreateMouseEvent": lambda *a: None,
                     "kCGEventMouseMoved": 0, "kCGMouseButtonLeft": 0, "kCGHIDEventTap": 0, **quartz}.items():
            setattr(q, k, v)
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(mock.patch.dict(sys.modules, {"Quartz": q}))
        stack.enter_context(mock.patch.dict(_os.environ, {"HANDS_FLASH_APPS": f"{TE},{NE}", "HANDS_CHAT_FLASH": "1"}))
        stack.enter_context(mock.patch.object(pk, "require_usable", return_value="active"))
        stack.enter_context(mock.patch.object(pk, "_ascii_input_source",
                                              return_value=types.SimpleNamespace(restore=lambda: None)))
        stack.enter_context(mock.patch.object(pk, "hid_idle_seconds", return_value=60.0))
        by_bundle = {b.lower(): pid for pid, (_, b) in APPS.items()}

        def run(args, *a, **k):                            # `open -b X` brings X forward; nothing else is run
            if list(args[:2]) == ["/usr/bin/open", "-b"] and str(args[2]).lower() in by_bundle:
                self.front["pid"] = by_bundle[str(args[2]).lower()]
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        stack.enter_context(mock.patch.object(pk.subprocess, "run", side_effect=run))
        return stack

    def _step(self, ex, stub, inner, app: str = TE, **quartz):
        """One action of the task's through executor.perform and the real driver.execute, `inner` as the step."""
        from deskmind_hands.drivers.base import ExecResult
        d = self._driver(app)
        d._execute_inner = lambda a: inner(d) or ExecResult(True, "done")
        stub.execute = d.execute
        with self._system(**quartz):
            return ex.perform({"kind": "click", "window": "com.apple.TextEdit#12", "target": "B2"})

    # -- the scenarios: each leaves the executor right after the user's input; the verdict is the next snapshot -------
    def review_textedit_forward_by_itself_during_a_step(self, ex, stub):
        """A background action brought TextEdit forward by itself; the user typed a Command key into it and
        Command-Tabbed back to the terminal; before and after the step the terminal is in front. The key's callback
        runs after the step, within its freshness, the front read naming the terminal (hands#62 review at 9def177)."""
        when = {}

        def inner(d):
            self.focus(MUSIC)                              # TextEdit came forward by itself
            when["key"] = time.time() - 0.005              # the Command key into it ...
            self.focus(ITERM, settle=False)                # ... and the switch back to the terminal
        self._step(ex, stub, inner)
        self.cmd_key(at=when["key"])

    def late_flash_registration_key_during_perform(self, ex, stub):
        """The earlier P1: a key into the flashed app while perform() runs, its callback then, the front meanwhile
        in the terminal (hands#62 review at af55263)."""
        def perform(Q):
            self.focus(ITERM, settle=False)
            self.cmd_key()
            return "typed"
        self._step(ex, stub, lambda d: d._flash(TE, perform)[1] and None)

    def chat_click_key_during_the_click(self, ex, stub):
        import types as _types
        from deskmind_hands.drivers import peekaboo as pk
        self.addCleanup(setattr, pk, "CHAT_APP", pk.CHAT_APP)
        pk.CHAT_APP = CHAT
        ex.current[CHAT] = f"{CHAT}#5"                     # the task operates the chat app too
        keyed = []

        def post(tap, e):
            if not keyed:
                keyed.append(1)
                self.focus(ITERM, settle=False)
                self.cmd_key()

        def inner(d):
            d._labels_by_id = {"B1": "打开"}
            d._rects = {"B1": _types.SimpleNamespace(x=10, y=10, w=20, h=10)}
            d._origin = _types.SimpleNamespace(x=0, y=0)
            d._moved_since_seen = lambda: None
            d._add_wait = lambda s: None
            d._chat_flash_click("B1")
            self.focus(ITERM, settle=False)
        self._step(ex, stub, inner, app=CHAT, CGEventPost=post)

    def staging_key_while_the_task_app_had_the_front(self, ex, stub):
        """Staging (a driver started for TextEdit) brought it forward; a key in it, the user then in the terminal:
        the front read after staging names the terminal."""
        def during():
            self.focus(MUSIC, settle=False)
            self.focus(ITERM, settle=False)
            self.cmd_key()
        fronts = [("iTerm2", "com.googlecode.iterm2"), ("iTerm2", "com.googlecode.iterm2")]
        self._stage(TE, [], fronts, during=during)

    _stage = FlashesTold._stage

    def cold_launch_key_while_the_app_had_the_front(self, ex, stub):
        from deskmind_hands import executor as exe
        ex.current[NE] = f"{NE}#7"                         # the task goes on to operate the launched app
        ex._known_windows = lambda: set()                  # noqa: SLF001
        ex._front_bundle_id = lambda: "com.googlecode.iterm2"   # noqa: SLF001
        ex._make_frontmost = lambda b: True                # noqa: SLF001

        def windows(*a):
            self.focus(NE_PID, settle=False)               # it took the front at launch ...
            self.focus(ITERM, settle=False)                # ... and the user went back to the terminal
            self.cmd_key()
            return []
        ex._launch_windows = windows                       # noqa: SLF001
        ex._settled = mock.Mock(side_effect=RuntimeError("stop here"))   # noqa: SLF001
        with mock.patch.object(exe, "resolve_app", return_value=NE), \
             mock.patch.object(exe, "running_apps", return_value=[]), \
             mock.patch.object(exe, "installed_apps", return_value=[]), \
             mock.patch.object(exe.subprocess, "run"):
            try:
                ex._open_window(NE, None)                  # noqa: SLF001
            except RuntimeError:
                pass

    def terminal_typing_while_the_flash_waits_for_idle(self, ex, stub):
        """The user works in the terminal while the driver waits for a still Mac before it brings TextEdit forward:
        their Command keys went to the terminal, before any app of the task's had the front."""
        def inner(d):
            d._takeover = lambda: False
            idle = iter([0.0, 100.0, 100.0, 100.0])        # busy, then still
            d._hid_idle = lambda: next(idle, 100.0)

            def wait(waited, bundle, what):
                self.cmd_key()                             # typing in the terminal meanwhile
                return True
            d._announce_wait = wait
            d._flash(TE, lambda Q: "typed")
        self.focus(ITERM)
        self._step(ex, stub, inner)

    def terminal_typing_between_steps(self, ex, stub):
        """The exception: every piece of evidence there -- nothing of the driver's under way over the key's span
        (the step ended more than FRONT_MARGIN_S before), the front seen throughout, the read at the callback the
        terminal, no app of the task's seen."""
        self._step(ex, stub, lambda d: None)
        self.focus(ITERM)
        threading.Event().wait(0.15)
        self.cmd_key()

    def brief_front_blip_during_a_step(self, ex, stub):
        """TextEdit came forward by itself for ~4 ms during a step, between two reads; a Command key landed in it
        (hands#62 review at e8fa010, B1a): the record cannot see it, the step under way makes it count."""
        when = {}

        def inner(d):
            self.front["pid"] = MUSIC
            when["at"] = time.time()
            threading.Event().wait(0.004)
            self.front["pid"] = ITERM
        self._step(ex, stub, inner)
        self.cmd_key(at=when["at"])

    def routed_after_a_read_past_the_keys_time(self, ex, stub):
        """Stamped at t, a read after it says the terminal, TextEdit came forward 30 ms after t and the key was routed
        to it: the span runs to the callback's start (B1c)."""
        self._step(ex, stub, lambda d: None)
        self.focus(ITERM)
        threading.Event().wait(0.15)
        at = time.time()
        threading.Event().wait(0.03)                       # at least one read of the terminal after the key's time
        self.focus(MUSIC, settle=False)
        threading.Event().wait(0.04)
        self.cmd_key(at=at)

    def wall_clock_stepped_back_key_in_the_task_app(self, ex, stub):
        """The user in the terminal; the wall clock steps back 0.4 s; they switch to TextEdit and type a Command key
        (B1d): reads stamped by the stepped clock were dropped as older than the record, and the key's span fell on
        the terminal's history. The record and the span are on the monotonic clock now."""
        self._step(ex, stub, lambda d: None)
        self.focus(ITERM)
        threading.Event().wait(0.6)
        real = time.time
        with mock.patch.object(time, "time", lambda: real() - 0.4):
            self.focus(MUSIC)
            self.cmd_key()

    def slow_read_answers_the_terminal_after_a_switch_and_back(self, ex, stub):
        """A read begins; while it is out the user brings TextEdit forward, types a Command key into it and goes back
        to the terminal; the read answers "terminal" (Codex at f0f7186: the read was taken to show the terminal from
        its start, and the key was the terminal's)."""
        self._step(ex, stub, lambda d: None)
        self.focus(ITERM)
        threading.Event().wait(0.15)
        real, slow = self.w.front_pid, [True]

        def front():
            if slow and slow.pop():
                threading.Event().wait(0.10)               # this one read is slow; it answers what holds then
            return real()
        self.w.front_pid = front
        threading.Event().wait(0.03)                       # the slow read is out
        self.front["pid"] = MUSIC
        at = time.time()
        threading.Event().wait(0.005)
        self.front["pid"] = ITERM
        threading.Event().wait(0.15)                       # it answers the terminal; reads go on at their pace
        self.cmd_key(at=at)

    def _chord(self, *codes: int):
        """Command down, the keys `codes` with it (each a keydown, the Dock its target), Command up."""
        cmd = 1 << 20                                      # kCGEventFlagMaskCommand
        self.fields.pop("ts", None)
        self.fields[40] = DOCK
        self.fields["flags"] = cmd
        self.w._callback(None, 12, object(), None)         # Command pressed
        for code in codes:
            self.fields[9] = code
            self.key()
        self.fields["flags"] = 0
        self.w._callback(None, 12, object(), None)         # Command let go
        self.fields.pop(9, None)

    def switcher_then_q_on_the_task_app(self, ex, stub):
        """In the terminal, the user holds Command, presses Tab to pick TextEdit in the switcher and Q to quit it:
        the front stays the terminal, every key the Dock's."""
        self._step(ex, stub, lambda d: None)
        self.focus(ITERM)
        threading.Event().wait(0.15)
        self._chord(48, 48, 12)                            # Tab, Tab, Q

    def plain_command_chord_in_the_terminal_between_steps(self, ex, stub):
        """Command-C in the terminal, no switcher: the rc.13 exception holds."""
        self._step(ex, stub, lambda d: None)
        self.focus(ITERM)
        threading.Event().wait(0.15)
        self._chord(8)                                     # C

    def _hang_poller(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        real = self.w.front_pid
        self.w.front_pid = lambda: (gate.wait(3), real())[1]
        threading.Event().wait(0.05)

    def hung_poller_key_in_the_task_app(self, ex, stub):
        self._step(ex, stub, lambda d: None)
        self._hang_poller()
        self.focus(MUSIC, settle=False)
        at = time.time()
        self.focus(ITERM, settle=False)
        self.cmd_key(at=at)

    def hung_poller_terminal_typing_between_steps(self, ex, stub):
        """Nothing seen, so nothing proves the terminal had it: counted, as on main."""
        self._step(ex, stub, lambda d: None)
        self.focus(ITERM)
        self._hang_poller()
        threading.Event().wait(0.2)
        self.cmd_key()

    def dead_poller_key_in_the_task_app(self, ex, stub):
        self._step(ex, stub, lambda d: None)
        if hasattr(self.w, "_poll_stop"):
            self.w.front_pid = lambda: (_ for _ in ()).throw(SystemExit())   # the poller's thread dies
            threading.Event().wait(0.05)
            self.w.front_pid = lambda: self.front["pid"]
        self.focus(MUSIC, settle=False)
        at = time.time()
        self.focus(ITERM, settle=False)
        threading.Event().wait(0.2)
        self.cmd_key(at=at)

    def key_without_a_timestamp_in_the_task_app(self, ex, stub):
        self._step(ex, stub, lambda d: None)
        self.focus(MUSIC)
        self.focus(ITERM, settle=False)
        self.fields[40] = DOCK
        with mock.patch.object(InputWatch, "_event_age", staticmethod(lambda Q, e: None)):
            self.key()

    def slow_front_reads_key_in_the_task_app(self, ex, stub):
        """Each read takes ~120 ms: TextEdit in front for ~100 ms between two reads, a Command key into it."""
        self._step(ex, stub, lambda d: None)
        real = self.w.front_pid

        def slow():
            threading.Event().wait(0.12)
            return real()
        self.w.front_pid = slow
        self.focus(ITERM)
        threading.Event().wait(0.3)
        self.front["pid"] = MUSIC
        at = time.time()
        threading.Event().wait(0.1)
        self.front["pid"] = ITERM
        self.cmd_key(at=at)

    def command_key_in_the_task_app_between_steps(self, ex, stub):
        self._step(ex, stub, lambda d: None)
        self.focus(MUSIC)                                  # the user in TextEdit themselves
        self.cmd_key()
        self.focus(ITERM, settle=False)

    def plain_key_into_the_task_app_between_steps(self, ex, stub):
        self._step(ex, stub, lambda d: None)
        self.focus(MUSIC)
        self.cmd_key(target=MUSIC)

    def late_callback_of_a_key_typed_in_the_task_app(self, ex, stub):
        self._step(ex, stub, lambda d: None)
        self.focus(MUSIC)
        at = time.time()
        self.focus(ITERM)                                  # the callback runs once the user is back in the terminal
        self.focus(ITERM)
        self.cmd_key(at=at)

    # -- the table --------------------------------------------------------------------------------------------------
    def stales(self, scenario: str, main: bool) -> bool:
        from deskmind_hands.drivers import peekaboo as pk
        from deskmind_hands.grounding import STOP
        STOP.clear()
        self.iw = __import__("deskmind_hands.inputwatch", fromlist=["FLASHES"])
        self.iw.FLASHES.clear()
        self._watch(main)
        self.focus(ITERM)                                  # the user in the terminal, read for a while already
        ex, stub, _ = self._task()
        if main:
            ex._input_by_source = lambda: False            # noqa: SLF001 -- main moved the mark at every action's end
        with mock.patch.object(pk.time, "sleep"):
            getattr(self, scenario)(ex, stub)
        e = ex.epoch
        s = ex.snapshot()
        return s["epoch"] == e + 1 and s["epoch_reason"].get("by") == "input"

    def test_wherever_main_stales_the_branch_stales_but_for_the_listed_exception(self):
        got = []
        for scenario, main_expected, branch_expected, exception in self.TABLE:
            with self.subTest(scenario=scenario):
                main, branch = self.stales(scenario, True), self.stales(scenario, False)
                got.append((scenario, main, branch))
                self.assertEqual(main, main_expected, f"main: {scenario}")
                self.assertEqual(branch, branch_expected, f"branch: {scenario}")
                if not exception:
                    self.assertTrue(branch or not main, f"main stales and the branch does not: {scenario}")
        self.assertEqual([s for s, _, _, x in self.TABLE if x],      # the one exception, two ways of typing it
                         ["terminal_typing_between_steps", "plain_command_chord_in_the_terminal_between_steps"])

    def test_review_the_app_that_came_forward_by_itself_during_a_step_stales_it(self):
        """The review's scenario alone (hands#62 review at 9def177): fails there, epoch 2 -> 2."""
        self.assertTrue(self.stales("review_textedit_forward_by_itself_during_a_step", False))


class ObservedFront(_KeysCase):
    """The front as observed (front_seen / fronts_between, monotonic seconds): runs of reads, each taken to reach back
    to the read before it and on to the read after it; time between two reads further apart than FRONT_GAP_S -- from
    the one's start to the other's answer -- is unseen, and a key over it counts as main counted it."""

    def test_an_app_seen_in_front_over_the_span_is_found_and_a_change_reaches_both_reads(self):
        w, t = self.w, 1000.0
        for i in range(5):
            w.front_seen(ITERM, t + i * 0.02)
        for i in range(5, 10):
            w.front_seen(MUSIC, t + i * 0.02)              # TextEdit from somewhere between 0.08 and 0.10
        self.assertEqual(w.fronts_between(t + 0.081, t + 0.085, wait_s=0), [ITERM, MUSIC])   # either, then
        self.assertEqual(w.fronts_between(t + 0.15, t + 0.16, wait_s=0), [MUSIC])
        self.assertIsNone(w.fronts_between(t - 1, t + 0.05, wait_s=0))      # nothing read before the span
        self.assertIsNone(w.fronts_between(t + 0.1, t + 0.5, wait_s=0))     # nor after it

    def test_time_between_reads_too_far_apart_or_a_slow_read_is_unseen(self):
        from deskmind_hands import inputwatch as iw
        w, t = self.w, 1000.0
        w.front_seen(ITERM, t)
        w.front_seen(ITERM, t + iw.FRONT_GAP_S + 0.05)    # the poller stalled in between
        self.assertIsNone(w.fronts_between(t + 0.01, t + 0.02, wait_s=0))
        g = t + iw.FRONT_GAP_S + 0.05
        w.front_seen(ITERM, g + 0.02)
        self.assertEqual(w.fronts_between(g, g + 0.01, wait_s=0), [ITERM])
        w.front_seen(ITERM, g + 0.04, g + 0.16)            # a read that took 120 ms ...
        w.front_seen(ITERM, g + 0.18, g + 0.30)            # ... and another: an app in front between is not seen
        self.assertIsNone(w.fronts_between(g + 0.05, g + 0.1, wait_s=0))

    def test_a_slow_read_proves_its_answer_at_one_instant_not_from_its_start(self):
        """Codex at f0f7186: a read begun at 20 ms answered "terminal" at 140 ms; the user typed into TextEdit at
        125 ms and was back in the terminal at 130 ms. Taken from its start, the terminal covered 20-140 ms and the
        key was the terminal's; the read proves the terminal only at some instant in 20-140 ms."""
        w, t = self.w, 1000.0
        w.front_seen(ITERM, t, t + 0.0002)
        w.front_seen(ITERM, t + 0.02, t + 0.14)            # slow
        for k in range(1, 6):
            w.front_seen(ITERM, t + 0.14 + 0.02 * k)
        self.assertIsNone(w.fronts_between(t + 0.125 - 0.1, t + 0.126, wait_s=0))   # unseen: it counts
        self.assertEqual(w.fronts_between(t + 0.17, t + 0.2, wait_s=0), [ITERM])   # reads at pace again: seen

    def test_a_read_older_than_the_last_never_moves_the_record_back(self):
        w = self.w
        w.front_seen(ITERM, 1000.0)
        w.front_seen(MUSIC, 999.0)
        self.assertEqual(list(w._front_reads), [(ITERM, 1000.0, 1000.0)])

    def _seen(self, *runs):
        """The poller's reads, 20 ms apart: (pid, seconds) runs, oldest first, ending now; the last read on after."""
        self.w._front_reads.clear()
        t = time.monotonic() - sum(secs for _, secs in runs)
        for pid, secs in runs:
            for i in range(int(round(secs / 0.02))):
                self.w.front_seen(pid, t + i * 0.02)
            t += secs
        for i in range(5):                                 # and on past the callback, 20 ms apart
            self.w.front_seen(runs[-1][0], t + i * 0.02)

    def test_a_key_counts_for_every_app_seen_and_the_terminal_alone_stales_nothing(self):
        self.front["pid"] = ITERM
        self._seen((MUSIC, 0.3), (ITERM, 0.02))           # the task's app until a moment ago
        self.fields[40] = DOCK
        self.key()
        self.assertGreater(self.ex._last_input_at(), 0.0)  # noqa: SLF001
        self.w.last_by_bundle.clear()
        self._seen((ITERM, 0.5))                           # the terminal all along, nothing of the driver's under way
        self.key()
        self.assertEqual(self.ex._last_input_at(), 0.0)    # noqa: SLF001
        self.assertIn("com.googlecode.iterm2", self.w.last_by_bundle)

    def test_without_every_piece_of_evidence_the_key_counts_as_on_main(self):
        """The terminal seen all along, yet: a step under way, a flash, the read at the callback failing or naming
        another app than the record, no timestamp -- each alone makes the key count."""
        from deskmind_hands import inputwatch as iw

        def case(prepare):
            self.w.last_by_bundle.clear()
            self.w.last_unknown_at = self.w.last_doubt_at = self.w._front_backoff_until = 0.0
            self.front["pid"] = ITERM
            self.w.front_pid = lambda: self.front["pid"]
            self._seen((ITERM, 0.5))
            undo = prepare()
            self.fields[40] = DOCK
            self.key()
            if undo:
                undo()
            return self.ex._last_input_at() > 0.0          # noqa: SLF001
        self.assertFalse(case(lambda: None))               # the exception: all the evidence there
        step = []
        self.assertTrue(case(lambda: step.append(self.w.step_began(NE)) or (lambda: self.w.step_ended(step[0]))))
        tok = []
        self.assertTrue(case(lambda: tok.append(iw.front_taken("com.example.other")) or
                             (lambda: iw.front_given_back(tok[0]))))
        self.assertTrue(case(lambda: setattr(self.w, "front_pid", lambda: 0)))            # the read failed (3a)
        self.assertTrue(case(lambda: setattr(self.w, "front_pid", lambda: MUSIC)))
        self.assertTrue(case(lambda: setattr(self.w, "front_pid", lambda: DESKMIND)))     # not among the seen (3b)
        with mock.patch.object(InputWatch, "_event_age", staticmethod(lambda Q, e: None)):
            self.assertTrue(case(lambda: None))

    def test_an_unreadable_front_counts_and_deskmind_alone_is_deskminds(self):
        self.front["pid"] = 0
        self._seen((0, 0.3))                               # the read failed
        self.fields[40] = DOCK
        self.key()
        self.assertTrue(self.counted_unknown())
        self.w.last_unknown_at = self.w.last_doubt_at = self.w._front_backoff_until = 0.0
        self.front["pid"] = DESKMIND
        self._seen((DESKMIND, 0.3))
        self.key()
        self.assertGreater(self.w.last_own_at, 0.0)
        self.assertFalse(self.counted_unknown())

    def test_the_wall_clock_stepped_back_moves_nothing(self):
        self.front["pid"] = ITERM
        self.observe_front(settle=0.6)                     # the terminal's history
        real = time.time
        with mock.patch.object(time, "time", lambda: real() - 0.4):
            self.front["pid"] = MUSIC                      # the user goes to TextEdit as the clock steps back
            threading.Event().wait(0.08)
            self.fields[40] = DOCK
            self.key()
        self.assertGreater(self.ex._last_input_at(), 0.0)  # noqa: SLF001

    def test_a_hung_or_dead_poller_does_not_hold_the_taps_thread(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.w.front_pid = lambda: (gate.wait(5), ITERM)[1]
        self.observe_front(settle=0.2)                     # its first read hangs
        self.fields[40] = DOCK
        t = time.monotonic()
        for _ in range(10):
            self.key()
        self.assertLess(time.monotonic() - t, 0.3)         # was ~100 ms a key (hands#62 review at e8fa010)
        self.assertGreater(self.ex._last_input_at(), 0.0)  # noqa: SLF001 -- nothing seen: it counts
        gate.set()
        w2 = InputWatch()
        w2.front_pid = lambda: (_ for _ in ()).throw(SystemExit())   # the poller's thread dies at its first read
        w2.watch_front()
        threading.Event().wait(0.05)
        t = time.monotonic()
        self.assertIsNone(w2.fronts_between(time.monotonic() - 0.1, time.monotonic()))
        self.assertLess(time.monotonic() - t, 0.05)

    def test_the_poller_reads_the_front_and_a_stopped_one_is_not_waited_for(self):
        self.front["pid"] = MUSIC
        self.observe_front(settle=0.1)
        self.assertEqual(self.w.fronts_between(time.monotonic() - 0.05, time.monotonic()), [MUSIC])
        self.w.stop_front()
        t = time.monotonic()
        self.assertIsNone(self.w.fronts_between(time.monotonic() + 1, time.monotonic() + 2))
        self.assertLess(time.monotonic() - t, 0.05)

    def test_a_key_to_a_system_panel_counts_whatever_the_front(self):
        """Only a key with no target or the Dock's is weighed: one the window server sent to a launcher's panel or
        Spotlight (no ordinary app) counts as on main, the terminal seen throughout (delta review at f0f7186)."""
        launcher = 77
        self.w._pid_bundle[launcher], self.w._pid_regular[launcher] = "com.example.launcher", False
        self.front["pid"] = ITERM
        self.observe_front()
        self.fields[40] = launcher
        self.key()
        self.assertGreater(self.ex._last_input_at(), 0.0)  # noqa: SLF001

    def test_a_reused_pid_is_asked_anew_on_the_long_lived_watch(self):
        """pid 31 was the terminal; later the task's app runs with it. A key routed to it is the task's app's."""
        from deskmind_hands import inputwatch as iw
        started = {"t": 100.0}
        with mock.patch.object(iw, "_proc_info", lambda pid: (1, started["t"])):
            self.assertFalse(self.w.is_own(ITERM))         # known as the terminal, its start time noted
            started["t"] = 200.0                           # another process has pid 31 now
            with mock.patch.object(InputWatch, "_bundle_of", staticmethod(lambda pid: NE)), \
                 mock.patch.object(InputWatch, "_regular_of", staticmethod(lambda pid: True)):
                self.fields[40] = ITERM
                self.key()
        self.assertIn(NE, self.w.last_by_bundle)
        self.assertGreater(self.ex._last_input_at(), 0.0)  # noqa: SLF001

    def test_a_pid_whose_start_time_could_not_be_read_is_not_trusted_later(self):
        """Delta review at 45e67fc: the terminal's pid looked up while its start time could not be read, the terminal
        gone, the pid reused by the task's app: the key is the task's app's."""
        from deskmind_hands import inputwatch as iw
        info = {"v": None}
        self.w._pid_bundle.pop(ITERM, None)
        self.w._pid_regular.pop(ITERM, None)
        with mock.patch.object(iw, "_proc_info", lambda pid: info["v"]), \
             mock.patch.object(InputWatch, "_regular_of", staticmethod(lambda pid: True)):
            with mock.patch.object(InputWatch, "_bundle_of", staticmethod(lambda pid: "com.googlecode.iterm2")):
                self.assertFalse(self.w.is_own(ITERM))     # the terminal, start time unreadable
                self.assertTrue(self.w.is_regular(ITERM))
            info["v"] = (1, 300.0)                         # the task's app now has the pid, readable
            with mock.patch.object(InputWatch, "_bundle_of", staticmethod(lambda pid: NE)):
                self.fields[40] = ITERM
                self.key()
        self.assertIn(NE, self.w.last_by_bundle)
        self.assertGreater(self.ex._last_input_at(), 0.0)  # noqa: SLF001

    def _chord(self, *codes: int, release: bool = True):
        cmd = 1 << 20                                      # kCGEventFlagMaskCommand
        self.fields[40] = DOCK
        self.fields["flags"] = cmd
        self.w._callback(None, 12, object(), None)
        for code in codes:
            self.fields[9] = code
            self.key()
        if release:
            self.fields["flags"] = 0
            self.w._callback(None, 12, object(), None)

    def test_keys_in_the_app_switcher_count_and_a_plain_command_chord_does_not(self):
        """Delta review at 45e67fc: Command-Tab to TextEdit, then Q, the terminal in front all along -- counted as on
        main; Command-C in the terminal, no switcher -- the rc.13 exception; the switcher closes when Command is let
        go."""
        self.front["pid"] = ITERM
        self.observe_front()
        self._chord(8)                                     # Command-C
        self.assertEqual(self.ex._last_input_at(), 0.0)    # noqa: SLF001
        for codes in ((48, 12), (50, 4)):                  # Command-Tab then Q; Command-` then H
            self.w.last_doubt_at = 0.0
            self._chord(*codes)
            self.assertGreater(self.ex._last_input_at(), 0.0)  # noqa: SLF001
        self.w.last_doubt_at = 0.0
        self._chord(8)                                     # Command let go before: no switcher any more
        self.assertEqual(self.ex._last_input_at(), 0.0)    # noqa: SLF001

    def test_a_watch_started_without_keys_is_replaced_once_they_are_permitted(self):
        """Delta review at 45e67fc: a watch made before Input Monitoring was granted watched no keys for the rest of
        the process."""
        from deskmind_hands import executor as exe
        perm, made, stopped = {"keys": False}, [], []

        class W(InputWatch):
            @staticmethod
            def keys_permitted():
                return perm["keys"]

            def start(self):
                made.append(self)
                self.keys, self.active = perm["keys"], True
                return True

            def stop(self):
                stopped.append(self)
        self.addCleanup(exe._SHARED_WATCH.clear)
        exe._SHARED_WATCH.clear()
        with mock.patch.object(exe, "InputWatch", W):
            first = exe.shared_watch()
            self.assertIs(exe.shared_watch(), first)        # no permission yet: kept
            perm["keys"] = True
            second = exe.shared_watch()
        self.assertIsNot(second, first)
        self.assertTrue(second.keys)
        self.assertEqual(stopped, [first])

    def test_a_doubt_keeps_the_user_active_after_an_action_ends(self):
        """user_active looks at the last USER_ACTIVE_S, as main did: an action's end does not hide a doubt there."""
        self.w.front_pid = lambda: 0
        self.fields[40] = DOCK
        self.key()
        self.assertGreater(self.w.last_doubt_at, 0.0)
        self.ex._action_end_at = self.w.mark()             # the next action ends
        self.ex.USER_ACTIVE_S = 3.0
        self.assertTrue(self.ex.user_active())

    def test_a_watch_that_could_not_start_is_tried_again_by_the_next_begin(self):
        """Codex at f0f7186: a failed first start was kept (None) for the process, and every later task fell back to
        global input; main tried again for each task."""
        from deskmind_hands import executor as exe
        starts = []

        class W(InputWatch):
            def start(self):
                starts.append(self)
                self.active = len(starts) > 1               # the permission granted after the first try
                return self.active
        self.addCleanup(exe._SHARED_WATCH.clear)
        exe._SHARED_WATCH.clear()
        with mock.patch.object(exe, "InputWatch", W):
            self.assertIsNone(exe.shared_watch())
            w = exe.shared_watch()
            self.assertIs(w, starts[1])
            self.assertIs(exe.shared_watch(), w)            # kept once started
        self.assertEqual(len(starts), 2)

    def test_every_begin_shares_one_watch(self):
        from deskmind_hands import executor as exe
        made = []

        class W(InputWatch):
            def start(self):
                made.append(self)
                return True
        self.addCleanup(exe._SHARED_WATCH.clear)
        exe._SHARED_WATCH.clear()
        with mock.patch.object(exe, "InputWatch", W):
            for _ in range(3):
                ex = exe.HandsExecutor.__new__(exe.HandsExecutor)
                ex._watch_tried, ex._watch, ex.drivers, ex.current = False, None, {}, {}
                ex._last_input_at()                        # noqa: SLF001
        self.assertEqual(len(made), 1)


#: Faster than any person brings an app forward, types into it and goes back: the oracle's own number, not the
#: implementation's (FRONT_GAP_S).
HUMAN_S = 0.05


class EvidenceProperty(_KeysCase):
    """Random interleavings of front changes (blips shorter than a read included), reads (some slow, some missing; each
    reporting the front at one random instant within [start, answer]), driver actions under way (steps, flashes) and
    Command keys whose callbacks run at once or late. Main counted every one of these keys. Judged against the truth
    -- who had the keyboard at the key's own instant -- never the implementation's reading of the reads (hands#62
    delta review at f0f7186): a key into the task's app counts unless its stint there was no longer than FRONT_GAP_S
    and nothing of the driver's was under way (the one thing polling cannot see, and no person can do)."""

    def test_the_branch_counts_wherever_main_counts_unless_the_evidence_held(self):
        import random
        from deskmind_hands import inputwatch as iw
        rng = random.Random(62)
        w = self.w
        pids = [ITERM, ITERM, MUSIC, DESKMIND, 0]
        task = "com.apple.textedit"
        w._pid_bundle[MUSIC] = TE
        dropped = 0
        for trial in range(400):
            w._front_reads.clear(); w._steps = []; iw.FLASHES.clear()
            w.last_by_bundle.clear(); w.last_unknown_at = w.last_doubt_at = w.last_own_at = 0.0
            w._front_backoff_until = 0.0
            now_m, now_w = time.monotonic(), time.time()
            base = now_m - 4.0
            segs, t = [], base                              # the truth: who had the keyboard when
            while t < now_m + 1.0:
                d = rng.choice([0.003, 0.01, 0.05, 0.2, 0.6])
                segs.append((t, t + d, rng.choice(pids)))
                t += d

            def truth(x):
                for a, b, p in segs:
                    if a <= x < b:
                        return p
                return segs[-1][2]
            reads, r = [], base
            while r < now_m + 0.8:
                dur = rng.choice([0.0003] * 16 + [0.01, 0.03, 0.12])   # some reads slow
                if rng.random() > 0.03:                     # a read now and then never happens
                    reads.append((r, r + dur, truth(r + rng.random() * dur)))   # what held at one instant within
                r += dur + 0.02
            for a, b, p in reads:
                w.front_seen(p, a, b)
            for _ in range(rng.choice([0, 0, 1, 2])):
                a = base + rng.random() * 3.5
                b = a + rng.choice([0.05, 0.3, 1.0])
                tok = (w.step_began(TE) if rng.random() < 0.5 else iw.front_taken(TE))
                tok[4], tok[5], tok[6] = a, b, b + 100
                tok[1], tok[2], tok[3] = a - now_m + now_w, b - now_m + now_w, b - now_m + now_w + 100
            ev = base + 0.5 + rng.random() * 3.0
            t0 = ev + rng.choice([0.0005, 0.003, 0.03, 0.3])
            w.front_pid = lambda t0=t0: truth(t0)
            self.fields[40] = DOCK
            w.note(DOCK, key=True, age=t0 - ev, t0=t0)
            counted = w.last_input_for({task}) > 0.0

            # -- the oracle: the truth, never the implementation's own reading of the reads. A key typed while the
            # task's app had the keyboard counts when that stint was longer than FRONT_GAP_S (no person switches,
            # types and switches back faster), and whatever the stint when a driver action was under way over the key's
            # span (the action may have raised the app for a moment)
            a, b = ev - iw.FRONT_MARGIN_S, t0
            busy = any(tok[4] <= b and (tok[5] + (iw.FLASH_MARGIN_S if tok in iw.FLASHES else 0)) >= a
                       for tok in list(w._steps) + list(iw.FLASHES))
            stint = next(sb - sa for sa, sb, p in segs if sa <= ev < sb)
            into_task = truth(ev) == MUSIC
            with self.subTest(trial=trial):
                if into_task and (stint > HUMAN_S or busy):
                    self.assertTrue(counted, (trial, round(ev - base, 3), round(t0 - ev, 4), round(stint, 3), busy))
            dropped += not counted
        self.assertGreater(dropped, 20)                     # keys are dropped at all: the property is not vacuous



class InFlightAndTheMark(_EpochCase):
    """Delta review at f0f7186: every way of opening a window is under way for the watch, and a step that ends with
    an error or is stopped leaves main's mark where it was (main moved it only at a completed action's end)."""

    def test_every_way_of_opening_a_window_is_under_way(self):
        from deskmind_hands import executor as exe
        ex, stub, W12 = self._task()
        seen = []

        def run(args, *a, **k):
            now = time.monotonic()
            seen.append((list(args[:3]), self.w.in_flight(now - 0.01, now)))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        ex._fresh_windows = lambda before, wait, bundle: []                     # noqa: SLF001
        with mock.patch.object(exe, "resolve_app", return_value=TE), \
             mock.patch.object(exe, "running_apps", return_value=[("TextEdit", TE)]), \
             mock.patch.object(exe, "installed_apps", return_value=[]), \
             mock.patch.object(exe, "is_browser", return_value=False), \
             mock.patch.object(exe.subprocess, "run", side_effect=run):
            ex._open_window(TE, "file:///tmp/x.txt")      # noqa: SLF001 -- open -g -b with a URL
            ex._open_window(TE, None)                      # noqa: SLF001 -- a new document by the app's scripting
        self.assertTrue(seen and all(f for _, f in seen), seen)
        threading.Event().wait(0.01)
        self.assertFalse(self.w.in_flight(time.monotonic() - 0.001, time.monotonic()))   # and over after

    def _doubt_during(self, ex, stub, how):
        real = stub.execute

        def during(a):
            self.front["pid"] = ITERM
            self.w.front_pid = lambda: 0                   # nothing proves where the key went: a doubt
            self.fields[40] = DOCK
            self.key()
            if how == "raise":
                raise RuntimeError("the driver failed")
            return real(a)
        stub.execute = during
        try:
            return ex.perform({"kind": "click", "window": "com.apple.TextEdit#12", "target": "B2"})
        except RuntimeError:
            return None

    def test_a_doubt_in_a_step_that_ends_with_an_error_or_is_stopped_is_not_wiped(self):
        from deskmind_hands.grounding import STOP
        for how in ("raise", "stop"):
            with self.subTest(how=how):
                self.setUp()
                ex, stub, _ = self._task()
                if how == "stop":
                    self.addCleanup(STOP.clear)
                    orig = ex._step_began

                    def began(bundle, max_s=180.0, orig=orig):
                        token = orig(bundle, max_s)
                        self._stop_key()                   # a key, then the app's stop, as the step gets ready
                        return token
                    ex._step_began = began
                    res = ex.perform({"kind": "click", "window": "com.apple.TextEdit#12", "target": "B2"})
                    self.assertIn("stopped", res.get("why", ""))
                    STOP.clear()
                else:
                    self._doubt_during(ex, stub, how)
                e = ex.epoch
                self.assertGreater(self.w.last_doubt_at, ex._action_end_at)   # noqa: SLF001 -- main's mark not moved
                self.assertEqual(ex.snapshot()["epoch"], e + 1)

    def _stop_key(self):
        """The user's Command key as the step begins, nothing to prove where it went; then the app's stop."""
        from deskmind_hands.grounding import STOP
        self.w.front_pid = lambda: 0
        self.fields[40] = DOCK
        self.key()
        STOP.set()

    def test_slow_reads_never_hide_a_stint_a_person_could_type_in(self):
        """The delta review's generator at f0f7186: every stint at least 50 ms, reads often slow (60-125 ms), each
        reporting one instant within [start, answer]: a key typed while the task's app had the keyboard counts, by the
        truth alone."""
        import random
        from deskmind_hands import inputwatch as iw
        rng = random.Random(7)
        w = self.w
        w._pid_bundle[MUSIC] = TE
        misses = []
        for trial in range(1500):
            w._front_reads.clear(); w._steps = []; iw.FLASHES.clear()
            w.last_by_bundle.clear(); w.last_unknown_at = w.last_doubt_at = w.last_own_at = 0.0
            w._front_backoff_until = 0.0
            now_m = time.monotonic()
            base = now_m - 4.0
            segs, t = [], base
            while t < now_m + 1.0:
                d = rng.choice([0.05, 0.08, 0.12, 0.2, 0.6])
                segs.append((t, t + d, rng.choice([ITERM, ITERM, MUSIC])))
                t += d

            def truth(x, segs=segs):
                return next((p for a, b, p in segs if a <= x < b), segs[-1][2])
            r = base
            while r < now_m + 0.8:
                dur = rng.choice([0.0003] * 15 + [0.06, 0.09, 0.11, 0.125])
                w.front_seen(truth(r + rng.random() * dur), r, r + dur)
                r += dur + 0.02
            ev = base + 0.5 + rng.random() * 3.0
            t0 = ev + rng.choice([0.0005, 0.003, 0.03])
            w.front_pid = lambda t0=t0, truth=truth: truth(t0)
            self.fields[40] = DOCK
            w.note(DOCK, key=True, age=t0 - ev, t0=t0)
            if truth(ev) == MUSIC and not w.last_input_for({TE.lower()}) > 0:
                misses.append((trial, round(ev - base, 3)))
        self.assertEqual(misses, [])
