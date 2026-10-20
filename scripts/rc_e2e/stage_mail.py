"""Serve one gym mail task on 127.0.0.1 for a real-desktop run through the installed app, and keep serving.

    uv run python scripts/rc_e2e/stage_mail.py <port> <seed>      (from the hands checkout)

Prints the page URL and the task's goal, then serves until killed. The page is the gym's own sandbox mail app;
its Send button sends nothing anywhere -- the page only records the reply in its state (readable at /state?run=).
"""
import json
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
from tools.gym import mail, server  # noqa: E402

port, seed = int(sys.argv[1]), int(sys.argv[2])
task = mail.make_task(seed)
rid = f"rc6-{seed}"
server.serve(port)
server.TASKS[rid] = mail.page_for(task)
url = f"http://127.0.0.1:{port}/apps/mail.html?run={rid}&seed={seed}"
print(json.dumps({"url": url, "kind": task["kind"], "goal": task["goal"], "app": task["app"], "lang": task["lang"],
                  "target": task["target"], "body": task.get("body"), "state_url": f"http://127.0.0.1:{port}/state?run={rid}"},
                 ensure_ascii=False), flush=True)
while True:
    time.sleep(3600)
