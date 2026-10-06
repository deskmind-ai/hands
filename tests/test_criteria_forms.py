"""hands sends a choice's options as a list where the server reads one (deskmind#36 item 1, protocol deskmind#38,
Brain brain#9). An object's key order is not JSON's to keep, and a tool that sorted keys on the way changed what the
model saw (brain#8). A server that does not say it reads lists -- the Brain 0.4.1 ships, another server -- gets the
object form as before."""
from __future__ import annotations

import http.server
import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.adapters.systemone import SystemOneAdapter   # noqa: E402

QUESTIONS = {
    "operation": {"type": "choice", "instructions": {}, "criteria": {"CLICK": "click", "OPEN": "open", "DONE": "done"}},
    "click_target": {"type": "choice", "instructions": {},
                     "criteria": {str(i): {"element": f"[{i}] row {i}"} for i in (1, 2, 10, 11)}},
}


class Server:
    """GET /v1/models says what `models` holds (None: 404); POST /v1/systemone answers the first option of each."""

    def __init__(self, models):
        self.models, self.bodies, self.gets = models, [], 0
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def reply(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                outer.gets += 1
                if outer.models is None:
                    return self.reply(404, {"error": {"message": "not found"}})
                self.reply(200, {"data": [outer.models]})

            def do_POST(self):  # noqa: N802
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                outer.bodies.append(req)
                answers = {}
                for qid, q in req["questions"].items():
                    crit = q["criteria"]
                    first = crit[0]["key"] if isinstance(crit, list) else next(iter(crit))
                    answers[qid] = {"type": "choice", "choice": first, "probabilities": {first: 1.0}}
                self.reply(200, {"answers": answers})

            def log_message(self, *a):
                pass
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"


class Forms(unittest.TestCase):
    def ask(self, models, times=1):
        server = Server(models)
        try:
            a = SystemOneAdapter(url=server.url)
            for _ in range(times):
                answers = a._ask({}, QUESTIONS)
            return server, a, answers
        finally:
            server.srv.shutdown()

    def test_a_server_that_reads_lists_gets_lists_in_order(self):
        server, a, answers = self.ask({"id": "m", "object": "model", "criteria_forms": ["object", "list"]})
        sent = server.bodies[0]["questions"]
        self.assertEqual(sent["operation"]["criteria"], [{"key": "CLICK", "description": "click"},
                                                         {"key": "OPEN", "description": "open"},
                                                         {"key": "DONE", "description": "done"}])
        self.assertEqual([o["key"] for o in sent["click_target"]["criteria"]], ["1", "2", "10", "11"])
        self.assertEqual(sent["click_target"]["criteria"][2]["description"], {"element": "[10] row 10"})
        self.assertEqual(answers["operation"]["choice"], "CLICK", "the reply is checked against the same options")
        self.assertIsInstance(a.last_request["questions"]["operation"]["criteria"], dict, "what is recorded stays an object")

    def test_a_server_that_does_not_say_gets_objects(self):
        for models in ({"id": "m", "object": "model"}, None):
            server, _, _ = self.ask(models)
            self.assertIsInstance(server.bodies[0]["questions"]["operation"]["criteria"], dict, repr(models))

    def test_the_server_is_asked_once(self):
        server, _, _ = self.ask({"id": "m", "criteria_forms": ["object", "list"]}, times=3)
        self.assertEqual(server.gets, 1)
        self.assertEqual(len(server.bodies), 3)
        server, _, _ = self.ask(None, times=3)
        self.assertEqual(server.gets, 1, "a 404 is an answer too: not asked again every step")


if __name__ == "__main__":
    unittest.main()
