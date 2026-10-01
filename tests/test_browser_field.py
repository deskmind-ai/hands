"""A browser's own field (its address bar) is not typed into for a goal that asks for nothing on the web: what is typed
there goes to a website (G18b typed a table row into Safari's, 09-30)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.drivers import axchrome   # noqa: E402
from deskmind_hands.drivers.peekaboo import PeekabooDriver   # noqa: E402
from deskmind_hands.geometry import Point, Rect   # noqa: E402

BAR = (300.0, 40.0, 400.0, 30.0)            # the address bar, on screen


def driver(goal):
    d = PeekabooDriver.__new__(PeekabooDriver)
    d.goal, d.app, d._origin = goal, "com.apple.Safari", Point(0, 0)
    d._rects = {"bar": Rect(300, 40, 400, 30), "field": Rect(300, 400, 400, 30)}
    d._labels_by_id = {"bar": "智能搜索栏", "field": "Merchant"}
    d._pid_of = lambda bundle, all_wins=None: 123
    return d


class Refusal(unittest.TestCase):
    def setUp(self):
        self.p = mock.patch.object(axchrome, "fields_outside_page", lambda pid: [BAR])
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_the_address_bar_for_a_goal_about_a_file(self):
        why = driver("Copy the table's rows into parts.csv and save it.")._browser_field_refusal("bar")
        self.assertIn("address bar", why)

    def test_the_page_is_typed_into(self):
        self.assertIsNone(driver("Fill in the expense report and submit.")._browser_field_refusal("field"))

    def test_a_goal_about_the_web_may_use_it(self):
        for goal in ("Open https://example.com and read the title", "在浏览器里打开百度搜一下天气",
                     "Look up the weather online", "search the web for the price"):
            self.assertIsNone(driver(goal)._browser_field_refusal("bar"), goal)

    def test_unreadable_structure_is_no_refusal(self):
        with mock.patch.object(axchrome, "fields_outside_page", side_effect=RuntimeError("AX")):
            self.assertIsNone(driver("Copy the rows into parts.csv")._browser_field_refusal("bar"))

    def test_refused_before_the_projected_route(self):
        # 10-01, in the app: an append into the address bar went through the projected route (pasted, Return) while
        # the check sat after it.
        from deskmind_hands.actions import Action, ActionKind, Binding
        d = driver("Open parts.csv, append the table's rows to it, then save and close it.")
        d._snapshot, d._mcp = "snap", object()
        d._execute_projected = mock.Mock(return_value=None)
        act = Action(kind=ActionKind.TYPE_TEXT, text="PN-5581,Precision resistor,3400",
                     binding=Binding(observation_id="o", app="Safari", element_id="bar"))
        res = d._execute_inner(act)
        self.assertFalse(res.ok)
        self.assertIn("address bar", res.detail)
        d._execute_projected.assert_not_called()

    def test_point_in_frame(self):
        self.assertTrue(axchrome.at([BAR], 500, 55))
        self.assertFalse(axchrome.at([BAR], 500, 415))


if __name__ == "__main__":
    unittest.main()
