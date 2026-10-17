"""Whether the Mac's GUI session can be seen and used right now, read from the window server.

A locked screen hides every window. Reading an app's accessibility tree on a locked Mac does not fail: it finds no
window, and an app with no window is a real state a planner can act on. On 0.4.3-rc.1 a request to open Safari and
search for the weather was made on a locked Mac: Safari read as having no window open, the planner said DONE on its
first step, and the run reported completed (deskmind#73). A run that reached the screen capture on a locked Mac was
told the truth ("the macOS GUI session is locked", an environment error, deskmind_hands/errors.py); the accessibility
path never captures the screen, so it never heard it.

So the session is asked directly, from the window server's own record of it (CGSessionCopyCurrentDictionary), before
an observation is taken and before an action is made. What it says is one of:

- "unlocked": the session is on the console and its screen is not locked;
- "locked": the screen is locked (CGSSessionScreenIsLocked);
- "off_console": another user's session is in front (fast user switching), so this one's screen is not shown;
- None: it could not be read (no Quartz, no GUI session for this process, no console key, a value of an unexpected
  type). Nothing is
  concluded from that, either way: the run is not stopped for it, and it is not taken as "unlocked" either.
"""
from __future__ import annotations

from .base import DriverUnavailable

#: Present, and true, while the screen is locked. Absent from the dictionary on an unlocked session.
LOCKED_KEY = "CGSSessionScreenIsLocked"
#: Quartz's kCGSessionOnConsoleKey: whether this session is the one shown on the display.
CONSOLE_KEY = "kCGSSessionOnConsoleKey"

#: What the run ends with in each state that cannot be read or acted on. Each matches an ENVIRONMENT rule in
#: deskmind_hands/errors.py, so the run ends as the machine's, the way a capture refused on a locked screen does.
MESSAGES = {
    "locked": "the macOS GUI session is locked: the screen is locked, and nothing on it can be read or acted on "
              "until it is unlocked",
    "off_console": "the macOS GUI session is not on the console: another user's session is in front, and nothing on "
                   "this one's screen can be read or acted on until it is back",
}


def _flag(value) -> bool | None:
    """A boolean from the dictionary's value (an NSNumber arrives as bool or int); None for anything else."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return None


def gui_session() -> str | None:
    """"unlocked", "locked", "off_console", or None when the session's state cannot be read."""
    try:
        import Quartz
        info = Quartz.CGSessionCopyCurrentDictionary()
        if info is None:
            return None     # this process is not in a GUI session (a daemon, an ssh shell): nothing to read
        console_key = getattr(Quartz, "kCGSessionOnConsoleKey", CONSOLE_KEY)
        raw_locked = info.get(LOCKED_KEY)
        raw_console = info.get(console_key)
    except Exception:  # noqa: BLE001 - an unreadable state is "can't tell", never a crash
        return None
    locked = False if raw_locked is None else _flag(raw_locked)
    on_console = None if raw_console is None else _flag(raw_console)
    if locked is True:
        return "locked"
    if on_console is None:
        # An unlocked session always carries the console key (True), and the lock key is absent (checked with the
        # app's bundled Python). Both keys are undocumented: a dictionary without the console key is one this code
        # does not know, and reading it as "unlocked" would bring the false success back on a macOS that renames them.
        return None
    if on_console is False:
        return "off_console"
    if locked is None:
        return None         # the key is there with a value that says nothing: can't tell
    return "unlocked"


def require_usable() -> str | None:
    """Raise DriverUnavailable when the session is locked or not on the console; otherwise return the state read
    ("unlocked", or None for can't tell)."""
    state = gui_session()
    if state in MESSAGES:
        raise DriverUnavailable(MESSAGES[state])
    return state
