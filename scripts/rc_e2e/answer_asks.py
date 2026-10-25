"""Stand in for the user at DeskMind's cards for a run submitted over hands.sock (the app's own UI shows no card for
such a run). For every approval ask in the run's event log: a real mouse click on DeskMind's window first (its
title bar: input the window server routes to DeskMind, as the user's click on a card is) -- only while DeskMind is
already the front app (read fresh: drivers/front.fresh_front_pid) and its window the frontmost one there: a click on
its title bar while another app is in front would activate it (hands#70 review). This script never brings DeskMind forward (rc.16: activating it put
DeskMind in the run's front-app record, which acceptance requires to be 0 for DeskMind) -- then the helper's `answer`
op with approve=true, what the app sends after that click. Stops at the run's done event.

    python3 answer_asks.py <run log> <timeout_s>
"""
import json
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

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


def title_bar_point(pid, front_pid, wins):
    """Where to click DeskMind's window (pid `pid`), or None: only when the front app is DeskMind (`front_pid`, read
    fresh; None -- unknown -- is not DeskMind) and DeskMind's window is the frontmost one at its title bar in `wins`
    (the on-screen window list, front to back)."""
    if front_pid is None or front_pid != pid:
        return None
    wins = [w for w in wins if w.get("kCGWindowLayer", 0) == 0]
    mine = next((w for w in wins if w.get("kCGWindowOwnerPID") == pid), None)
    if mine is None:
        return None
    b = mine["kCGWindowBounds"]
    x, y = b["X"] + b["Width"] / 2, b["Y"] + 12
    front = next((w for w in wins if w["kCGWindowBounds"]["X"] <= x <= w["kCGWindowBounds"]["X"] + w["kCGWindowBounds"]["Width"]
                  and w["kCGWindowBounds"]["Y"] <= y <= w["kCGWindowBounds"]["Y"] + w["kCGWindowBounds"]["Height"]), None)
    return (x, y) if front is not None and front.get("kCGWindowOwnerPID") == pid else None


def deskmind_title_bar():
    """A point on DeskMind's title bar to click (title_bar_point), the front app and the window list read fresh each
    try -- never by bringing DeskMind forward (K1: nothing here activates it). A click that would land on another
    app's window, or on DeskMind's while another app is in front, is the user's input to that app or an activation
    (10-20: the task's Safari window covered the title bar and the 'approval' click went to Safari): no point then,
    and the ask is answered without the click."""
    import Quartz
    from AppKit import NSRunningApplication
    from deskmind_hands.drivers.front import fresh_front_pid
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_("ai.deskmind.app")
    if not apps:
        raise RuntimeError("DeskMind is not running")
    pid = apps[0].processIdentifier()
    for _ in range(20):
        time.sleep(0.15)
        wins = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, 0)
        pt = title_bar_point(pid, fresh_front_pid(), wins)
        if pt is not None:
            return pt
    raise RuntimeError("DeskMind is not the front app with its window frontmost at its title bar")


def real_click(x, y):
    import Quartz
    pt = Quartz.CGPointMake(x, y)
    for t in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(None, t, pt, Quartz.kCGMouseButtonLeft))
        time.sleep(0.06)


def main(log, timeout):
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


if __name__ == "__main__":
    main(sys.argv[1], float(sys.argv[2]))
