"""A point taken from an observation is used only while the window is where it was seen: moved or resized in the
seconds the planner took, the click is refused as stale and the loop looks again (cua binds a coordinate click to
its capture the same way)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.actions import Action, ActionKind, Binding   # noqa: E402
from deskmind_hands.drivers.peekaboo import PeekabooDriver   # noqa: E402
from deskmind_hands.geometry import Point, Rect   # noqa: E402


def driver(frames):
    d = PeekabooDriver.__new__(PeekabooDriver)
    seq = iter(frames)
    d._window_frame = lambda: next(seq, None)
    d._seen_frame = d._window_frame()
    return d


class Moved(unittest.TestCase):
    def test_still_there(self):
        self.assertIsNone(driver([(0, 0, 900, 700), (0, 0, 900, 700)])._moved_since_seen())

    def test_moved_or_resized(self):
        self.assertIn("moved or changed size", driver([(0, 0, 900, 700), (40, 0, 900, 700)])._moved_since_seen())
        self.assertIn("observe again", driver([(0, 0, 900, 700), (0, 0, 800, 700)])._moved_since_seen())

    def test_no_answer_is_no_check(self):
        self.assertIsNone(driver([None, (5, 5, 1, 1)])._moved_since_seen())
        self.assertIsNone(driver([(0, 0, 900, 700), None])._moved_since_seen())

    def test_a_vision_click_on_a_moved_window_is_refused_as_stale(self):
        d = driver([(0, 0, 900, 700), (60, 0, 900, 700)])
        d._rects, d._labels_by_id, d._element_value = {"ocr:3": Rect(10, 10, 40, 20)}, {"ocr:3": "Play"}, {}
        d._origin, d._last_geometry = Point(0, 0), None
        d._window_digest = lambda: "d"
        res = d._vision_act(Action(kind=ActionKind.CLICK, binding=Binding(element_id="ocr:3")))
        self.assertFalse(res.ok)
        self.assertTrue(res.stale)


if __name__ == "__main__":
    unittest.main()
