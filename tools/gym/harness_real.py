"""The gym's mail app on the real screen, for harness 0.5 (the runtime drives it through `deskmind_hands.executor`).

    uv run --extra macos python -m tools.gym.harness_real open --seed 3      # serve, register the task, open GymHost
    uv run --extra macos python -m tools.gym.harness_real grade --seed 3 --run <run> [--answer "14:00"]

`open` prints one JSON line {run, seed, goal, kind, url, port} and keeps serving until killed; the harness is then
run by hand with `driver: "hands"` and that goal. `grade` reads the page's true state back and applies mail.passed.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.request

from tools.gym import mail, server
from tools.gym.run import HOST_APP, _open_host, host_args  # noqa: F401 -- the same host, the same wake-up


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("open", "grade"))
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--port", type=int, default=8977)
    ap.add_argument("--run")
    ap.add_argument("--answer")
    a = ap.parse_args()
    task = mail.make_task(a.seed)
    if a.cmd == "grade":
        with urllib.request.urlopen(f"http://127.0.0.1:{a.port}/state?run={a.run}", timeout=5) as r:
            state = json.load(r)
        ok = mail.passed(task, state, a.answer)
        print(json.dumps({"passed": ok, "state": state, "kind": task["kind"], "target": task["target"]}, ensure_ascii=False))
        return 0 if ok else 1
    rid = a.run or f"real-{a.seed}-{int(time.time())}"
    server.serve(a.port)
    with server.LOCK:
        server.TASKS[rid] = task["page"]
    url = f"http://127.0.0.1:{a.port}/apps/mail.html?run={rid}&seed={a.seed}"
    n = _open_host(url)
    print(json.dumps({"run": rid, "seed": a.seed, "goal": task["goal"], "kind": task["kind"], "url": url, "port": a.port, "ax_elements": n}, ensure_ascii=False))
    sys.stdout.flush()
    threading.Event().wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
