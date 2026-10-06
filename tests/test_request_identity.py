"""Every request says which run, step and observation it is for (protocol: Request identity, deskmind#36 item 4), so
a server's log and the run's trace can be joined, and a duplicate told apart. Brain drops fields it does not read, so
they go to any server. The trace keeps, per step, the ids of the requests it took and the form their options went in."""
from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_criteria_forms import QUESTIONS, Server   # noqa: E402  -- the fake server that keeps every body

from deskmind_hands.adapters.systemone import SystemOneAdapter   # noqa: E402


def digest(state) -> str:
    """As the protocol defines it: compact JSON in the order sent, SHA-256."""
    return "sha256:" + hashlib.sha256(json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class Identity(unittest.TestCase):
    def test_a_request_says_what_it_is_for(self):
        server = Server({"id": "m", "criteria_forms": ["object", "list"]})
        try:
            a = SystemOneAdapter(url=server.url)
            a._turn = {"session_id": "do-20261006-2300", "step": 4, "observation_id": "obs-0009"}
            state = {"page": {"title": "w"}, "elements": [{"index": 1}]}
            a._ask(state, QUESTIONS)
            a._ask(state, QUESTIONS)   # a second request in the same turn, as an override's re-ask is
        finally:
            server.srv.shutdown()
        first, second = server.bodies
        self.assertEqual((first["session_id"], first["step"], first["observation_id"]), ("do-20261006-2300", 4, "obs-0009"))
        self.assertEqual(first["state_digest"], digest(state))
        self.assertNotEqual(first["request_id"], second["request_id"], "two requests, two ids")
        self.assertEqual(second["step"], 4, "one step, whatever it took to decide it")
        self.assertEqual(a.sent, [{"id": first["request_id"], "form": "list"}, {"id": second["request_id"], "form": "list"}])
        self.assertNotIn("request_id", a.last_request, "replays compare last_request; an id is new every time")

    def test_the_trace_names_the_requests(self):
        """The loop puts the adapter's sent requests in the step's record."""
        import tempfile
        from deskmind_hands.adapters.base import Proposal
        from deskmind_hands.actions import Action, ActionKind
        from deskmind_hands.drivers.mock import MockDriver
        from deskmind_hands.env.workspace import Workspace
        from deskmind_hands.runtime.loop import RunConfig, run_task
        from deskmind_bench.task import load_task
        repo = Path(__file__).resolve().parent.parent
        task = load_task(repo / "tasks" / "smoke" / "S01-rename.yaml")
        ws = Workspace.create(Path(tempfile.mkdtemp()), repo / "fixtures")
        seen = []

        class Planner:
            name = "stub"

            def __init__(self):
                self.sent = []

            def propose(self, ctx):
                seen.append((ctx.run_id, ctx.step))
                self.sent = [{"id": f"req-{ctx.step}", "form": "object"}]
                return Proposal(action=Action(kind=ActionKind.DONE), raw_text="done")

            def usage(self):
                return {}
        res = run_task(task, MockDriver(render=False), Planner(), ws, config=RunConfig(), run_id="run-7")
        self.assertEqual(seen, [("run-7", 1)])
        self.assertEqual(res.steps[-1].get("requests"), [{"id": "req-1", "form": "object"}])


if __name__ == "__main__":
    unittest.main()
