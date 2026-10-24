"""Giving the front back to the app that had it: restore, never reopen.

Every give-back -- after a flash (PeekabooDriver._give_front_back), after staging, after a step that took the front,
after a launch that took it (the executor) -- goes through restore_front. A give-back puts back the front as it was:
the app is activated without a reopen, so a window the user had minimised stays minimised and no window is made.
`open -b` is a reopen (applicationShouldHandleReopen: a minimised window comes back, deskmind#118), and AppleScript
`activate` launches an app that quit meanwhile; neither is used here. Raising the task's own app is another matter
(PeekabooDriver._raise_app).
"""
from __future__ import annotations

#: what restore_front did
GIVEN, QUIT, REFUSED = "given back", "not given back: the app is no longer running", \
    "not given back: the activation was refused"
REFUSED_AMBIGUOUS = "not given back: several running apps have that name"


def _front_pid() -> int | None:
    """The front app's pid with no accessibility wait: the window server, else NSWorkspace; None when neither tells."""
    try:
        from deskmind_hands.drivers import skylight
        pid = skylight.front_pid()
        if pid is not None:
            return pid
    except Exception:  # noqa: BLE001
        pass
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(app.processIdentifier()) if app is not None else None
    except Exception:  # noqa: BLE001
        return None


def restore_front(bundle: str, name: str = "") -> str:
    """Give `bundle` the front again -- or, only when its bundle id is not known, the one running ordinary app called
    `name` -- and say what happened (GIVEN, QUIT, REFUSED). Its running process only: one that quit is not launched
    again, and a name that matches no app or several (Xcode and Xcode-beta, many "Electron") brings nothing forward.
    Activated without a reopen (NSRunningApplication.activateWithOptions_); then the front is read (the window
    server or NSWorkspace, no accessibility wait) -- under cooperative activation (macOS 14+) activation can say yes
    and do nothing -- and when the app is not in front, it is made frontmost through accessibility (AXFrontmost)."""
    try:
        from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication, NSWorkspace
        if bundle:
            apps = [a for a in (NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle) or [])
                    if not a.isTerminated()]
        elif name:
            apps = [a for a in (NSWorkspace.sharedWorkspace().runningApplications() or [])
                    if str(a.localizedName() or "") == name and not a.isTerminated()
                    and int(a.activationPolicy()) == 0]                  # an ordinary app (the Dock's)
            if len(apps) > 1:
                return REFUSED_AMBIGUOUS
        else:
            apps = []
    except Exception:  # noqa: BLE001 -- not known: nothing is done
        return QUIT
    if not apps:
        return QUIT                                       # quit meanwhile: not launched again
    app = max(apps, key=lambda a: int(a.processIdentifier()))
    pid = int(app.processIdentifier())
    try:
        app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    except Exception:  # noqa: BLE001 -- refused: the front is read below
        pass
    if _front_pid() == pid:
        return GIVEN
    try:
        import ApplicationServices as AS
        ok = AS.AXUIElementSetAttributeValue(AS.AXUIElementCreateApplication(pid), "AXFrontmost", True) == 0
    except Exception:  # noqa: BLE001
        ok = False
    return GIVEN if ok else REFUSED

