"""Stand in for the user typing in another app: press and release Shift (a modifier, so it types nothing) about
once a second for `seconds`, counting how many presses went while each app was in front.

    python3 press_shift.py <seconds>
"""
import sys
import time
from collections import Counter

import Quartz
from AppKit import NSWorkspace

seconds = float(sys.argv[1])
fronts = Counter()
t0 = time.time()
while time.time() - t0 < seconds:
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    fronts[str(app.bundleIdentifier()) if app else "?"] += 1
    for down in (True, False):
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateKeyboardEvent(None, 56, down))
        time.sleep(0.05)
    time.sleep(0.9)
print("presses by front app:", dict(fronts), flush=True)
