"""The trace keeps what the model answered, head by head, with its full probabilities and who answered -- before any
rule here acts on it (deskmind#59: "know when it doesn't know" is studied from these; the decision record keeps only
the top three operations, cut to 300 characters)."""
from __future__ import annotations

import http.server
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from deskmind_hands.adapters.systemone import SystemOneAdapter   # noqa: E402

QUESTIONS = {
    "operation": {"type": "choice", "instructions": {}, "criteria": {"CLICK": "click", "OPEN": "open", "DONE": "done"}},
    "click_target": {"type": "choice", "instructions": {}, "criteria": {"1": {"element": "a"}, "2": {"element": "b"}}},
}


class Server:
    """Answers every option of every head, with the probabilities spread, and says which tier answered."""

    def __init__(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self._reply({"data": [{"id": "m", "criteria_forms": ["object"]}]})

            def do_POST(self):  # noqa: N802
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                answers = {}
                for qid, q in req["questions"].items():
                    keys = list(q["criteria"])
                    probs = {k: round(0.7 if i == 0 else 0.3 / (len(keys) - 1), 4) for i, k in enumerate(keys)}
                    answers[qid] = {"type": "choice", "choice": keys[0], "confidence": 0.7, "probabilities": probs}
                self._reply({"answers": answers, "routing": {"by": "fast", "reason": "confident", "fast_conf": 0.7}})

            def _reply(self, obj):
                body = json.dumps(obj).encode()
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
    def test_every_reply_is_kept_whole(self):
        server = Server()
        try:
            a = SystemOneAdapter(url=server.url)
            a._ask({"elements": []}, QUESTIONS)
            a._ask({"elements": []}, QUESTIONS)   # a re-ask in the same turn is a reply of its own
        finally:
            server.srv.shutdown()
        self.assertEqual([r["id"] for r in a.replies], [s["id"] for s in a.sent])
        first = a.replies[0]
        self.assertEqual(first["routing"]["by"], "fast")
        self.assertEqual(first["answers"]["operation"],
                         {"choice": "CLICK", "probabilities": {"CLICK": 0.7, "OPEN": 0.15, "DONE": 0.15}})
        self.assertEqual(first["answers"]["click_target"]["probabilities"], {"1": 0.7, "2": 0.3})

    def test_the_trace_keeps_them_by_step(self):
        from deskmind_bench.task import load_task
        from deskmind_hands.actions import Action, ActionKind
        from deskmind_hands.adapters.base import Proposal
        from deskmind_hands.drivers.mock import MockDriver
        from deskmind_hands.env.workspace import Workspace
        from deskmind_hands.record.recorder import Recorder
        from deskmind_hands.runtime.loop import RunConfig, run_task

        class Planner:
            name = "stub"

            def __init__(self):
                self.sent, self.replies = [], []

            def propose(self, ctx):
                self.replies = [{"id": f"req-{ctx.step}", "answers": {"operation": {"choice": "DONE",
                                                                                      "probabilities": {"DONE": 0.9}}}}]
                return Proposal(action=Action(kind=ActionKind.DONE), raw_text="done")

            def usage(self):
                return {}
        task = load_task(REPO / "tasks" / "smoke" / "S01-rename.yaml")
        ws = Workspace.create(Path(tempfile.mkdtemp()), REPO / "fixtures")
        rec = Recorder(ws.root / "run")
        try:
            run_task(task, MockDriver(render=False), Planner(), ws, config=RunConfig(), recorder=rec)
        finally:
            rec.close()
        trace = [json.loads(line) for line in (rec.dir / "trace.jsonl").read_text().splitlines()]
        got = [r for r in trace if r.get("t") == "answers"]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["n"], 1)
        self.assertEqual(got[0]["replies"][0]["answers"]["operation"]["probabilities"], {"DONE": 0.9})


if __name__ == "__main__":
    unittest.main()
