"""The gym's local server: serves the sandbox apps and keeps the state each one reports.

    python -m tools.gym.server --port 8977

An app page is /apps/<name>.html?run=<id>&seed=<n>. The page posts its true state (what is playing, which settings
changed) to /state?run=<id> after every change; the runner and the oracle read it back from the same URL. Nothing
leaves 127.0.0.1, and no real app or user data is involved -- that is the point of a gym.
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
STATES: dict[str, dict] = {}
#: The task each run's page renders, set by the runner and fetched by the page (/task?run=<id>): kept out of the URL,
#: which the planner reads.
TASKS: dict[str, dict] = {}
LOCK = threading.Lock()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(ROOT), **k)

    def log_message(self, *a):   # quiet: the runner prints what matters
        pass

    def _run_id(self) -> str:
        return (parse_qs(urlparse(self.path).query).get("run") or [""])[0]

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/state", "/task"):
            with LOCK:
                src = STATES if path == "/state" else TASKS
                body = json.dumps(src.get(self._run_id(), {}), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path != "/state":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            state = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self.send_error(400)
            return
        with LOCK:
            STATES[self._run_id()] = state
        self.send_response(204)
        self.end_headers()


def serve(port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8977)
    a = ap.parse_args()
    serve(a.port)
    print(f"gym on http://127.0.0.1:{a.port}/apps/")
    threading.Event().wait()
