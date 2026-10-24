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
#: AXFrontmost accepted, the front not readable to confirm it
UNCONFIRMED = "given back (not confirmed)"
REFUSED_AMBIGUOUS = "not given back: several running apps have that name"


def fresh_front_pid() -> int | None:
    """The front app's pid, from the one source that reports the active process itself, fresh and with no
    accessibility wait: the window server's front process (SkyLight). Never inferred -- not from window order (a user's
    app with no window or only a panel, a window ordered front without activation: review of #69 @3d0d781), not from
    NSWorkspace's frontmostApplication (stale without a run loop: @2baf2f8). None when SkyLight cannot answer:
    unknown -- a gate refuses, a give-back is not claimed confirmed."""
    try:
        from deskmind_hands.drivers import skylight
        pid = skylight.front_pid()
        return int(pid) if pid else None
    except Exception:  # noqa: BLE001
        return None


def _front_pid() -> int | None:
    return fresh_front_pid()


def restore_front(bundle: str, name: str = "") -> str:
    """Give `bundle` the front again -- or, only when its bundle id is not known, the one running ordinary app called
    `name` -- and say what happened (GIVEN, QUIT, REFUSED). Its running process only: one that quit is not launched
    again, and a name that matches no app or several (Xcode and Xcode-beta, many "Electron") brings nothing forward.
    Activated without a reopen (NSRunningApplication.activateWithOptions_); then the front is read (the window
    server or NSWorkspace, no accessibility wait) -- under cooperative activation (macOS 14+) activation can say yes
    and do nothing -- and when the app is not in front, it is made frontmost through accessibility (AXFrontmost).
    GIVEN only on a read that shows it in front; AXFrontmost accepted with the front unreadable: UNCONFIRMED."""
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
        return GIVEN                                      # confirmed by a read
    try:
        import ApplicationServices as AS
        ok = AS.AXUIElementSetAttributeValue(AS.AXUIElementCreateApplication(pid), "AXFrontmost", True) == 0
    except Exception:  # noqa: BLE001
        ok = False
    front = _front_pid()
    if front == pid:
        return GIVEN
    if ok and front is None:
        return UNCONFIRMED                                # accepted, and nothing can confirm it: said so
    return REFUSED

