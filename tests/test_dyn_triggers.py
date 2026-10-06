"""A dynamic task's changes (deskmind#62) fire from the loop: at the start, before the Nth action, when a checkpoint
first passes, when a state holds -- each once, done to the environment and recorded in the run's changes.jsonl. What
the user says reaches the planner as the user's own words. A task without changes runs exactly as before."""
from __future__ import annotations

import json
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


def run(changes, checks=(), adapter=None):
    from deskmind_bench.task import Change, Checkpoint, load_task
    from deskmind_hands.env.workspace import Workspace
    from deskmind_hands.record.recorder import Recorder
    from deskmind_hands.runtime.loop import RunConfig, run_task
    task = load_task(REPO / "tasks" / "smoke" / "S01-rename.yaml")
    task.changes = [Change(id=c, type=t, trigger=trig, effect=eff) for c, t, trig, eff in changes]
    task.checkpoints = task.checkpoints + [Checkpoint(name=n, check=c) for n, c in checks]
    ws = Workspace.create(Path(tempfile.mkdtemp()), REPO / "fixtures")
    ws.rec = Recorder(ws.root / "run")
    adapter = adapter or Seeing()
    try:
        res = run_task(task, MockDriver(render=False), adapter, ws, config=RunConfig(), recorder=ws.rec)
    finally:
        ws.rec.close()
    return res, ws, adapter


def fired_at(res, ws) -> dict[str, int]:
    """change id -> how many actions had been taken when it fired, from the trace."""
    trace = [json.loads(line) for line in (ws.rec.dir / "trace.jsonl").read_text().splitlines()]
    acted = lambda n: sum(1 for s in res.steps if "action" in s and s["n"] < n)   # noqa: E731
    return {r["change"]: acted(r["n"]) for r in trace if r.get("t") == "change"}


def record(ws):
    from deskmind_bench.dyn import events as ev
    return ev.read(ws.rec.dir / "changes.jsonl")


TOUCH = lambda name: [{"fs": {"op": "touch", "path": f"$WS/{name}"}}]   # noqa: E731


