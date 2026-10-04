"""Every request within the /v1/systemone protocol's bounds (1..255 options a choice), whatever is on screen: a
Downloads folder with ten subfolders put 380 "move to" options in one SELECT head, DeskMind Brain refused the request
whole (HTTP 400), and the run ended before its first step. And when a server does refuse, what it said is kept."""
from __future__ import annotations

import http.server
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.adapters.base import AdapterUnavailable, TurnContext   # noqa: E402
from deskmind_hands.adapters.systemone import (MAX_CHOICE_OPTIONS, SystemOneAdapter, fit_choices,   # noqa: E402
                                                request_shape)
from deskmind_hands.drivers.base import Element, Observation   # noqa: E402
from deskmind_hands.geometry import ImageTransform, Rect, ScreenGeometry, Size   # noqa: E402
from deskmind_hands.live import live_task   # noqa: E402


def choice(n):
    return {"type": "choice", "criteria": {str(i): f"option {i}" for i in range(n)}, "instructions": {}}


class FitChoices(unittest.TestCase):
    def test_within_bounds_unchanged(self):
        qs = {"operation": {"type": "choice", "criteria": {"CLICK": "", "DONE": ""}}, "click_target": choice(3),
              "goal_score": {"type": "score", "criteria": ["no", "yes"]}}
        self.assertEqual(fit_choices(qs), qs)

    def test_too_many_keeps_the_first(self):
        out = fit_choices({"select_target": choice(380)})
        crit = out["select_target"]["criteria"]
        self.assertEqual(len(crit), MAX_CHOICE_OPTIONS)
        self.assertEqual(list(crit)[:3], ["0", "1", "2"])           # options come ranked: the first are kept
        self.assertEqual(MAX_CHOICE_OPTIONS, 255)

    def test_empty_choice_and_its_operation_left_out(self):
        qs = {"operation": {"type": "choice", "criteria": {"SELECT": "", "CLICK": "", "DONE": ""}},
              "select_target": choice(0), "click_target": choice(2)}
        out = fit_choices(qs)
        self.assertNotIn("select_target", out)
        self.assertEqual(list(out["operation"]["criteria"]), ["CLICK", "DONE"])

    def test_shape_keeps_no_text(self):
        shape = request_shape({"state": {"page": {"text": "secret.pdf"}}, "questions": {"select_target": choice(4)}})
        self.assertEqual(shape, {"select_target": {"type": "choice", "options": 4}})
        self.assertNotIn("secret", json.dumps(shape))


def folder_window(files, folders):
    """A Finder-like window: one row per file, each with a "move to" dropdown listing every subfolder."""
    s = Size(1200, 900)
    els = [Element(id=f"f{i}", role="file", label=f, rect=Rect(10, 20 * i, 300, 18), app="访达",
                   options=list(folders)) for i, f in enumerate(files)]
    return Observation(id="obs-1", geometry=ScreenGeometry(s, s), transform=ImageTransform.identity(s), elements=els,
                       focused_app="访达", window_title="Downloads")


class ManyDropdowns(unittest.TestCase):
    def test_every_question_sent_is_within_bounds(self):
        d = Path(tempfile.mkdtemp())
        task = live_task("整理目录", d, app="com.apple.finder", max_actions=5, wall_clock_s=60)
        o = folder_window([f"file-{i}.pdf" for i in range(59)], [f"folder {k}" for k in range(10)])
        a = SystemOneAdapter(url="http://127.0.0.1:9", timeout=2)   # nothing listens: the request is built, then fails
        with self.assertRaises(AdapterUnavailable):
            a.propose(TurnContext(task=task, observation=o, history=[], channels=frozenset({"ax"})))
        sent = a.last_request["questions"]                              # as it went on the wire
        raw = sum(len(e.get("options") or ()) for e in a.last_request["state"]["elements"])
        self.assertGreater(raw, MAX_CHOICE_OPTIONS)                     # the screen did offer more than the bound
        for key, q in sent.items():
            if q.get("type") == "choice":
                self.assertTrue(1 <= len(q["criteria"]) <= MAX_CHOICE_OPTIONS, f"{key}: {len(q['criteria'])} options")
        self.assertIn("select_target", sent)                            # still offered, cut to the bound


class Refused(unittest.TestCase):
    def test_server_message_is_kept(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = json.dumps({"error": {"message": "choice criteria must be a map with 1..255 options"}}).encode()
                self.send_response(400); self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            a = SystemOneAdapter(url=f"http://127.0.0.1:{srv.server_port}")
            with self.assertRaises(AdapterUnavailable) as cm:
                a._ask({}, {"operation": choice(2)})
            self.assertIn("HTTP Error 400", str(cm.exception))
            self.assertIn("choice criteria must be a map with 1..255 options", str(cm.exception))
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
