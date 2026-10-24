"""Giving the front back to the app that had it: restore, never reopen.

Every give-back -- after a flash (PeekabooDriver._give_front_back), after staging, after a step that took the front,
after a launch that took it (the executor) -- goes through restore_front. A give-back puts back the front as it was:
the app is activated without a reopen, so a window the user had minimised stays minimised and no window is made.
`open -b` is a reopen (applicationShouldHandleReopen: a minimised window comes back, deskmind#118), and AppleScript
`activate` launches an app that quit meanwhile; neither is used here. Raising the task's own app is another matter
(PeekabooDriver._raise_app).
"""
from __future__ import annotations


def restore_front(bundle: str, name: str = "") -> bool:
    """Give `bundle` the front again -- or, its bundle id not known, the running app called `name` -- its running
    process only (one that quit is not launched again), activated without a reopen
    (NSRunningApplication.activateWithOptions_), else -- activation refused, as recent macOS may to a background
    process -- made frontmost through accessibility (AXFrontmost). True when either said yes."""
    if not bundle and not name:
        return False
    try:
        from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication, NSWorkspace
        apps = [a for a in (NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle) or [] if bundle
                            else []) if not a.isTerminated()]
        if not apps and name:
            apps = [a for a in (NSWorkspace.sharedWorkspace().runningApplications() or [])
                    if str(a.localizedName() or "") == name and not a.isTerminated()]
    except Exception:  # noqa: BLE001 -- not known: nothing is done
        return False
    if not apps:
        return False                                      # quit meanwhile: not launched again
    app = max(apps, key=lambda a: int(a.processIdentifier()))
    try:
        if app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps):
            return True
    except Exception:  # noqa: BLE001 -- refused: the fallback
        pass
    try:
        import ApplicationServices as AS
        return AS.AXUIElementSetAttributeValue(AS.AXUIElementCreateApplication(int(app.processIdentifier())),
                                               "AXFrontmost", True) == 0
    except Exception:  # noqa: BLE001
        return False
