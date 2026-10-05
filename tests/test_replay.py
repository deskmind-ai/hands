"""Layer 2 of the harness evaluation: recorded runs replayed without the desktop or a model (tools/harness_replay.py).

Every case must replay to exactly the requests it was recorded with. A failure names the case and the first
difference; if the change is meant, `python tools/harness_replay.py check --update` accepts it (and the diff is a
change to what the planner sees -- tell whoever trains it)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))


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
