import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from deskmind_hands import lessons
from deskmind_hands.lessons import Lesson

ORDER = Lesson("When a goal asks for a table's rows in the table's order, write them top to bottom as the table "
               "lists them, and compare the file with the table before saving.", keywords=["order", "顺序"])
SAVE_AS = Lesson("Never type document content into a Save dialog's file-name field.", apps=["com.apple.TextEdit"])
MUSIC = Lesson("To play a song from search results, double-click its row; 播放 buttons on the bar pause instead.",
               keywords=["play", "song", "播放"])


class Recall(unittest.TestCase):
    def test_a_goal_recalls_the_lesson_it_resembles(self):
        goal = ("Copy all rows of the parts table in Safari into parts.csv, keeping the table's order, then save it.")
        got = [l for l, _ in lessons.recall(goal, {"com.apple.Safari"}, [ORDER, MUSIC])]
        self.assertEqual(got, [ORDER])

    def test_a_lesson_is_recalled_only_when_one_of_its_keywords_is_in_the_goal(self):
        goal = "Append the table's four rows to parts.csv sorted by Qty from high to low, then save it."
        self.assertEqual(lessons.recall(goal, set(), [ORDER]), [])

    def test_han_goals_match_by_character_pairs(self):
        got = [l for l, _ in lessons.recall("在网易云音乐里搜索并播放一首歌", set(), [ORDER, MUSIC])]
        self.assertEqual(got, [MUSIC])

    def test_a_lesson_for_other_apps_is_not_recalled(self):
        goal = "Type the rows into the document and save it with the file-name field set to notes"
        self.assertEqual(lessons.recall(goal, {"com.apple.Safari"}, [SAVE_AS]), [])
        self.assertEqual([l for l, _ in lessons.recall(goal, {"com.apple.TextEdit"}, [SAVE_AS])], [SAVE_AS])

    def test_an_unrelated_goal_recalls_nothing(self):
        self.assertEqual(lessons.recall("Turn on Do Not Disturb", set(), [ORDER, SAVE_AS, MUSIC]), [])

    def test_a_pinned_lesson_is_shown_whatever_the_goal(self):
        pin = Lesson("Close any dialog you did not open before acting.", pinned=True)
        got = [l for l, _ in lessons.recall("Turn on Do Not Disturb", set(), [ORDER, pin])]
        self.assertEqual(got, [pin])

    def test_retired_lessons_are_not_recalled(self):
        old = Lesson(ORDER.text, keywords=ORDER.keywords, status="retired")
        self.assertEqual(lessons.recall("copy the table rows in order", set(), [old]), [])


class Store(unittest.TestCase):
    def test_saved_lessons_load_back_and_a_broken_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            lessons.save(ORDER, Path(d))
            (Path(d) / "broken.json").write_text("{", encoding="utf-8")
            got = lessons.load(Path(d))
            self.assertEqual([(l.id, l.text, l.keywords) for l in got], [(ORDER.id, ORDER.text, ORDER.keywords)])

    def test_the_id_is_the_text(self):
        self.assertEqual(Lesson("a  b").id, Lesson("a b").id)


class InTheState(unittest.TestCase):
    def ctx(self, goal):
        task = SimpleNamespace(goal=goal, app="com.apple.Safari", vars={"apps": {"TextEdit": "com.apple.TextEdit"}})
        return SimpleNamespace(task=task, observation=SimpleNamespace(focused_app="com.apple.Safari"))

    def test_the_adapter_recalls_once_per_goal_from_the_store(self):
        from deskmind_hands.adapters.systemone import SystemOneAdapter
        with tempfile.TemporaryDirectory() as d:
            lessons.save(ORDER, Path(d))
            lessons.save(SAVE_AS, Path(d))
            a = SystemOneAdapter.__new__(SystemOneAdapter)
            old = os.environ.get("HANDS_LESSONS_DIR")
            os.environ["HANDS_LESSONS_DIR"] = d
            try:
                got = a._lessons(self.ctx("Append the table's rows in order to parts.csv, then save it"))
                self.assertEqual(set(got), {ORDER.text})
                lessons.save(MUSIC, Path(d))  # recalled once: a lesson added mid-run does not change the run
                self.assertEqual(set(a._lessons(self.ctx("Append the table's rows in order to parts.csv, then save it"))),
                                 {ORDER.text})
            finally:
                if old is None:
                    os.environ.pop("HANDS_LESSONS_DIR", None)
                else:
                    os.environ["HANDS_LESSONS_DIR"] = old

    def test_off_by_default(self):
        old = os.environ.pop("HANDS_LESSONS", None)
        try:
            self.assertFalse(lessons.enabled())
        finally:
            if old is not None:
                os.environ["HANDS_LESSONS"] = old


if __name__ == "__main__":
    unittest.main()
