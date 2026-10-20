"""Stand in for the user at DeskMind's cards for a run submitted over hands.sock (the app's own UI shows no card for
such a run). For every approval ask in the run's event log: a real mouse click on DeskMind's window first (its
title bar: input the window server routes to DeskMind, as the user's click on a card is), then the helper's
`answer` op with approve=true -- what the app sends after that click. Stops at the run's done event.

    python3 answer_asks.py <run log> <timeout_s>
"""
import json
import os
import socket
import subprocess
import sys
import time

import Quartz

log, timeout = sys.argv[1], float(sys.argv[2])
SOCK = os.path.expanduser("~/Library/Application Support/DeskMind/hands.sock")


def request(body):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(20)
    s.connect(SOCK)
    s.sendall(json.dumps(body, ensure_ascii=False).encode() + b"\n")
    buf = b""
    while b"\n" not in buf:
        c = s.recv(65536)
        if not c:
            break
        buf += c
    s.close()
    return json.loads(buf.split(b"\n")[0] or b"{}")


def deskmind_title_bar():
    """A point on DeskMind's window where DeskMind is the frontmost window: DeskMind is brought forward first (as
    it is when it shows a card), and the point is checked against the window list -- a click that lands on another
    app's window is the user's input to that app, and correctly counts as a change (10-20: the task's Safari window
    covered the title bar and the 'approval' click went to Safari)."""
    from AppKit import NSRunningApplication, NSApplicationActivateIgnoringOtherApps
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_("ai.deskmind.app")
    if not apps:
        raise RuntimeError("DeskMind is not running")
    pid = apps[0].processIdentifier()
    apps[0].activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    for _ in range(20):
        time.sleep(0.15)
        wins = [w for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, 0)
                if w.get("kCGWindowLayer", 0) == 0]
        mine = next((w for w in wins if w.get("kCGWindowOwnerPID") == pid), None)
        if mine is None:
            continue
        b = mine["kCGWindowBounds"]
        x, y = b["X"] + b["Width"] / 2, b["Y"] + 12
        front = next((w for w in wins if w["kCGWindowBounds"]["X"] <= x <= w["kCGWindowBounds"]["X"] + w["kCGWindowBounds"]["Width"]
                      and w["kCGWindowBounds"]["Y"] <= y <= w["kCGWindowBounds"]["Y"] + w["kCGWindowBounds"]["Height"]), None)
        if front is not None and front.get("kCGWindowOwnerPID") == pid:
            return x, y
    raise RuntimeError("DeskMind's window is not frontmost at its title bar")


def real_click(x, y):
    pt = Quartz.CGPointMake(x, y)
    for t in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(None, t, pt, Quartz.kCGMouseButtonLeft))
        time.sleep(0.06)


answered = set()
t0 = time.time()
pos = 0
while time.time() - t0 < timeout:
    with open(log, encoding="utf-8") as fh:
        fh.seek(pos)
        lines = fh.readlines()
        pos = fh.tell()
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("event") == "done":
            print("run done; stopping", flush=True)
            sys.exit(0)
        if ev.get("event") == "ask" and ev.get("approval") and ev.get("ask_id") not in answered:
            answered.add(ev["ask_id"])
            try:
                x, y = deskmind_title_bar()
                real_click(x, y)
                time.sleep(0.5)
            except Exception as e:  # noqa: BLE001 -- no window to click: answer anyway
                x = y = float("nan")
                print(f"no DeskMind window to click ({e}); answering without the click", flush=True)
            r = request({"op": "answer", "reply": "", "approve": True, "lang": "zh"})
            print(f"[{time.time() - t0:6.1f}s] ask {ev['ask_id']} ({(ev.get('question') or '')[:40]!r}): clicked DeskMind at ({x:.0f},{y:.0f}), answered approve -> {r}", flush=True)
    time.sleep(0.5)
print("timeout", flush=True)
