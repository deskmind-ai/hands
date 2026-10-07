"""The ask stage (deskmind#58 step 2): the questions for a turn come from the rendered state and what the run keeps,
passed in. Building them changes nothing; the one thing learnt -- the text seen so far in the task -- is handed back
for the caller to keep."""
from __future__ import annotations

import copy
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from test_render import context                 # noqa: E402  -- the S01 window on the mock desktop
from deskmind_hands.pipeline import ask, render  # noqa: E402


def rendered(ctx):
    mem = render.Memory()
    render.remember(mem, ctx)
    return mem, render.render(ctx, mem, prior=[], lessons=None, progress=None, done_note=None)


class Ask(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.startswith("HANDS_")},
                              clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_building_changes_nothing_and_gives_the_same_questions(self):
        ctx = context()
        mem, state = rendered(ctx)
        seen = {}
        before = (copy.deepcopy(mem), copy.deepcopy(state))
        first = ask.build(ctx, state, mem=mem, seen=seen, prior=[], text_helper=None)
        again = ask.build(ctx, state, mem=mem, seen=seen, prior=[], text_helper=None)
        self.assertEqual(first, again)
        self.assertEqual((mem, state), before)
        self.assertEqual(seen, {}, "what was seen is handed back, not kept here")
        self.assertIn("operation", first.questions)
        self.assertIn("CLICK", first.questions["operation"]["criteria"])

    def test_the_text_seen_is_handed_back_once(self):
        ctx = context()
        mem, state = rendered(ctx)
        first = ask.build(ctx, state, mem=mem, seen={}, prior=[], text_helper=None)
        self.assertTrue(first.seen_now)
        again = ask.build(ctx, state, mem=mem, seen={ctx.task.goal: first.seen_now}, prior=[], text_helper=None)
        self.assertIsNone(again.seen_now, "nothing new on screen: nothing to keep")

    def test_ask_is_offered_with_a_text_helper(self):
        """Replays never set a text helper, so the corpus does not reach this (review of #27)."""
        ctx = context()
        mem, state = rendered(ctx)
        without = ask.build(ctx, state, mem=mem, seen={}, prior=[], text_helper=None)
        self.assertNotIn("ASK", without.questions["operation"]["criteria"], "S01's goal is not ambiguous")
        helped = ask.build(ctx, state, mem=mem, seen={}, prior=[], text_helper="http://helper.invalid")
        self.assertIn("ASK", helped.questions["operation"]["criteria"])
        ctx.asked = 1
        once = ask.build(ctx, state, mem=mem, seen={}, prior=[], text_helper="http://helper.invalid")
        self.assertNotIn("ASK", once.questions["operation"]["criteria"], "asked once already")

    def test_what_an_earlier_phase_read_comes_first_among_the_values(self):
        """HANDS_PRIOR_READ: lines read before this phase, offered whole and line by line, ahead of the rest. Replays
        never set it either (review of #27)."""
        ctx = context()
        mem, state = rendered(ctx)
        prior = [{"source": "Safari", "lines": ["Order R-3307", "Due 10/12"]}]
        got = ask.build(ctx, state, mem=mem, seen={}, prior=prior, text_helper=None)
        self.assertEqual(got.candidates[:3], ["Order R-3307\nDue 10/12\n", "Order R-3307", "Due 10/12"])
        values = got.questions["type_text_value"]["criteria"]
        self.assertEqual(values["2"], {"value": "Order R-3307"})
        self.assertLessEqual(len(got.candidates), 24)

    def test_the_adapter_asks_what_the_stage_builds(self):
        from deskmind_hands.adapters.systemone import SystemOneAdapter

        class Stop(Exception):
            pass

        class Capture(SystemOneAdapter):
            def _ask(self, state, questions):
                self.captured = (state, questions)
                raise Stop

        a = Capture(url="http://127.0.0.1:9")
        ctx = context()
        with self.assertRaises(Stop):
            a.propose(ctx)
        _, state = rendered(ctx)
        want = ask.build(ctx, a._redact(state), mem=a._mem, seen={}, prior=[], text_helper=None)
        self.assertEqual(a.captured[1], a._redact(want.questions))
        self.assertEqual(a._seen_text, {ctx.task.goal: want.seen_now}, "the adapter keeps what was seen")


if __name__ == "__main__":
    unittest.main()
