"""The check before a run may finish (deskmind_hands/done_check.py): what the goal's own words hold it to, the ledger it
keeps, and the loop sending a DONE back with what is missing."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from deskmind_hands.done_check import Ledger, objection, progress, rows_wanted, satisfied   # noqa: E402

TABLE = ["PN-7713,Ceramic capacitor,1250", "PN-2046,Schottky diode,86", "PN-5581,Precision resistor,3400"]


class Rules(unittest.TestCase):
    def test_unsaved_and_edited_after_saving(self):
        led = Ledger()
        led.wrote("parts.csv", "PN-7713,Ceramic capacitor,1250\n")
        self.assertIn("written but not saved", objection("Append the rows, then save it.", led, [], []))
        led.was_saved("parts.csv")
        self.assertIsNone(objection("Append the rows, then save it.", led, [], []))
        led.wrote("parts.csv", "x")
        self.assertIn("changed after it was saved", objection("… and save it.", led, [], []))
        self.assertIsNone(objection("Append the rows.", led, [], []))                 # the goal never said save

    def test_rows_the_goal_counts_and_the_next_one(self):
        led = Ledger()
        led.wrote("parts.csv", TABLE[0] + "\n")
        why = objection("Copy all of its rows into parts.csv.", led, [], TABLE)
        self.assertIn("1 of the 3 rows", why)
        self.assertIn(TABLE[1], why)                                              # the next one, named
        self.assertIsNone(objection("Copy the rows with Qty under 500 into parts.csv.", led, [], TABLE))  # a condition
        for goal, n in (("append its four rows", 4), ("把表格里四行追加进去", 4), ("copy 2 rows", 2),
                        ("所有行都抄过去", 3), ("copy the rows under 500", None)):
            self.assertEqual(rows_wanted(goal, TABLE), n, goal)

    def test_close_what_was_written(self):
        led = Ledger()
        led.wrote("parts.csv", "x")
        led.was_saved("parts.csv")
        self.assertIn("still open", objection("then save and close it", led, ["parts.csv"], []))
        self.assertIsNone(objection("then save and close it", led, ["records.txt"], []))


class Satisfied(unittest.TestCase):
    """10-01: parts.csv written, saved and closed, and G18b went on to the user's other windows instead of DONE."""
    GOAL = "Open parts.csv, append the table's rows to it, then save and close it."

    def ledger(self, close=True):
        l = Ledger()
        l.wrote("parts.csv", "PN-7713,Ceramic capacitor,1250\n")
        l.was_saved("parts.csv")
        if close:
            l.was_closed("parts.csv")
        return l

    def test_said_once_written_saved_and_closed(self):
        said = satisfied(self.GOAL, self.ledger(), ["notes.txt"], [])
        self.assertEqual(said, "'parts.csv' has been written, saved and closed as the goal asks. "
                               "If the goal asks for nothing else, finish now (DONE).")

    def test_not_while_anything_is_left(self):
        self.assertIsNone(satisfied(self.GOAL, self.ledger(close=False), ["parts.csv"], []))
        self.assertIsNone(satisfied(self.GOAL, Ledger(), [], []))
        unsaved = self.ledger()
        unsaved.wrote("parts.csv", "more\n")
        self.assertIsNone(satisfied(self.GOAL, unsaved, [], []))

    def test_no_claim_when_only_writing_is_checkable(self):
        l = Ledger()
        l.wrote("notes.txt", "hello\n")
        self.assertIsNone(satisfied("Type hello into notes.txt", l, ["notes.txt"], []))


class Progress(unittest.TestCase):
    """The state's progress section (HANDS_PROGRESS): counts and rows left, never an order to write them in."""
    GOAL = "Copy all of its rows into parts.csv, keeping the table's order. Save it when done."

    def test_counts_and_rows_left_in_table_order(self):
        l = Ledger()
        l.wrote("parts.csv", TABLE[1] + "\n")
        p = progress(self.GOAL, l, TABLE)
        self.assertEqual(p["rows"], {"wanted": 3, "written": 1, "not_yet_written": [TABLE[0], TABLE[2]]})
        self.assertEqual(p["documents"], {"parts.csv": {"written": True, "saved": False, "closed": False}})
        l.was_saved("parts.csv")
        l.was_closed("parts.csv")
        self.assertEqual(progress(self.GOAL, l, TABLE)["documents"]["parts.csv"],
                         {"written": True, "saved": True, "closed": True})

    def test_nothing_to_count(self):
        self.assertIsNone(progress("Turn on Do Not Disturb", Ledger(), []))

    def test_off_by_default(self):
        from deskmind_hands.done_check import progress_enabled
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HANDS_PROGRESS", None)
            self.assertFalse(progress_enabled())


class AdapterLedger(unittest.TestCase):
    def test_writes_that_landed_and_saves(self):
        from deskmind_hands.adapters.base import Turn, TurnContext
        from deskmind_hands.adapters.systemone import SystemOneAdapter
        from deskmind_hands.drivers.base import Observation
        from deskmind_hands.geometry import ImageTransform, ScreenGeometry, Size
        from deskmind_hands.live import live_task
        s = Size(900, 700)
        o = Observation(id="o", geometry=ScreenGeometry(s, s), transform=ImageTransform.identity(s),
                        focused_app="TextEdit", window_title="parts.csv")
        task = live_task("Append the rows, then save it.", None, app="com.apple.TextEdit", max_actions=5,
                         wall_clock_s=60)
        a = SystemOneAdapter()
        a._pending_write = ("parts.csv", "row\n")
        ctx = lambda h: TurnContext(task=task, observation=o, history=h, channels=frozenset({"ax"}))
        wrote = Turn({"kind": "type_text"}, True, "set 5 chars, verified")
        self.assertIn("not saved", a.done_objection(ctx([wrote])))
        saved = Turn({"kind": "click"}, True, "saved 'parts.csv' (scripted, in the background)")
        self.assertIsNone(a.done_objection(ctx([wrote, saved])))


