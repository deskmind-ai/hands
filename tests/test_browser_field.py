"""A browser's own field (its address bar) is told apart by structure, and is typed into only when the run allowed it:
what is typed there goes to a website (G18b typed a table row into Safari's, 09-30). Whether it may is a model's
judgement in the loop (test_judge.py), not a keyword list over the goal (deskmind#73)."""
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
        self.q = mock.patch.object(axchrome, "address_bars", lambda pid: [])     # tests that need one patch it
        self.p.start()
        self.q.start()

    def tearDown(self):
        self.q.stop()
        self.p.stop()

    def test_the_address_bar_is_told_apart(self):
        self.assertEqual(driver("打开 Safari，搜索新加坡的天气").browser_field("bar"), "智能搜索栏")
        self.assertIsNone(driver("打开 Safari，搜索新加坡的天气").browser_field("field"))

    def test_refused_unless_allowed(self):
        why = driver("Copy the table's rows into parts.csv and save it.")._browser_field_refusal("bar")
        self.assertIn("address bar", why)

    def test_no_goal_words_open_it(self):
        # deskmind#73: the goal's words decide nothing here any more; only the run's judgement does.
        for goal in ("Open https://example.com and read the title", "打开 Safari，搜索新加坡的天气"):
            self.assertIsNotNone(driver(goal)._browser_field_refusal("bar"), goal)

    def test_an_allowance_does_not_open_the_address_bar(self):
        # Only 「在地址栏搜索」 types into the address bar: a judged 照做 on the raw bar is still refused, the plain
        # routes check neither the window nor the focus (review of 27b84b8).
        d = driver("打开 Safari，搜索新加坡的天气")
        with mock.patch.object(axchrome, "address_bars", lambda pid: [BAR]):
            d.allow_browser_field("bar")
            self.assertIsNotNone(d._browser_field_refusal("bar"))
            self.assertIn("在地址栏搜索", d.browser_field_refused("bar"))

    def test_another_browser_field_is_typed_into_once_allowed(self):
        # Safari's 在页面上查找 is the browser's own field, not the address bar: judged, it is typed into, once
        # (review of bd79705: refusing it left "Find 'invoice' on this web page" with no way to do it).
        d = driver("Find 'invoice' on this web page")
        with mock.patch.object(axchrome, "address_bars", lambda pid: []):
            self.assertIsNone(d.browser_field_refused("bar"))
            self.assertIsNotNone(d._browser_field_refusal("bar"))      # not judged: refused
            d.allow_browser_field("bar")
            self.assertIsNone(d._browser_field_refusal("bar"))         # judged: typed
            self.assertIsNotNone(d._browser_field_refusal("bar"))      # for one step

    def test_what_the_judgement_is_told_the_field_is(self):
        # 「another field」 only beside an address bar known by its identifier; without one (Chrome, Firefox), or
        # unread, the field may be the address bar and is described as one (review of b62652e).
        d = driver("Find 'invoice' on this web page")
        with mock.patch.object(axchrome, "address_bars", lambda pid: [(300, 0, 400, 30)]):
            self.assertFalse(d.browser_field_is_address_bar("bar"))       # the bar is elsewhere: a find bar
        self.assertTrue(d.browser_field_is_address_bar("bar"))            # no identified bar (setUp): maybe it
        with mock.patch.object(axchrome, "address_bars", side_effect=RuntimeError("AX")):
            self.assertTrue(d.browser_field_is_address_bar("bar"))
        with mock.patch.object(axchrome, "address_bars", lambda pid: [BAR]):
            self.assertTrue(d.browser_field_is_address_bar("bar"))        # it is the identified bar

    def test_an_allowance_does_not_outlive_its_step(self):
        # An early return (here: no snapshot yet) or an error in the step must not leave it for the next (review).
        from deskmind_hands.actions import Action, ActionKind, Binding
        d = driver("打开 Safari，搜索新加坡的天气")
        d._mcp, d._snapshot = None, None
        d.allow_browser_field("bar")
        res = d.execute(Action(kind=ActionKind.CLICK, binding=Binding(observation_id="o", app="Safari",
                                                                       element_id="field")))
        self.assertFalse(res.ok)
        self.assertIsNone(getattr(d, "_browser_field_allowed", None))

    def test_page_info_is_the_observed_windows(self):
        d = driver("打开 Safari，搜索新加坡的天气")
        d._window_frame = lambda: (900, 0, 800, 600)
        with mock.patch.object(axchrome, "page_info", lambda pid, frame, wid=None: {"frame": frame}):
            self.assertEqual(d.page_info(), {"frame": (900, 0, 800, 600)})

    def test_the_page_is_typed_into(self):
        self.assertIsNone(driver("Fill in the expense report and submit.")._browser_field_refusal("field"))

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
