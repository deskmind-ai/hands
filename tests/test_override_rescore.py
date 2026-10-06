"""A low DONE or BLOCKED that hands overrides is replaced by an operation whose heads the model actually chose
(protocol review, 10-06). A server that scores in two stages scores only the heads of the operation it picked and
returns the rest uniform; the override then acted on those: a single target reads 1.00, and the value taken was the
first candidate, which the model never chose. The alternative's heads are now asked again, for it alone."""
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


class TwoStage:
    """Answers the way a two-stage server does: the operation, then only the chosen operation's heads; every
    other head uniform."""

    def __init__(self, first: dict[str, float], want_value: str):
        self.first, self.want_value, self.requests = first, want_value, []

    def __call__(self, state, questions):
        self.requests.append(questions)
        ops = list(questions["operation"]["criteria"])
        out = {}
        if len(ops) > 1:
            probs = {o: self.first.get(o, 0.0) for o in ops}
            rest = (1.0 - sum(probs.values())) / max(1, sum(1 for o in ops if o not in self.first))
            probs = {o: p if o in self.first else rest for o, p in probs.items()}
            out["operation"] = {"type": "choice", "choice": max(probs, key=probs.get), "probabilities": probs}
            for qid, q in questions.items():
                if qid != "operation":
                    keys = list(q["criteria"])
                    out[qid] = {"type": "choice", "choice": keys[0], "probabilities": {k: 1 / len(keys) for k in keys}}
            return out
        op = ops[0]
        out["operation"] = {"type": "choice", "choice": op, "probabilities": {op: 1.0}}
        for qid, q in questions.items():
            if qid == "operation":
                continue
            keys = list(q["criteria"])
            pick = next((k for k, v in q["criteria"].items() if value_of(v) == self.want_value), keys[0]) \
                if qid == "type_text_value" else keys[0]
            out[qid] = {"type": "choice", "choice": pick,
                        "probabilities": {k: (0.9 if k == pick else 0.1 / max(1, len(keys) - 1)) for k in keys}}
        return out


class OverrideRescore(unittest.TestCase):
    def propose(self, server):
        s = Size(1200, 900)
        o = Observation(id="obs-1", geometry=ScreenGeometry(s, s), transform=ImageTransform.identity(s),
                        elements=[Element(id="e1", role="textarea", ax_role="AXTextArea", label="", value="alpha\n",
                                          settable=True, rect=Rect(10, 40, 800, 600), app="Notes")],
                        focused_app="Notes", window_title="note")
        task = live_task(GOAL, Path(tempfile.mkdtemp()), app="com.apple.Notes", max_actions=20, wall_clock_s=60)
        a = SystemOneAdapter(url="http://127.0.0.1:9", timeout=2)
        a._ask = server
        return a.propose(TurnContext(task=task, observation=o, history=[], channels=frozenset({"ax"})))

    def test_the_override_acts_on_heads_the_model_chose(self):
        """Asked for TYPE_TEXT alone, the model chooses the second candidate; the uniform head had the first."""
        server = TwoStage({"DONE": 0.45, "TYPE_TEXT": 0.40}, want_value="alpha")
        p = self.propose(server)
        values = [value_of(v) for v in server.requests[0]["type_text_value"]["criteria"].values()]
        self.assertEqual(values[1], "alpha", f"the test needs alpha second: {values}")
        self.assertEqual(p.action.text, "alpha", f"took {p.action.text!r}: the uniform head's first candidate")

    def test_the_second_request_asks_the_alternative_alone(self):
        server = TwoStage({"DONE": 0.45, "TYPE_TEXT": 0.40}, want_value="alpha")
        self.propose(server)
        self.assertEqual(len(server.requests), 2)
        again = server.requests[1]
        self.assertEqual(list(again["operation"]["criteria"]), ["TYPE_TEXT"])
        self.assertEqual(set(again) - {"operation"}, {"type_text_target", "type_text_value"})

    def test_no_override_no_second_request(self):
        server = TwoStage({"TYPE_TEXT": 0.9}, want_value="beta")
        self.propose(server)
        self.assertEqual(len(server.requests), 1)


if __name__ == "__main__":
    unittest.main()