class StopsOnce(unittest.TestCase):
    """The loop sends a DONE back with the objection as the notice, and accepts the next one it has no objection to."""

    def run_s01(self, enabled: bool):
        from deskmind_hands.adapters.scripted import OracleAdapter
        from deskmind_hands.drivers.mock import MockDriver
        from deskmind_hands.env.workspace import Workspace
        from deskmind_hands.runtime.loop import RunConfig, run_task
        from deskmind_bench.task import load_task
        task = load_task(REPO / "tasks" / "smoke" / "S01-rename.yaml")
        ws = Workspace.create(Path(tempfile.mkdtemp()), REPO / "fixtures")

        class Objecting(OracleAdapter):
            asked, notices = 0, []

            def propose(self, ctx):
                self.notices.append(ctx.notice)
                return super().propose(ctx)

            def done_objection(self, ctx):
                self.asked += 1
                return "parts.csv was written but not saved: save it before finishing." if self.asked == 1 else None
        a = Objecting()
        with mock.patch.dict(os.environ, {"HANDS_DONE_CHECK": "1" if enabled else "0"}):
            return run_task(task, MockDriver(render=False), a, ws, config=RunConfig()), a

    def test_sent_back_once_then_finished(self):
        res, a = self.run_s01(True)
        self.assertEqual(res.state.value, "completed")
        self.assertEqual([s["kind"] for s in res.steps if s.get("kind") == "done_blocked"], ["done_blocked"])
        self.assertTrue(any(n and n.startswith("Not finished yet: parts.csv") for n in a.notices))

    def test_off_by_default(self):
        res, a = self.run_s01(False)
        self.assertEqual(a.asked, 0)
        self.assertFalse(any(s.get("kind") == "done_blocked" for s in res.steps))



class OnRecordedRuns(unittest.TestCase):
    """The check against runs G18b really made (tests/replay): it objects where they finished short, with what is
    missing, and not where they finished the job. A table row typed into Safari's address bar is not a document.

    The D1 and D5 runs were recorded in the user's own Safari, whose tab bar is in every observation: they stay on
    that Mac, in HANDS_REPLAY_PRIVATE, and the tests on them are skipped where it is not set."""

    def objections(self, case):
        import deskmind_hands.done_check as dc
        from deskmind_hands.replay import replay
        where = [REPO / "tests" / "replay" / case]
        if os.environ.get("HANDS_REPLAY_PRIVATE"):
            where.append(Path(os.environ["HANDS_REPLAY_PRIVATE"]).expanduser() / case)
        found = next((p for p in where if (p / "trace.jsonl").exists()), None)
        if found is None:
            self.skipTest(f"{case} is kept off the repository (HANDS_REPLAY_PRIVATE)")
        seen, real = [], dc.objection
        dc.objection = lambda *a: seen.append(real(*a)) or seen[-1]
        try:
            with mock.patch.dict(os.environ, {"HANDS_DONE_CHECK": "1"}):
                replay(found)
        finally:
            dc.objection = real
        return seen

    def test_short_runs_are_sent_back_with_what_is_missing(self):
        self.assertIn("3 of the 6 rows", self.objections("d1-en-6rows-fail")[0])
        why = self.objections("d1-en-open-from-folder")[0]
        self.assertIn("still open", why)                     # told to save and close; closed nothing
        self.assertNotIn("Parts inventory", why)             # the address bar it typed into is not a document

    def test_finished_runs_are_let_finish(self):
        for case in ("d4-en-preopened-ask", "d4-en-open-ask"):
            self.assertEqual(self.objections(case), [None], case)

    def test_finished_runs_in_the_users_safari_are_let_finish(self):
        for case in ("d1-zh-preopened", "d1-en-preopened"):
            self.assertEqual(self.objections(case), [None], case)



class NothingWritten(unittest.TestCase):
    """10-01: the whole-text write refused twice (it would have dropped a row), G18b typed the row into the Save
    dialog's name field and said DONE with ledger.csv untouched."""
    GOAL = "Find Lisa Wong's order in records.txt and add it to ledger.csv, then save ledger.csv."

    def test_a_goal_that_adds_to_a_file_not_written(self):
        why = objection(self.GOAL, Ledger(), ["ledger.csv"], [])
        self.assertIn("nothing has been written to 'ledger.csv'", why)
        l = Ledger()
        l.wrote("ledger.csv", "2026-09-27,Lisa Wong,R-3307,96\n")
        l.was_saved("ledger.csv")
        self.assertIsNone(objection(self.GOAL, l, [], []))

    def test_not_for_goals_that_change_or_name_no_destination(self):
        self.assertIsNone(objection("Change Mark Chen's amount to 600 in ledger.csv and save it.", Ledger(), [], []))
        self.assertIsNone(objection("Play a song in the music app", Ledger(), [], []))


if __name__ == "__main__":
    unittest.main()
