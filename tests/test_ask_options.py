"""A question the harness builds from what is ambiguous carries the alternatives it lists, and they go to the app with
it as answers to pick from: the D4 shoot had the user type "The one from September 27." where a click would do."""
import io
import json
import unittest
from unittest import mock

from deskmind_hands.adapters.systemone import ambiguity, ambiguity_question

RECORDS = """Date,Customer,Order,Amount,Status
2026-09-05,Lisa Wong,R-2291,1340,Paid
2026-09-15,Lisa Wang,R-3012,640,Paid
2026-09-27,Lisa Wong,R-3307,96,Paid"""


class Options(unittest.TestCase):
    def test_the_alternatives_come_with_the_question(self):
        q, opts = ambiguity("Add Lisa Wong's order to ledger.csv and save it.", RECORDS)
        self.assertEqual(opts, ["2026-09-05,Lisa Wong,R-2291,1340,Paid", "2026-09-27,Lisa Wong,R-3307,96,Paid"])
        self.assertEqual(q, ambiguity_question("Add Lisa Wong's order to ledger.csv and save it.", RECORDS))
        self.assertTrue(all(o in q for o in opts))

    def test_nothing_ambiguous(self):
        self.assertIsNone(ambiguity("Add Sara Lopez's order to ledger.csv.", RECORDS))

    def test_the_ask_line_carries_them(self):
        from deskmind_hands.runtime.loop import StdinUser
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("select.select", return_value=([1], [], [])), \
             mock.patch("sys.stdin", io.StringIO('{"reply": "2026-09-27,Lisa Wong,R-3307,96,Paid"}\n')):
            reply, ok, _ = StdinUser().respond("Which one?", False, ("a", "b"))
        line = out.getvalue().strip()
        self.assertTrue(line.startswith("HANDS_ASK "))
        self.assertEqual(json.loads(line[len("HANDS_ASK "):])["options"], ["a", "b"])
        self.assertEqual(reply, "2026-09-27,Lisa Wong,R-3307,96,Paid")

    def test_no_options_no_field(self):
        from deskmind_hands.runtime.loop import StdinUser
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("select.select", return_value=([1], [], [])), \
             mock.patch("sys.stdin", io.StringIO('{"reply": "yes"}\n')):
            StdinUser().respond("Go ahead?", True)
        self.assertNotIn("options", json.loads(out.getvalue().strip()[len("HANDS_ASK "):]))



class ChosenOption(unittest.TestCase):
    """10-01: the picked line handed over raw had G18b rewrite the document a dozen ways; told what sets the pick apart
    ("The one with 2026-09-27, R-3307, 96.") it appended the row and saved."""
    OPTS = ("2026-09-05,Lisa Wong,R-2291,1340,Paid", "2026-09-27,Lisa Wong,R-3307,96,Paid")

    def test_the_fields_that_set_it_apart(self):
        from deskmind_hands.runtime.loop import chosen_option
        self.assertEqual(chosen_option(self.OPTS[1], self.OPTS, "Which one should I use?"),
                         "The one with 2026-09-27, R-3307, 96.")
        self.assertEqual(chosen_option("李娜,R-3307,96", ("李娜,R-2291,1340", "李娜,R-3307,96"), "应该用哪一个？"),
                         "就是 R-3307、96 那一条。")

    def test_a_picked_option_reaches_the_planner_as_that_sentence(self):
        from deskmind_hands.runtime.loop import StdinUser
        with mock.patch("sys.stdout", io.StringIO()), mock.patch("select.select", return_value=([1], [], [])), \
             mock.patch("sys.stdin", io.StringIO(json.dumps({"reply": self.OPTS[1]}) + "\n")):
            reply, _, _ = StdinUser().respond("Which one?", False, self.OPTS)
        self.assertEqual(reply, "The one with 2026-09-27, R-3307, 96.")

    def test_a_typed_answer_is_kept(self):
        from deskmind_hands.runtime.loop import StdinUser
        with mock.patch("sys.stdout", io.StringIO()), mock.patch("select.select", return_value=([1], [], [])), \
             mock.patch("sys.stdin", io.StringIO('{"reply": "The one from September 27."}\n')):
            reply, _, _ = StdinUser().respond("Which one?", False, self.OPTS)
        self.assertEqual(reply, "The one from September 27.")


if __name__ == "__main__":
    unittest.main()
