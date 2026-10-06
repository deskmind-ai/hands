"""HANDS_LOG_REQUESTS=1 on a run with no recorder used to raise AttributeError at the first step, which the run then
reported as a harness bug. Without a recorder there is nowhere to log to: the run goes on."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class NoRecorder(unittest.TestCase):
    def test_logging_requests_without_a_recorder_does_not_end_the_run(self):
        from deskmind_bench.task import load_task
        from deskmind_hands.actions import Action, ActionKind
        from deskmind_hands.adapters.base import Proposal
        from deskmind_hands.drivers.mock import MockDriver
        from deskmind_hands.env.workspace import Workspace
        from deskmind_hands.runtime.loop import RunConfig, run_task
        repo = Path(__file__).resolve().parent.parent
        task = load_task(repo / "tasks" / "smoke" / "S01-rename.yaml")
        ws = Workspace.create(Path(tempfile.mkdtemp()), repo / "fixtures")

        class Planner:
            name = "stub"
            last_request = {"state": {}, "questions": {}}

            def propose(self, ctx):
                return Proposal(action=Action(kind=ActionKind.DONE), raw_text="done")

            def usage(self):
                return {}
        with mock.patch.dict(os.environ, {"HANDS_LOG_REQUESTS": "1"}):
            res = run_task(task, MockDriver(render=False), Planner(), ws, config=RunConfig(), recorder=None)
        self.assertEqual(res.state.value, "completed", res.failure)


if __name__ == "__main__":
    unittest.main()
