"""Where the user's input went: input to DeskMind is the user answering DeskMind, not changing their Mac."""
from __future__ import annotations

import unittest
from unittest import mock

from deskmind_hands.inputwatch import InputWatch


class Targets(unittest.TestCase):
    def test_input_the_window_server_routed_to_deskmind_is_not_the_users_change(self):
        w = InputWatch()
        w._pid_bundle = {11: "ai.deskmind.app", 12: "ai.deskmind.hands", 22: "com.apple.TextEdit"}
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
                  "kCGEventKeyDown": 10, "kCGEventFlagsChanged": 12, "kCGAnnotatedSessionEventTap": 2, "kCGHeadInsertEventTap": 0,
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


if __name__ == "__main__":
    unittest.main()
