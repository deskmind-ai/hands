"""hands does not act on a head the model did not score (scored: false, deskmind#36 item 2, brain#10). A two-stage
server scores only the chosen operation's heads and returns the others uniform; TYPE_FOCUSED's value was never among
them (G1), so hands typed the first candidate whatever the model would have chosen. An unscored head with more than
one option is asked again for its operation alone; still unscored, the step is refused. One option needs no choice."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.adapters.base import TurnContext   # noqa: E402
from deskmind_hands.adapters.systemone import SystemOneAdapter   # noqa: E402
from deskmind_hands.drivers.base import Element, Observation   # noqa: E402
from deskmind_hands.geometry import ImageTransform, Rect, ScreenGeometry, Size   # noqa: E402
from deskmind_hands.live import live_task   # noqa: E402

GOAL = 'In the note, write "beta" instead of "alpha"'


def value_of(v):
    return v if isinstance(v, str) else next(iter(v.values()))


class Server:
    """TYPE_TEXT chosen; its value head unscored on the full request, and scored on the re-ask unless `stubborn`."""

    def __init__(self, stubborn=False, single=False):
        self.stubborn, self.single, self.requests = stubborn, single, []

    def __call__(self, state, questions):
        self.requests.append(questions)
        out = {}
        ops = list(questions["operation"]["criteria"])
        out["operation"] = {"type": "choice", "choice": "TYPE_TEXT",
                            "probabilities": {o: (1.0 if o == "TYPE_TEXT" else 0.0) for o in ops}}
        for qid, q in questions.items():
            if qid == "operation":
                continue
            keys = list(q["criteria"])
            if qid == "type_text_value" and (len(self.requests) == 1 or self.stubborn):
                out[qid] = {"type": "choice", "choice": keys[0], "probabilities": {k: 1 / len(keys) for k in keys},
                            "scored": False}
            else:
                pick = next((k for k, v in q["criteria"].items() if value_of(v) == "alpha"), keys[0])
                out[qid] = {"type": "choice", "choice": pick, "probabilities": {k: float(k == pick) for k in keys}}
        return out


class Unscored(unittest.TestCase):
    def propose(self, server, goal=GOAL):
        size = Size(1200, 900)
        o = Observation(id="obs-1", geometry=ScreenGeometry(size, size), transform=ImageTransform.identity(size),
                        elements=[Element(id="e1", role="textarea", ax_role="AXTextArea", label="", value="alpha\n",
                                          settable=True, rect=Rect(10, 40, 800, 600), app="Notes")],
                        focused_app="Notes", window_title="note")
        task = live_task(goal, Path(tempfile.mkdtemp()), app="com.apple.Notes", max_actions=20, wall_clock_s=60)
        a = SystemOneAdapter(url="http://127.0.0.1:9", timeout=2)
        a._ask = server
        return a.propose(TurnContext(task=task, observation=o, history=[], channels=frozenset({"ax"})))

    def test_an_unscored_value_is_asked_again(self):
        server = Server()
        p = self.propose(server)
        self.assertEqual(len(server.requests), 2)
        self.assertEqual(list(server.requests[1]["operation"]["criteria"]), ["TYPE_TEXT"])
        self.assertEqual(p.action.text, "alpha", f"typed {p.action.text!r}: the placeholder's first candidate")

    def test_still_unscored_is_refused(self):
        p = self.propose(Server(stubborn=True))
        self.assertIsNotNone(p.parse_error)
        self.assertIn("did not choose type_text_value for TYPE_TEXT", p.parse_error)

    def test_one_option_needs_no_choice(self):
        server = Server(stubborn=True)
        p = self.propose(server, goal='In the note, write "alpha"')
        self.assertIsNone(p.parse_error, p.parse_error)
        self.assertEqual(len(server.requests), 1)


if __name__ == "__main__":
    unittest.main()
