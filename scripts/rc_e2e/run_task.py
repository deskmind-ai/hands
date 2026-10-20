"""Submit one task to the installed DeskMind helper over hands.sock (engine: harness) and log every streamed event.

    DM_CONFIG=ds-flash python3 run_task.py <log file> <task folder or -> <goal> ['<apps json>']

Each event line is appended to the log as it arrives; the last line is the helper's done event. An `ask` event is
printed with its id so it can be answered with the helper's `answer` op from another shell."""
import json
import os
import socket
import sys
import time

log, folder, goal = sys.argv[1], sys.argv[2], sys.argv[3]
apps = json.loads(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else [{"name": "文本编辑", "bundle": "com.apple.TextEdit"}]
config = os.environ.get("DM_CONFIG", "ds-flash")   # ds-flash (DeepSeek) / ark-evolving (Volcengine); keys come from the app's models.env
req = {"op": "run", "engine": "harness", "goal": goal, "lang": "zh", "config": config,
       "foreground_ok": True, "live_view": False, "apps": apps}
if folder and folder != "-":
    req["folder"] = folder
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(os.path.expanduser("~/Library/Application Support/DeskMind/hands.sock"))
s.sendall(json.dumps(req, ensure_ascii=False).encode() + b"\n")
t0 = time.time()
buf = b""
with open(log, "a", encoding="utf-8") as out:
    out.write(json.dumps({"_sent": req, "_at": t0}, ensure_ascii=False) + "\n"); out.flush()
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                ev = {"_raw": line.decode("utf-8", "replace")}
            ev["_t"] = round(time.time() - t0, 1)
            out.write(json.dumps(ev, ensure_ascii=False) + "\n"); out.flush()
            kind = ev.get("event")
            brief = {k: ev[k] for k in ("n", "human", "status", "harness_status", "outcome", "error", "ask_id", "question", "text", "why") if k in ev}
            print(f"[{ev['_t']:6.1f}s] {kind}: {json.dumps(brief, ensure_ascii=False)[:300]}", flush=True)
            if kind == "done":
                print("DONE", json.dumps({k: v for k, v in ev.items() if not k.startswith('_')}, ensure_ascii=False)[:1500], flush=True)
print("stream closed after %.0fs" % (time.time() - t0))
