"""The action-result effect contract, the focus guard and the new-window note (cua review, item 9)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.adapters.base import Turn   # noqa: E402
from deskmind_hands.drivers.base import ExecResult, classify_effect   # noqa: E402


class Effects(unittest.TestCase):
    def test_classification(self):
        cases = [
            (ExecResult(True, "set 12 chars in the background, verified"), "confirmed"),
            (ExecResult(True, "chose 'Meals' (verified) in 'Category'"), "confirmed"),
            (ExecResult(True, "dispatched but the outcome could not be confirmed; ...", indeterminate=True),
             "unverifiable"),
            (ExecResult(True, "entered 'x' into 'Merchant' (in the background, Safari not brought forward, 1.2s)"),
             "unverifiable"),
            (ExecResult(False, "clicked 'Play' and nothing on screen changed"), "suspected_noop"),
            (ExecResult(False, "Click needs an element id"), "refused"),
            (ExecResult(False, "stale", stale=True), "refused"),
            (ExecResult(True, "clicked 'Save'"), "confirmed"),
        ]
        for res, want in cases:
            self.assertEqual(classify_effect(res)[0], want, res.detail)
        # A driver that says it knows keeps its word.
        self.assertEqual(classify_effect(ExecResult(True, "x", effect="partial", evidence="2 of 3")), ("partial", "2 of 3"))


class LastEffect(unittest.TestCase):
    def test_last_changing_action_counts(self):
        from deskmind_hands.adapters.systemone import last_effect
        h = [Turn({"kind": "double_click"}, True, "clicked", effect="unverifiable"),
             Turn({"kind": "focus_window"}, True, "now observing", effect="confirmed")]
        self.assertEqual(last_effect(h), "unverifiable")   # a switch of window does not settle it
        h.append(Turn({"kind": "click"}, True, "clicked", effect="confirmed"))
        self.assertEqual(last_effect(h), "confirmed")
        self.assertEqual(last_effect([]), "")

    def test_the_planner_sees_it_when_switched_on(self):
        from unittest import mock
        from deskmind_hands.adapters.systemone import SystemOneAdapter
        a = SystemOneAdapter.__new__(SystemOneAdapter)
        a._labels = {}
        turn = Turn({"kind": "click", "binding": {"element_id": "e1"}}, True, "clicked", effect="suspected_noop")
        # Off by default: G14-G18b were trained without it, and their re-tests get the state they know.
        with mock.patch.dict("os.environ", {"HANDS_EFFECT_NOTES": "0"}):
            self.assertNotIn("effect", a._action_record(turn))
        with mock.patch.dict("os.environ", {"HANDS_EFFECT_NOTES": "1"}):
            rec = a._action_record(turn)
        self.assertEqual(rec.get("effect"), "suspected_noop")
        rec = a._action_record(Turn({"kind": "click"}, True, "clicked", effect="confirmed"))
        self.assertNotIn("effect", rec)   # confirmed: as before


class NewWindowNote(unittest.TestCase):
    def test_window_sheet_and_nothing(self):
        from deskmind_hands.drivers.peekaboo import new_window_note
        main = {"id": 1, "title": "Expense report", "bounds": {"X": 100, "Y": 50, "Width": 900, "Height": 700}}
        self.assertIsNone(new_window_note([main], [main]))
        sheet = {"id": 2, "title": "", "bounds": {"X": 300, "Y": 80, "Width": 400, "Height": 200}}
        self.assertEqual(new_window_note([main], [main, sheet]), "a sheet appeared")
        win = {"id": 3, "title": "Receipt 4471", "bounds": {"X": 1200, "Y": 50, "Width": 600, "Height": 800}}
        self.assertEqual(new_window_note([main], [main, win]), "opened a new window 'Receipt 4471'")


class FocusGuard(unittest.TestCase):
    def _driver(self, fronts, idle):
        from deskmind_hands.actions import Action, ActionKind
        from deskmind_hands.drivers.peekaboo import PeekabooDriver
        d = PeekabooDriver.__new__(PeekabooDriver)
        d.app, d._mcp, d._foreground_actions = "com.apple.Safari", object(), 0
        seq = iter(fronts)
        d._frontmost = lambda: next(seq)
        d._hid_idle = lambda: idle
        d._task_windows = lambda: []
        import time
        d._execute_inner = lambda action: time.sleep(0.3) or ExecResult(True, "clicked 'Submit'")   # a step takes a while
        d.activated = []
        d._osa = lambda script: d.activated.append(script) or (True, "")
        d._q = lambda s: s
        from deskmind_hands.actions import Binding
        return d, Action(kind=ActionKind.CLICK, binding=Binding(element_id="elem_1"))

    def test_a_stranger_taking_the_front_is_sent_back(self):
        from unittest import mock
        d, act = self._driver(["iTerm2", "Some Updater"], idle=60)
        with mock.patch.dict("os.environ", {"HANDS_EFFECT_NOTES": "1"}):
            res = d._execute_outer(act)
        self.assertEqual(d.activated, ['tell application "iTerm2" to activate'])
        self.assertIn("given back to iTerm2", res.detail)

    def test_the_guard_acts_but_says_nothing_by_default(self):
        from unittest import mock
        d, act = self._driver(["iTerm2", "Some Updater"], idle=60)
        with mock.patch.dict("os.environ", {"HANDS_EFFECT_NOTES": "0"}):
            res = d._execute_outer(act)
        self.assertEqual(d.activated, ['tell application "iTerm2" to activate'])   # the front is still given back
        self.assertNotIn("given back", res.detail)                                 # but the planner's text is as before

    def test_the_user_switching_apps_is_left_alone(self):
        d, act = self._driver(["iTerm2", "Mail"], idle=0.1)   # touched during the step: the user's doing
        res = d._execute_outer(act)
        self.assertEqual(d.activated, [])
        self.assertNotIn("given back", res.detail)

    def test_an_intended_foreground_step_is_left_alone(self):
        from deskmind_hands.actions import Action, ActionKind
        d, _ = self._driver(["iTerm2", "Some Updater"], idle=60)
        from deskmind_hands.actions import Binding
        d._execute_outer(Action(kind=ActionKind.CLICK, foreground=True, binding=Binding(element_id="elem_1")))
        self.assertEqual(d.activated, [])


if __name__ == "__main__":
    unittest.main()
