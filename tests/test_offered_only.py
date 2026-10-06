"""hands acts only on options it offered (protocol review, 10-06). It takes its own argmax of the probabilities, and
read a 1-based position key with int(key) - 1: a reply whose choice was "1" but whose probability sat on an option
nobody offered was acted on, and a key "0" became the last candidate, "-1" the one before it."""
from __future__ import annotations

import http.server
import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.adapters import systemone   # noqa: E402
from deskmind_hands.adapters.base import AdapterUnavailable   # noqa: E402
from deskmind_hands.adapters.systemone import SystemOneAdapter   # noqa: E402

QUESTIONS = {
    "operation": {"type": "choice", "criteria": {"CLICK": "click", "DONE": "done"}, "instructions": {}},
    "type_text_value": {"type": "choice", "criteria": {"1": {"value": "a"}, "2": {"value": "b"}, "3": {"value": "c"}},
                        "instructions": {}},
}


def answer(probs: dict, choice=None) -> dict:
    return {"type": "choice", "choice": choice if choice is not None else max(probs, key=probs.get),
            "probabilities": probs, "confidence": 1.0}


class Server:
    """A /v1/systemone server that answers with whatever the test sets."""

    def __init__(self):
        self.reply: dict = {}
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = json.dumps(outer.reply).encode()   # allow_nan: a NaN goes out as the bare token
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"


class Replies(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server()

    @classmethod
    def tearDownClass(cls):
        cls.server.srv.shutdown()

    def ask(self, answers: dict) -> dict:
        self.server.reply = {"answers": answers}
        return SystemOneAdapter(url=self.server.url)._ask({}, QUESTIONS)

    def refused(self, answers: dict) -> str:
        with self.assertRaises(AdapterUnavailable) as cm:
            self.ask(answers)
        return str(cm.exception)

    def test_a_reply_about_the_offered_options_is_taken(self):
        got = self.ask({"operation": answer({"CLICK": 0.7, "DONE": 0.3}),
                        "type_text_value": answer({"1": 0.2, "2": 0.5, "3": 0.3}),
                        "click_target": answer({"9": 1.0})})          # not asked: left out, never acted on
        self.assertEqual(set(got), {"operation", "type_text_value"})

    def test_a_probability_on_an_option_not_offered_is_refused(self):
        """The review's case: the choice is fine, the probability that hands acts on is not."""
        why = self.refused({"operation": answer({"CLICK": 1.0}),
                            "type_text_value": answer({"1": 0.0, "0": 1.0}, choice="1")})
        self.assertIn("type_text_value: a probability for '0', which was not offered", why)

    def test_keys_a_position_would_misread_are_refused(self):
        for key in ("0", "-1", "01", "+1", "4"):
            why = self.refused({"type_text_value": answer({"1": 0.0, key: 1.0}, choice="1")})
            self.assertIn(f"a probability for {key!r}", why)

    def test_a_choice_not_offered_is_refused(self):
        self.assertIn("chose 'TELEPORT'", self.refused({"operation": answer({"CLICK": 1.0}, choice="TELEPORT")}))

    def test_probabilities_are_numbers_from_0_to_1(self):
        for bad in (float("nan"), float("inf"), -0.5, 1.5, True, "0.9"):
            why = self.refused({"operation": {"type": "choice", "probabilities": {"CLICK": bad}}})
            self.assertIn("operation: probability", why)


class Positions(unittest.TestCase):
    def test_only_1_to_n_names_a_candidate(self):
        self.assertEqual([systemone.position(k, 3) for k in ("1", "2", "3")], [0, 1, 2])
        for key in ("0", "-1", "01", "+1", "4", "", " 1", "1.0", None, 1, "١", "²", "1\n"):
            self.assertIsNone(systemone.position(key, 3), repr(key))

    def test_a_value_key_0_is_not_the_last_candidate(self):
        got = SystemOneAdapter._chosen_value({"type_text_value": answer({"0": 1.0})}, ["a", "b", "c"])
        self.assertIsNone(got, "int('0') - 1 is -1: the last candidate")
        got = SystemOneAdapter._chosen_value({"type_text_value": answer({"-1": 1.0})}, ["a", "b", "c"])
        self.assertIsNone(got)
        self.assertEqual(SystemOneAdapter._chosen_value({"type_text_value": answer({"2": 1.0})}, ["a", "b", "c"]), "b")


if __name__ == "__main__":
    unittest.main()
