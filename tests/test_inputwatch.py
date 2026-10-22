"""Where the user's input went: input to DeskMind is the user answering DeskMind, not changing their Mac."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from deskmind_hands.inputwatch import InputWatch


class Targets(unittest.TestCase):
    def test_input_the_window_server_routed_to_deskmind_is_not_the_users_change(self):
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
                 "kCGEventTargetUnixProcessID": 40, "kCGEventSourceUnixProcessID": 41}.items():
        setattr(q, k, v)
    q.CGEventGetIntegerValueField = lambda e, f: fields.get(f, 0)
    return q


class KeysGoToTheAppInFront(unittest.TestCase):
    """A key event can reach the tap with no target, or with one that is no ordinary app (a Command-key press comes
    with the Dock as its target); keyboard input goes to the key window of the app in front, so it is input to the
    app that had the keyboard when it arrived (rc.13, 10-22: the owner working in iTerm2 while a task worked in
    NetEase Music came out as input to an unknown target and staled 4 steps of run_fd8ab281)."""

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

    def key(self):
        self.w._callback(None, 10, object(), None)

    def test_keys_with_no_target_while_another_app_is_in_front_do_not_stale_the_task(self):
        self.front["pid"] = ITERM                          # typing in the terminal
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
                self.w.last_unknown_at = 0.0
                self.key()
                self.assertGreater(self.w.last_unknown_at, 0.0)
                self.assertEqual(self.w.last_input_where({NE})[1], "unknown")
                self.assertGreater(self.ex._last_input_at(), 0.0)   # noqa: SLF001
        # the app with the keyboard is itself no ordinary app (Spotlight's field, say): unknown, it counts
        self.front["pid"] = DOCK
        self.w.front_pid = lambda: self.front["pid"]
        self.w.last_unknown_at = 0.0
        self.fields[40] = 0
        self.key()
        self.assertGreater(self.w.last_unknown_at, 0.0)

    def test_input_to_deskmind_stays_excluded(self):
        self.front["pid"] = DESKMIND                       # the user answering DeskMind's card from the keyboard
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
        _t.sleep(0.01)
        self.front["pid"] = ITERM
        self.key()
        self.assertEqual(ex.snapshot()["epoch"], e0)       # typing in the terminal: no change
        _t.sleep(0.01)
        self.front["pid"] = MUSIC
        self.key()
        s = ex.snapshot()
        self.assertEqual(s["epoch"], e0 + 1)
        self.assertEqual((s["epoch_reason"]["by"], s["epoch_reason"]["to"]), ("input", TE.lower()))

if __name__ == "__main__":
    unittest.main()
