"""A dynamic task's changes (deskmind#62) fire from the loop: at the start, before the Nth action, when a checkpoint
first passes, when a state holds -- each once, done to the environment and recorded in the run's changes.jsonl. What
the user says reaches the planner as the user's own words. A task without changes runs exactly as before."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.drivers.mock import MockDriver   # noqa: E402

REPO = Path(__file__).resolve().parent.parent


class Seeing:
    """The oracle's steps, and what the planner was shown each turn."""

    def __init__(self):
        from deskmind_hands.adapters.scripted import OracleAdapter
        self.oracle, self.seen = OracleAdapter(), []

    def propose(self, ctx):
        self.seen.append(list(ctx.dialogue))
        return self.oracle.propose(ctx)

    def usage(self):
        return {}


def run(changes, checks=()):
    from deskmind_bench.task import Change, Checkpoint, load_task
    from deskmind_hands.env.workspace import Workspace
    from deskmind_hands.runtime.loop import RunConfig, run_task
    task = load_task(REPO / "tasks" / "smoke" / "S01-rename.yaml")
    task.changes = [Change(id=c, type=t, trigger=trig, effect=eff) for c, t, trig, eff in changes]
    task.checkpoints = task.checkpoints + [Checkpoint(name=n, check=c) for n, c in checks]
    ws = Workspace.create(Path(tempfile.mkdtemp()), REPO / "fixtures")
    adapter = Seeing()
    res = run_task(task, MockDriver(render=False), adapter, ws, config=RunConfig())
    return res, ws, adapter


def fired_at(res) -> dict[str, int]:
    """change id -> how many actions had been taken when it fired."""
    out, acted = {}, 0
    for s in res.steps:
        if "change" in s:
            out[s["change"]] = acted
        elif "action" in s:
            acted += 1
    return out


def record(ws):
    from deskmind_bench.dyn import events as ev
    return ev.read(ws.root / "changes.jsonl")


TOUCH = lambda name: [{"fs": {"op": "touch", "path": f"$WS/{name}"}}]   # noqa: E731


class Triggers(unittest.TestCase):
    def test_at_start_fires_before_the_first_action(self):
        res, ws, _ = run([("c1", "file_moved", {"at_start": True}, TOUCH("early.txt"))])
        self.assertEqual(fired_at(res), {"c1": 0})
        self.assertTrue((ws.ws / "early.txt").exists())
        self.assertEqual([(r["t"], r["change_id"]) for r in record(ws)], [("change_fired", "c1")])

    def test_at_action_fires_before_that_action_is_carried_out(self):
        res, ws, _ = run([("c2", "file_moved", {"at_action": 2}, TOUCH("mid.txt"))])
        self.assertEqual(fired_at(res), {"c2": 2})
        self.assertEqual(len(record(ws)), 1, "fired once")

    def test_at_checkpoint_fires_once_the_checkpoint_passes(self):
        res, ws, _ = run([("c3", "file_moved", {"at_checkpoint": "renamed"}, TOUCH("late.txt"))])
        self.assertEqual(fired_at(res), {"c3": 4}, "right after the rename (the 4th action), before `done`")
        self.assertEqual(res.state.value, "completed", res.failure)

    def test_an_unknown_checkpoint_is_an_error_not_a_silent_skip(self):
        from deskmind_hands.runtime.loop import _change_due
        from deskmind_bench.graders.primitives import GradeContext
        from deskmind_bench.task import Change
        with self.assertRaises(ValueError):
            _change_due(Change(id="x", type="file_moved", trigger={"at_checkpoint": "nope"}),
                        GradeContext(workspace=Path(tempfile.mkdtemp())), 0, {})

    def test_at_state_fires_when_the_state_holds(self):
        res, ws, _ = run([("c4", "file_moved", {"at_state": {"file_absent": {"path": "$WS/draft.txt"}}},
                           TOUCH("gone.txt"))])
        self.assertEqual(fired_at(res), {"c4": 4})

    def test_what_the_user_says_reaches_the_planner_as_theirs(self):
        res, ws, adapter = run([("u1", "user_amend", {"at_action": 1}, [{"user_says": "名字用 final.txt 就行"}])])
        said = [d for d in res.dialogue if d.get("kind") == "user_says"]
        self.assertEqual([(d["reply"], d["by"]) for d in said], [("名字用 final.txt 就行", "user")])
        self.assertNotIn(("（用户主动说）", "名字用 final.txt 就行"), adapter.seen[1])
        self.assertIn(("（用户主动说）", "名字用 final.txt 就行"), adapter.seen[2], "shown on the next turn")

    def test_the_final_grade_sees_the_run_directory(self):
        res, ws, _ = run([("c5", "file_moved", {"at_start": True}, TOUCH("early.txt"))],
                         checks=[("nothing_after", {"no_mutation_after": {"change": "c5"}})])
        self.assertFalse(res.grade.error, res.grade.error)
        cp = res.grade.checkpoints["nothing_after"]
        self.assertTrue(cp.ok, cp.detail)          # the rename came after the change; nothing reads as a write here

    def test_a_task_without_changes_records_nothing(self):
        res, ws, _ = run([])
        self.assertEqual(fired_at(res), {})
        self.assertFalse((ws.root / "changes.jsonl").exists())
        self.assertEqual(res.state.value, "completed", res.failure)


if __name__ == "__main__":
    unittest.main()
