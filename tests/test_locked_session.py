"""A locked Mac ends the run as the environment's, before the planner sees anything (deskmind#73).

On 0.4.3-rc.1 a request to open Safari and search was made on a locked Mac. The accessibility read found Safari with no
window open, the planner said DONE on its first step, and the run reported completed. The driver now asks the window
server whether the session is locked before every observation and every action (drivers/session.py). Quartz is not
in the test environment: it is stood in for through sys.modules, with the dictionary CGSessionCopyCurrentDictionary
returns.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from deskmind_hands.actions import Action, ActionKind   # noqa: E402
from deskmind_hands.adapters.base import Proposal   # noqa: E402
from deskmind_hands.drivers import session   # noqa: E402
from deskmind_hands.drivers.base import DriverUnavailable   # noqa: E402
from deskmind_hands.errors import Kind, classify   # noqa: E402

LOCKED = {"CGSSessionScreenIsLocked": True, "kCGSSessionOnConsoleKey": True}
UNLOCKED = {"kCGSSessionOnConsoleKey": True}           # the lock key is absent on an unlocked session


def quartz(read):
    """A Quartz whose CGSessionCopyCurrentDictionary returns read() (or raises what read() raises)."""
    return types.SimpleNamespace(CGSessionCopyCurrentDictionary=read, kCGSessionOnConsoleKey="kCGSSessionOnConsoleKey")


def with_session(info):
    return mock.patch.dict(sys.modules, {"Quartz": quartz(lambda: info)})


class ReadingTheSession(unittest.TestCase):
    def test_states(self):
        for info, state in [
            (LOCKED, "locked"),
            ({"CGSSessionScreenIsLocked": 1}, "locked"),
            (UNLOCKED, "unlocked"),
            ({"CGSSessionScreenIsLocked": 0, "kCGSSessionOnConsoleKey": 1}, "unlocked"),
            ({"kCGSSessionOnConsoleKey": False}, "off_console"),
            ({"CGSSessionScreenIsLocked": "maybe", "kCGSSessionOnConsoleKey": True}, None),   # says nothing
        ]:
            with with_session(info):
                self.assertEqual(session.gui_session(), state, info)

    def test_a_dictionary_without_the_console_key_is_cant_tell(self):
        # The keys are undocumented. A dictionary this code does not know -- empty, or a macOS that renamed them --
        # is never read as unlocked: that would bring the false success back.
        for info in ({}, {"CGSSessionScreenIsLocked": False}, {"kCGSSessionUserNameKey": "someone"}):
            with with_session(info):
                self.assertIsNone(session.gui_session(), info)
                self.assertIsNone(session.require_usable(), info)      # nothing raised either

    def test_an_unreadable_session_is_cant_tell_never_unlocked(self):
        def boom():
            raise RuntimeError("window server unavailable")
        for q in (None,                                  # no Quartz at all: the import fails
                  quartz(lambda: None),                  # no GUI session for this process
                  quartz(boom),
                  types.SimpleNamespace()):              # a Quartz without the call
            with mock.patch.dict(sys.modules, {"Quartz": q}):
                self.assertIsNone(session.gui_session())
                self.assertIsNone(session.require_usable())       # nothing raised

    def test_a_locked_or_switched_session_is_the_environment(self):
        for info in (LOCKED, {"kCGSSessionOnConsoleKey": False}):
            with with_session(info), self.assertRaises(DriverUnavailable) as cm:
                session.require_usable()
            self.assertIs(classify(f"DriverUnavailable: {cm.exception}"), Kind.ENVIRONMENT)
        self.assertIn("screen is locked", session.MESSAGES["locked"])


class Planner:
    """Says `first` on its first turn and DONE after; `on_turn` runs before each answer."""

    name = "planner"

    def __init__(self, first=None, on_turn=None):
        self.turns, self.first, self.on_turn = 0, first, on_turn

    def propose(self, ctx):
        self.turns += 1
        if self.on_turn:
            self.on_turn(self.turns)
        if self.turns == 1 and self.first is not None:
            return Proposal(action=self.first, raw_text="first")
        return Proposal(action=Action(kind=ActionKind.DONE), raw_text="done")

    def usage(self):
        return {}


def safari_with_no_window(scratch: Path):
    """The live driver as the 0.4.3-rc.1 run met it: Safari answers every read with no window. Nothing reaches the Mac:
    Peekaboo, AppleScript and the clipboard are stood in for, and each call is recorded."""
    from deskmind_hands.drivers.peekaboo import PeekabooDriver
    with mock.patch.dict(os.environ, {"HANDS_PERMISSIONS_VERIFIED": "1"}):
        d = PeekabooDriver(app="com.apple.Safari", binary=sys.executable, scratch=scratch)
    d.calls = []

    def run(key, _extra=None, **fmt):
        d.calls.append(("peekaboo", key))
        return False, {}, "WINDOW_NOT_FOUND: no window of Safari"

    def osa(script):
        d.calls.append(("osascript", script[:40]))
        return True, ""
    d._run, d._osa = run, osa
    d.clipboard = lambda: d.calls.append(("clipboard", "read")) or ""
    d._set_clipboard = lambda value: d.calls.append(("clipboard", "write"))
    d._frontmost = lambda: "Finder"
    d._front_app = lambda: ("Finder", "com.apple.finder")   # hands#62: the front by name and bundle id
    return d


def run_live(planner, sessions):
    """The goal of the 0.4.3-rc.1 run, through the loop, with the session reading each of `sessions` in turn (the
    last one from then on), or whatever `sessions` returns when it is a function."""
    from deskmind_hands.env.workspace import LiveWorkspace
    from deskmind_hands.live import live_task
    from deskmind_hands.runtime.loop import RunConfig, run_task
    scratch = Path(tempfile.mkdtemp())
    (scratch / "ws").mkdir()
    ws = LiveWorkspace(root=scratch, ws=scratch / "ws", fixtures_dir=scratch)
    task = live_task("打开 Safari，搜索新加坡的天气", None, app="com.apple.Safari", max_actions=10, wall_clock_s=60)
    reads = [sessions] if callable(sessions) else list(sessions)

    def read():
        info = reads.pop(0) if len(reads) > 1 else reads[0]
        if callable(info):
            info = info()
        if isinstance(info, Exception):
            raise info
        return info
    env = {k: v for k, v in os.environ.items() if k not in ("HANDS_DONE_CHECK", "HANDS_TRACK_GOAL")}
    with mock.patch.dict(sys.modules, {"Quartz": quartz(read)}), mock.patch.dict(os.environ, env, clear=True):
        driver = safari_with_no_window(scratch)
        try:
            res = run_task(task, driver, planner, ws, config=RunConfig())
        finally:
            driver.close()
    return res, driver


class LockedRun(unittest.TestCase):
    def test_a_locked_screen_is_the_environment_not_completed(self):
        planner = Planner()
        res, driver = run_live(planner, [LOCKED])
        self.assertEqual(res.state.value, "errored")
        self.assertEqual(res.failure.cls.value, "environment", res.failure)
        self.assertIn("screen is locked", res.failure.detail)
        self.assertEqual(planner.turns, 0)                 # the planner never saw the empty screen
        # Nothing staged, read or done on the locked Mac (the grader's own read of the clipboard aside).
        self.assertEqual([c for c in driver.calls if c != ("clipboard", "read")], [])

    def test_a_screen_locked_before_an_action_stops_it(self):
        locked_now = []
        planner = Planner(first=Action(kind=ActionKind.WAIT, duration_s=0.1),
                          on_turn=lambda n: locked_now.append(True))

        def read():
            return LOCKED if locked_now else UNLOCKED
        res, _ = run_live(planner, read)
        self.assertEqual(res.failure.cls.value, "environment", res.failure)
        self.assertEqual(res.metrics.actions, 0)
        self.assertEqual(planner.turns, 1)

    def test_locked_at_start_stays_refused_after_an_unlock(self):
        # Nothing was staged, so the run is not let go on just because the screen came back.
        planner = Planner()
        res, driver = run_live(planner, [LOCKED, UNLOCKED])
        self.assertEqual(res.failure.cls.value, "environment", res.failure)
        self.assertIn("nothing was staged", res.failure.detail)
        self.assertEqual(planner.turns, 0)

    def test_each_run_counts_its_own_unread_states(self):
        # The driver is reused across a task set's runs: a count left from the last run is not this one's.
        scratch = Path(tempfile.mkdtemp())
        with mock.patch.dict(sys.modules, {"Quartz": None}):
            d = safari_with_no_window(scratch)
            d.start(scratch)
            d.observe()
        self.assertGreaterEqual(d.state()["session_unread"], 1)
        with with_session(LOCKED):
            d.start(scratch)
        self.assertEqual(d.state()["session_unread"], 0)

    def test_an_unlocked_session_is_unaffected(self):
        planner = Planner()
        res, driver = run_live(planner, [UNLOCKED])
        self.assertEqual(res.state.value, "completed", res.failure)
        self.assertEqual(planner.turns, 1)
        # Safari is read only by the id of a window the run may use, never by app (the front window can be the user's).
        self.assertIn(("peekaboo", "window_list"), driver.calls)
        self.assertNotIn(("peekaboo", "see"), driver.calls)
        self.assertEqual(driver.state()["session_unread"], 0)

    def test_an_unreadable_session_does_not_crash_or_stop_the_run(self):
        for unreadable in (None, RuntimeError("window server unavailable"), {"CGSSessionScreenIsLocked": "?"}, {}):
            planner = Planner()
            res, driver = run_live(planner, [unreadable])
            self.assertNotIn(res.failure.cls.value, ("harness_bug", "environment"), (unreadable, res.failure))
            self.assertEqual(planner.turns, 1, unreadable)          # the observation went through
            self.assertGreaterEqual(driver.state()["session_unread"], 1, unreadable)


if __name__ == "__main__":
    unittest.main()
