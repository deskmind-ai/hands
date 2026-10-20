"""Stand in for the user typing in another app: before each press, bring that app forward (the terminal unless
told otherwise), then press and release Shift (a modifier, so it types nothing) about once a second for
`seconds`. Prints how many presses went while each app was in front -- presses that landed in the task's own app
say nothing about #49.

    python3 scripts/rc_e2e/press_shift.py <seconds> [bundle id]      (default com.apple.Terminal)

Whether the run's hands driver could hear the keys at all depends on DeskMind Hands' Input Monitoring permission,
which this script cannot read (permissions are per process); `summarize.py` prints `input_watch` from the run's
trace: "keys" is what this check needs, "mouse" means no Input Monitoring (every key press counts, the check is
void), "" means no tap at all.
"""
import subprocess
import sys
import time
from collections import Counter

import Quartz
from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication, NSWorkspace

seconds = float(sys.argv[1])
bundle = sys.argv[2] if len(sys.argv) > 2 else "com.apple.Terminal"
if not NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle):
    subprocess.run(["open", "-b", bundle], capture_output=True, timeout=20)
    time.sleep(2)
fronts = Counter()
t0 = time.time()
while time.time() - t0 < seconds:
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle)
    if apps:
        apps[0].activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        time.sleep(0.15)
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    fronts[str(app.bundleIdentifier()) if app else "?"] += 1
    for down in (True, False):
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateKeyboardEvent(None, 56, down))
        time.sleep(0.05)
    time.sleep(0.8)
print(f"presses by front app (asked for {bundle}):", dict(fronts), flush=True)
if fronts.get(bundle, 0) < sum(fronts.values()) / 2:
    print(f"WARNING: fewer than half the presses went to {bundle}; the check says little", flush=True)
