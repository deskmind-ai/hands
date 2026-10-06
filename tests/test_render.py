"""The render stage (deskmind#58 step 1): the state is a function of the observation and the run's memory. Asking
for it changes nothing, and the same inputs give the same state; what the run learns from a turn goes into the
memory before the state is built, and what this look showed after it."""
from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from deskmind_hands.adapters.base import Turn, TurnContext   # noqa: E402
from deskmind_hands.pipeline import render                    # noqa: E402


def context(history=()):
    from deskmind_bench.task import load_task
    from deskmind_hands.drivers.mock import MockDriver
    from deskmind_hands.env.workspace import Workspace
    task = load_task(REPO / "tasks" / "smoke" / "S01-rename.yaml")
    ws = Workspace.create(Path(tempfile.mkdtemp()), REPO / "fixtures")
    ws.reset(task.fixture)
    d = MockDriver(render=False)
    d.start(ws.ws)
    obs = d.observe()
    return TurnContext(task=task, observation=obs, history=list(history), channels=frozenset({"ax"}),
                       dialogue=[("which one?", "draft.txt")], notice="look again")


class Render(unittest.TestCase):
    def test_asking_changes_nothing_and_gives_the_same_state(self):
        ctx = context()
        mem = render.Memory()
        render.remember(mem, ctx)
        before = copy.deepcopy(mem)
        first = render.render(ctx, mem, prior=[], lessons=None, progress=None, done_note=None)
        again = render.render(ctx, mem, prior=[], lessons=None, progress=None, done_note=None)
        self.assertEqual(first, again)
        self.assertEqual(mem, before, "render does not write the memory")
        self.assertEqual(first["user_answers"], [{"question": "which one?", "answer": "draft.txt"}])
        self.assertIn("look again", first["note"])
        self.assertTrue(first["elements"], "the window's elements are offered")

    def test_a_verified_effect_is_remembered_once_and_shown(self):
        moved = Turn({"kind": "click"}, True, "moved 'a.txt' to 'keep'")
        mem = render.Memory()
        for _ in range(2):
            render.remember(mem, context([moved]))
        self.assertEqual(mem.effects, ["moved 'a.txt' to 'keep' ✓"], "done again is one fact, not two")
        self.assertEqual(mem.moved, {"a.txt"})
        state = render.render(context([moved]), mem, prior=[], lessons=None, progress=None, done_note=None)
        self.assertEqual(state["effects_so_far"], ["moved 'a.txt' to 'keep' ✓"])

    def test_what_was_shown_is_remembered_after(self):
        ctx = context()
        mem = render.Memory()
        render.remember(mem, ctx)
        self.assertIsNone(mem.last_seen, "the look is remembered after the state, which compares with the last one")
        render.render(ctx, mem, prior=[], lessons=None, progress=None, done_note=None)
        render.remember_shown(mem, ctx)
        self.assertEqual(mem.last_seen[0], (ctx.observation.focused_app, ctx.observation.window_title))
        self.assertTrue(mem.apps)

    def test_the_adapter_renders_through_the_stage(self):
        from deskmind_hands.adapters.systemone import SystemOneAdapter
        a = SystemOneAdapter(url="http://127.0.0.1:9")
        ctx = context()
        state = a._state_raw(ctx)
        mem = render.Memory()
        render.remember(mem, ctx)
        self.assertEqual(state, render.render(ctx, mem, prior=[], lessons=None, progress=None, done_note=None))
        self.assertIs(a._mem.labels, a._labels, "the adapter's names for the memory are the memory")


if __name__ == "__main__":
    unittest.main()
