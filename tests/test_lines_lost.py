"""Replacing a document's text is refused when the goal only adds and the new text drops lines it has: with ledger.csv
holding a header and one row, G18b answered "add Lisa Wong's order" with the header and the new row, and Mark Chen's
row was gone (10-01, app run)."""
import unittest
from pathlib import Path

from deskmind_hands.actions import Action, ActionKind, Binding
from deskmind_hands.drivers.peekaboo import PeekabooDriver, lines_lost

GOAL = "Find Lisa Wong's order in records.txt and add it to ledger.csv as `Date,Customer,Order,Amount`, then save it."
HAVE = "Date,Customer,Order,Amount\n2026-09-02,Mark Chen,R-1180,560\n"


class LinesLost(unittest.TestCase):
    def test_a_row_the_goal_did_not_ask_to_remove(self):
        self.assertEqual(lines_lost(GOAL, HAVE, "Date,Customer,Order,Amount\n2026-09-27,Lisa Wong,R-3307,96\n"),
                         ["2026-09-02,Mark Chen,R-1180,560"])
        self.assertEqual(lines_lost("把李娜的订单追加进 ledger.csv", "客户,单号\n张伟,R-1\n", "客户,单号\n李娜,R-2\n"),
                         ["张伟,R-1"])

    def test_kept_rows_changes_and_one_liners_pass(self):
        self.assertEqual(lines_lost(GOAL, HAVE, HAVE + "2026-09-27,Lisa Wong,R-3307,96\n"), [])
        self.assertEqual(lines_lost("Change Mark Chen's amount to 600 in ledger.csv", HAVE, HAVE.replace("560", "600")),
                         [])
        self.assertEqual(lines_lost("把状态改成 approved", "a\nstatus: draft\n", "a\nstatus: approved\n"), [])
        self.assertEqual(lines_lost(GOAL, "Date,Customer,Order,Amount\n", "Date,Customer,Order,Amount\nx\n"), [])
        self.assertEqual(lines_lost("Open the music app", HAVE, "x"), [])

    def test_the_driver_refuses_it_before_writing(self):
        d = PeekabooDriver.__new__(PeekabooDriver)
        d.goal, d._element_value = GOAL, {"e2": HAVE}
        act = Action(kind=ActionKind.TYPE_TEXT, text="Date,Customer,Order,Amount\n2026-09-27,Lisa Wong,R-3307,96\n",
                     clear_first=True, binding=Binding(observation_id="o", app="TextEdit", element_id="e2"))
        why = d._lines_lost_refusal(act)
        self.assertIn("Mark Chen", why)
        self.assertTrue(why.startswith("not written: the goal only adds -- append instead"))   # within 120 chars
        act.clear_first = False
        self.assertIsNone(d._lines_lost_refusal(act))



class OnlyWhatWasThere(unittest.TestCase):
    """10-02: protecting the run's own lines too, G04 had its rewrite of the text it had just typed refused, was left
    with appending only, and appended the same text eleven times. Only lines the document had at the run's first look
    are the user's."""

    def driver(self, first_seen: str, now: str):
        from types import SimpleNamespace
        d = PeekabooDriver.__new__(PeekabooDriver)
        d.goal, d._window_title = "在 report.txt 里写入两行：一季度营收 1,240 万元。未结订单 37 笔。", "report.txt"
        d._keep_baseline([SimpleNamespace(settable=True, value=first_seen, ax_role="AXTextArea")])
        d._element_value = {"e2": now}
        return d

    def act(self, text):
        return Action(kind=ActionKind.TYPE_TEXT, text=text, clear_first=True,
                      binding=Binding(observation_id="o", app="TextEdit", element_id="e2"))

    def test_the_runs_own_lines_may_be_rewritten(self):
        d = self.driver("", "一季度营收 1,240 万元。\n未结订单 37 笔。\n")
        self.assertIsNone(d._lines_lost_refusal(self.act("一季度营收 1,240 万元。\n未结订单 37 笔，均已发货。\n")))

    def test_lines_that_were_there_are_kept(self):
        d = self.driver("备忘\n周五给供应商打电话\n", "备忘\n周五给供应商打电话\n一季度营收 1,240 万元。\n")
        why = d._lines_lost_refusal(self.act("一季度营收 1,240 万元。\n未结订单 37 笔。\n"))
        self.assertIn("remove 2 line(s)", why)


if __name__ == "__main__":
    unittest.main()
