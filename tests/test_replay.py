"""Layer 2 of the harness evaluation: recorded runs replayed without the desktop or a model (tools/harness_replay.py).

Every case must replay to exactly the requests it was recorded with. A failure names the case and the first
difference; if the change is meant, `python tools/harness_replay.py check --update` accepts it (and the diff is a
change to what the planner sees -- tell whoever trains it)."""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "tests"))


class ReplaySnapshots(unittest.TestCase):
    def test_every_recorded_run_replays_to_the_same_requests(self):
        import harness_replay
        os.environ["HANDS_EFFECT_NOTES"] = "0"
        failures = harness_replay.check()
        self.assertEqual(failures, [], "\n".join(f"{n}: {d}" for n, d in failures))

    def test_the_replay_does_not_depend_on_the_login_name(self):
        """hands#3: on a machine whose login name was "user" -- the name the recorded traces were sanitized to --
        the sidebar label was redacted a second time and the snapshot differed."""
        import harness_replay
        from deskmind_hands.adapters import systemone
        os.environ["HANDS_EFFECT_NOTES"] = "0"
        saved = systemone.HOME_NAME
        try:
            for name in ("user", "someone", ""):
                systemone.HOME_NAME = name
                failures = harness_replay.check()
                self.assertEqual(failures, [], f"login name {name!r}: " + "\n".join(f"{n}: {d}" for n, d in failures))
                self.assertEqual(systemone.HOME_NAME, name, "the replay puts the login name back")
        finally:
            systemone.HOME_NAME = saved

    def test_a_reordering_is_a_difference(self):
        """The order of a question's options sets the letters the planner answers with (brain#8: re-sorted, the same
        model fell from 220 to about 130 of 223 valid steps). The snapshots were written and compared with sorted
        keys, so a harness change that reordered the options passed."""
        import harness_replay
        from deskmind_hands.replay import normalize
        want = {"criteria": {"1": "a", "2": "b", "10": "c"}}
        got = {"criteria": {"1": "a", "10": "c", "2": "b"}}
        self.assertEqual(harness_replay.first_difference(want, got),
                         "/criteria: key 2 is '2' expected, '10' now")
        self.assertIsNone(harness_replay.first_difference(want, want))
        self.assertEqual(list(normalize(want)["criteria"]), ["1", "2", "10"], "normalize keeps the order")
        case = next(p for p in harness_replay.CASES.iterdir() if (p / "expected.json").exists())
        first = json.loads((case / "expected.json").read_text(encoding="utf-8"))["requests"][0]["questions"]
        self.assertEqual(next(iter(first)), "operation", f"{case.name}: the snapshot is in the order sent")

    def test_no_setting_of_the_callers_reaches_the_replay(self):
        """deskmind#58 step 0: HANDS_COMPLETION_CHECK was not among the switches turned off for a replay, so set in
        the caller's shell it added a goal_complete question to every request after the first, and the snapshots
        failed on one machine and passed on another."""
        import harness_replay
        from unittest import mock
        with mock.patch.dict(os.environ, {"HANDS_COMPLETION_CHECK": "0.5", "HANDS_DONE_CHECK": "1",
                                          "HANDS_PROGRESS": "1", "HANDS_PRIOR_READ": "1"}):
            failures = harness_replay.check()
        self.assertEqual(failures, [], "\n".join(f"{n}: {d}" for n, d in failures))

    def test_an_answer_is_replayed(self):
        """An ANSWER ends the run: its step has no action and keeps the answer in its text. The replay read the text
        from the (missing) action and reported every recorded answer as not offered."""
        from deskmind_hands.replay import FollowAdapter
        a = FollowAdapter([{"kind": "done", "text": "answer: Meeting time: 14:00",
                            "decision": '{"operation": "ANSWER", "answer": "Meeting time: 14:00"}'}])
        answers = a._ask({"elements": []}, {
            "operation": {"type": "choice", "criteria": {"CLICK": "", "ANSWER": "", "DONE": ""}},
            "answer_value": {"type": "choice", "criteria": {"1": {"value": "Offsite venue"},
                                                            "2": {"value": "Meeting time: 14:00"}}}})
        self.assertEqual(a.divergences, [])
        self.assertEqual(answers["answer_value"]["choice"], "2")

    def test_the_completion_check_is_answered_as_recorded(self):
        """Answered with its first option ("yes"), the completion check ended every replay of a run recorded with
        HANDS_COMPLETION_CHECK at its second step."""
        from deskmind_hands.replay import FollowAdapter
        q = {"operation": {"type": "choice", "criteria": {"CLICK": "", "DONE": ""}},
             "goal_complete": {"type": "choice", "criteria": {"yes": "", "no": ""}}}
        a = FollowAdapter([{"decision": '{"operation": "CLICK"}', "action": {"kind": "click"}},
                           {"decision": '{"operation": "DONE", "stop_by": "check"}', "kind": "done"}])
        self.assertEqual(a._ask({"elements": []}, q)["goal_complete"]["choice"], "no")
        self.assertEqual(a._ask({"elements": []}, q)["goal_complete"]["choice"], "yes")

    def test_what_no_case_reaches_is_known(self):
        """deskmind#58 step 0: the refactor may move only code the snapshots pin. What no case puts to the planner
        is listed here; a new case that reaches one of these takes it off the list."""
        import harness_replay
        self.assertEqual(harness_replay.gaps(), {
            "questions": ["focus_app_target", "rename_target"],
            "operations": ["FOCUS_APP", "RENAME", "TYPE_FOCUSED"],
        })

    def test_a_task_run_replays_to_the_requests_it_sent(self):
        """deskmind#58 step 0: bench and gym runs (a task file, not a `hands do` goal) replay too, so the refactor is
        checked against every recorded run on a machine -- thousands of Finder and TextEdit steps the snapshots do
        not have -- and a replay of one sends what the run sent."""
        import tempfile
        from test_criteria_forms import Server
        from deskmind_bench.task import load_task
        from deskmind_hands.adapters.systemone import SystemOneAdapter
        from deskmind_hands.drivers.mock import MockDriver
        from deskmind_hands.env.workspace import Workspace
        from deskmind_hands.record.recorder import Recorder
        from deskmind_hands.replay import replay
        from deskmind_hands.runtime.loop import RunConfig, run_task
        import harness_replay
        task_file = REPO / "tasks" / "smoke" / "S01-rename.yaml"
        task = load_task(task_file)
        task.budget.max_actions = 4
        root = Path(tempfile.mkdtemp())
        ws = Workspace.create(root / "live", REPO / "fixtures")
        rec = Recorder(root / "run")
        rec.manifest({"run_id": "r1", "task_id": task.id, "task_file": str(task_file), "adapter": "systemone"})
        server = Server({"id": "m", "criteria_forms": ["object"]})
        try:
            run_task(task, MockDriver(render=False), SystemOneAdapter(url=server.url), ws, config=RunConfig(),
                     recorder=rec)
        finally:
            rec.close()
            server.srv.shutdown()
        # What the run sent, as a replay builds it (the model name is the replay's own).
        sent = [{"state": b["state"], "model": "replay", "questions": b["questions"]} for b in server.bodies]
        self.assertGreaterEqual(len(sent), 2)
        with mock.patch.dict(os.environ, {k: "0" for k in harness_replay.SWITCHES}):
            r = replay(root / "run")
        self.assertEqual(r.divergences, [])
        self.assertEqual([harness_replay.normalize(q) for q in r.requests],
                         [harness_replay.normalize(q) for q in sent])
        dumped = harness_replay.corpus([root])
        self.assertEqual(list(dumped.values())[0]["requests"], [harness_replay.normalize(q) for q in sent])
        self.assertNotIn("hands-replay-", json.dumps(dumped), "the scratch folder's name is not in the dump")

    def test_a_changed_option_is_a_divergence(self):
        """The recorded choice missing from the options is reported, not guessed around."""
        from deskmind_hands.replay import FollowAdapter
        a = FollowAdapter([{"decision": '{"operation": "ASK"}', "kind": "ask_user"}])
        answers = a._ask({"elements": []}, {"operation": {"type": "choice",
                                                          "criteria": {"CLICK": "", "DONE": ""}}})
        self.assertEqual(answers["operation"]["choice"], "CLICK")
        self.assertIn("operation ASK not offered", a.divergences[0].what)


if __name__ == "__main__":
    unittest.main()
