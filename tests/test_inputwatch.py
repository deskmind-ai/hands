"""Where the user's input goes: input to DeskMind is the user answering DeskMind, not changing their Mac."""
from __future__ import annotations

import unittest

from deskmind_hands.inputwatch import InputWatch, target_of_point

CARD = {"kCGWindowOwnerPID": 11, "kCGWindowLayer": 3, "kCGWindowAlpha": 1.0, "kCGWindowBounds": {"X": 900, "Y": 600, "Width": 360, "Height": 200}}
GLASS = {"kCGWindowOwnerPID": 99, "kCGWindowLayer": 0, "kCGWindowAlpha": 0.0, "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 3000, "Height": 2000}}
TEXTEDIT = {"kCGWindowOwnerPID": 22, "kCGWindowLayer": 0, "kCGWindowAlpha": 1.0, "kCGWindowBounds": {"X": 200, "Y": 100, "Width": 1000, "Height": 800}}


class Targets(unittest.TestCase):
    def test_the_frontmost_window_under_the_pointer_owns_the_click(self):
        wins = [GLASS, CARD, TEXTEDIT]                       # front to back, as the window server lists them
        self.assertEqual(target_of_point(1000, 700, wins), 11)   # on the card, which floats above the document
        self.assertEqual(target_of_point(300, 300, wins), 22)    # on the document
        self.assertIsNone(target_of_point(2500, 1900, wins))     # on nothing (the glass cannot be hit)

    def test_input_to_deskmind_is_not_the_users_change(self):
        w = InputWatch()
        w._pid_bundle = {11: "ai.deskmind.app", 12: "ai.deskmind.hands", 22: "com.apple.TextEdit"}
        w.note(11, at=100.0)
        w.note(12, at=101.0)
        self.assertEqual(w.last_external_at, 0.0)          # the user answered DeskMind's card: no change
        self.assertEqual(w.last_own_at, 101.0)
        w.note(22, at=102.0)
        self.assertEqual(w.last_external_at, 102.0)        # the user did something in TextEdit
        w.note(None, at=103.0)
        self.assertEqual(w.last_external_at, 103.0)        # unknown where it went: it counts

    def test_the_executor_uses_the_watch_when_it_runs_and_the_idle_time_otherwise(self):
        from deskmind_hands.executor import HandsExecutor
        ex = HandsExecutor.__new__(HandsExecutor)
        ex._watch_tried, ex._watch = True, None
        w = InputWatch()
        w.active, w.keys, w.last_external_at = True, True, 123.0
        ex._watch = w
        self.assertEqual(ex._last_input_at(), 123.0)
        w.keys = False                                      # a mouse-only tap: key presses count by the idle time too
        self.assertGreaterEqual(ex._last_input_at(), 123.0)
        w.active = False                                    # the tap died: back to the idle time, every input counts
        self.assertNotEqual(ex._last_input_at(), 123.0)


if __name__ == "__main__":
    unittest.main()