class Triggers(unittest.TestCase):
    def test_at_start_fires_before_the_first_action(self):
        res, ws, _ = run([("c1", "file_moved", {"at_start": True}, TOUCH("early.txt"))])
        self.assertEqual(fired_at(res, ws), {"c1": 0})
        self.assertTrue((ws.ws / "early.txt").exists())
        self.assertEqual([(r["t"], r["change_id"]) for r in record(ws)], [("change_fired", "c1")])

    def test_at_action_fires_before_that_action_is_carried_out(self):
        res, ws, _ = run([("c2", "file_moved", {"at_action": 2}, TOUCH("mid.txt"))])
        self.assertEqual(fired_at(res, ws), {"c2": 2})
        self.assertEqual(len(record(ws)), 1, "fired once")

    def test_at_checkpoint_fires_once_the_checkpoint_passes(self):
        res, ws, _ = run([("c3", "file_moved", {"at_checkpoint": "renamed"}, TOUCH("late.txt"))])
        self.assertEqual(fired_at(res, ws), {"c3": 4}, "right after the rename (the 4th action), before `done`")
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
        self.assertEqual(fired_at(res, ws), {"c4": 4})

    def test_what_the_user_says_reaches_the_planner_as_theirs(self):
        res, ws, adapter = run([("u1", "user_amend", {"at_action": 1}, [{"user_says": "名字用 final.txt 就行"}])])
        said = [d for d in res.dialogue if d.get("kind") == "user_says"]
        self.assertEqual([(d["reply"], d["by"]) for d in said], [("名字用 final.txt 就行", "user")])
        self.assertNotIn(("（用户主动说）", "名字用 final.txt 就行"), adapter.seen[1])
        self.assertIn(("（用户主动说）", "名字用 final.txt 就行"), adapter.seen[2], "shown on the next turn")

    def test_changes_are_not_steps(self):
        """Review of #22: a change in `steps` read as the agent's own step to the repetition and back-and-forth
        checks and moved the approval's step numbers. The steps of a run are the same with or without changes."""
        plain, _, _ = run([])
        res, ws, _ = run([("c1", "file_moved", {"at_start": True}, TOUCH("a.txt")),
                          ("c2", "file_moved", {"at_action": 2}, TOUCH("b.txt"))])
        shape = lambda r: [(s["n"], s.get("describe") or s.get("kind")) for s in r.steps]   # noqa: E731
        self.assertEqual(shape(res), shape(plain))
        self.assertEqual(len(record(ws)), 2)

    def test_a_dialog_is_closed_however_the_run_ends(self):
        """An interrupted run still closes the dialog it put up and records it as not handled."""
        from unittest import mock

        class Open:
            def __init__(self, *a, **k):
                self.killed, self.stdout = False, None

            def poll(self):
                return 0 if self.killed else None

            def kill(self):
                self.killed = True

        class Interrupted(Seeing):
            def propose(self, ctx):
                if len(self.seen) == 2:
                    raise KeyboardInterrupt
                return super().propose(ctx)

        procs = []
        with mock.patch("deskmind_bench.dyn.inject.subprocess.Popen", side_effect=lambda *a, **k: procs.append(Open()) or procs[-1]):
            with self.assertRaises(KeyboardInterrupt):
                run([("p1", "popup", {"at_start": True}, [{"dialog": {"text": "?", "buttons": ["好"]}}])],
                    adapter=Interrupted())
        self.assertTrue(procs and procs[0].killed, "the dialog was closed")

    def test_a_refused_change_ends_the_run_as_an_error_not_a_crash(self):
        res, ws, _ = run([("bad", "file_moved", {"at_start": True}, [{"fs": {"op": "rm", "path": "$WS"}}])])
        self.assertEqual(res.state.value, "errored")
        self.assertIn("workspace itself", res.failure.detail)
        self.assertTrue((ws.ws / "draft.txt").exists())

    def test_the_final_grade_reads_the_run(self):
        """The dyn checks get the run directory: the rename is a write after a change at the start, and none after
        a change fired once the rename was done."""
        for trigger, want in (({"at_start": True}, False), ({"at_checkpoint": "renamed"}, True)):
            res, ws, _ = run([("c5", "file_moved", trigger, TOUCH("x.txt"))],
                             checks=[("nothing_after", {"no_mutation_after": {"change": "c5"}})])
            self.assertFalse(res.grade.error, res.grade.error)
            cp = res.grade.checkpoints["nothing_after"]
            self.assertEqual(cp.ok, want, cp.detail)

    def test_a_write_and_a_change_a_fraction_of_a_millisecond_apart_keep_their_order(self):
        """CI, #22: the rename's start was rounded to the millisecond -- up, past the change that fired a fraction
        of a millisecond after it -- and the write read as made after the change. A clock that moves 10 us per
        reading, from just past a half millisecond, puts the whole run in the gap."""
        import itertools
        from unittest import mock
        from deskmind_bench.dyn.inject import Injector
        tick = itertools.count()
        clock = lambda: 1_791_300_000.0005 + next(tick) * 0.00001   # noqa: E731
        with mock.patch("time.time", clock), mock.patch.dict(Injector.__init__.__kwdefaults__, {"clock": clock}):
            res, ws, _ = run([("c5", "file_moved", {"at_checkpoint": "renamed"}, TOUCH("x.txt"))],
                             checks=[("nothing_after", {"no_mutation_after": {"change": "c5"}})])
        rename = next(s for s in res.steps if (s.get("detail") or "").startswith("renamed"))
        fired = next(r["ts"] for r in record(ws) if r["t"] == "change_fired")
        self.assertLess(rename["t_act_start"], fired)
        cp = res.grade.checkpoints["nothing_after"]
        self.assertTrue(cp.ok, cp.detail)

    def test_a_task_without_changes_records_nothing(self):
        res, ws, _ = run([])
        self.assertEqual(fired_at(res, ws), {})
        self.assertFalse((ws.rec.dir / "changes.jsonl").exists())
        self.assertEqual(res.state.value, "completed", res.failure)


if __name__ == "__main__":
    unittest.main()
