"""A step left undone because the user was using their Mac is not a stall: the run waits it out (D5, 09-30: a run
died as "4 identical observations" while the user typed elsewhere and every step politely waited)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.drivers.base import USER_BUSY, ExecResult   # noqa: E402
from deskmind_hands.drivers.mock import MockDriver   # noqa: E402

REPO = Path(__file__).resolve().parent.parent


class BusyDriver(MockDriver):
    """The mock desktop, with the first `busy` actions left undone as the real driver leaves them while the user is
    active -- or, with `words`, refused in other words."""

    def __init__(self, busy: int, words: str = f"{USER_BUSY}; the step was not carried out -- wait"):
        super().__init__(render=False)
        self.busy, self.words = busy, words

    def execute(self, action):
        if self.busy > 0 and action.kind.value not in ("done", "ask_user"):
            self.busy -= 1
            return ExecResult(False, self.words)
        return super().execute(action)


class Retrying:
    """The oracle's steps, each taken again while the last was left undone -- what a planner told to wait does."""

    def __init__(self):
        from deskmind_hands.adapters.scripted import OracleAdapter
        self.oracle, self.last = OracleAdapter(), None

    def propose(self, ctx):
        if self.last is not None and ctx.history and "not carried out" in (ctx.history[-1].result_detail or ""):
            return self.last
        self.last = self.oracle.propose(ctx)
        return self.last

    def usage(self):
        return {}


def run(driver):
    from deskmind_hands.env.workspace import Workspace
    from deskmind_hands.runtime.loop import RunConfig, run_task
    from deskmind_bench.task import load_task
    task = load_task(REPO / "tasks" / "smoke" / "S01-rename.yaml")
    ws = Workspace.create(Path(tempfile.mkdtemp()), REPO / "fixtures")
    return run_task(task, driver, Retrying(), ws, config=RunConfig()), ws


class Deferred(unittest.TestCase):
    def test_marked_from_the_drivers_words(self):
        self.assertTrue(ExecResult(False, f"{USER_BUSY} (in the chat app, or typing); the click was not made").deferred)
        self.assertFalse(ExecResult(False, "Click needs an element id").deferred)
        self.assertFalse(ExecResult(True, f"{USER_BUSY}? no, clicked").deferred)   # only an undone step

    def test_the_run_waits_the_user_out(self):
        res, ws = run(BusyDriver(busy=8))
        self.assertEqual(res.state.value, "completed", res.failure)
        self.assertTrue((ws.ws / "final.txt").exists())
        waited = [s for s in res.steps if s.get("deferred")]
        self.assertEqual(len(waited), 8)
        self.assertTrue(all("not carried out" in s["detail"] for s in waited))

    def test_waiting_costs_no_action_budget(self):
        busy, _ = run(BusyDriver(busy=8))
        calm, _ = run(BusyDriver(busy=0))
        self.assertEqual(busy.metrics.actions, calm.metrics.actions)

    def test_a_real_refusal_still_ends_the_run(self):
        res, _ = run(BusyDriver(busy=50, words="the element is disabled; the step was not carried out"))
        self.assertNotEqual(res.state.value, "completed")
        self.assertEqual(res.failure.cls.value, "no_progress_loop")



class IdleWithTheAppsPath(unittest.TestCase):
    """10-01: the app starts hands with PATH = its bundle, /usr/bin, /bin. `ioreg` (in /usr/sbin) was looked up on
    PATH, read nothing, and every foreground step waited for a user who was not there."""

    def test_idle_is_read_without_path(self):
        import os
        from deskmind_hands.drivers.peekaboo import hid_idle_seconds
        old = os.environ.get("PATH")
        os.environ["PATH"] = "/nonexistent"
        try:
            self.assertGreater(hid_idle_seconds(), 0.0)
        finally:
            os.environ["PATH"] = old

    def test_no_quartz_and_no_ioreg_is_not_touched(self):
        # Neither reader there (CI runs on Linux): taken as "not touched", never an exception that stops the step.
        import sys
        from unittest import mock
        from deskmind_hands.drivers.peekaboo import hid_idle_seconds
        with mock.patch.dict(sys.modules, {"Quartz": None}), \
             mock.patch("subprocess.run", side_effect=FileNotFoundError("/usr/sbin/ioreg")):
            self.assertEqual(hid_idle_seconds(), 1e9)

    def test_the_fallback_uses_ioreg_by_its_full_path(self):
        import sys
        from unittest import mock
        from deskmind_hands.drivers.peekaboo import hid_idle_seconds
        with mock.patch.dict(sys.modules, {"Quartz": None}), \
             mock.patch("subprocess.run") as run:
            run.return_value.stdout = '  |   "HIDIdleTime" = 12500000000\n'
            self.assertEqual(hid_idle_seconds(), 12.5)
            self.assertEqual(run.call_args[0][0][0], "/usr/sbin/ioreg")



class WaitsOffTheClock(unittest.TestCase):
    """10-01: gym expense runs ended "wall clock 385 s > 360 s" after a few steps, the rest spent waiting for the user
    working at the Mac. Time waited on the user is not the task's: the run's clock leaves it out, and reports it."""

    def test_a_run_that_waited_is_not_out_of_time(self):
        import time as _time

        class Waiting(MockDriver):
            def __init__(self):
                super().__init__(render=False)
                self.user_wait_s, self.waits = 0.0, 3

            def execute(self, action):
                if self.waits > 0:
                    self.waits -= 1
                    _time.sleep(0.4)
                    self.user_wait_s += 0.4
                return super().execute(action)

        from deskmind_hands.env.workspace import Workspace
        from deskmind_hands.runtime.loop import RunConfig, run_task
        from deskmind_bench.task import load_task
        task = load_task(REPO / "tasks" / "smoke" / "S01-rename.yaml")
        task.budget.wall_clock_s = 1.0
        ws = Workspace.create(Path(tempfile.mkdtemp()), REPO / "fixtures")
        d = Waiting()
        res = run_task(task, d, Retrying(), ws, config=RunConfig())
        self.assertEqual(res.state.value, "completed", res.failure)
        self.assertGreaterEqual(res.metrics.user_wait_s, 1.2)

    def test_the_driver_times_its_waits(self):
        from deskmind_hands.drivers.peekaboo import PeekabooDriver
        import time as _time
        d = PeekabooDriver.__new__(PeekabooDriver)
        d._announce_wait(False, "x", "y")
        _time.sleep(0.2)
        d._resumed()
        self.assertGreaterEqual(d.user_wait_s, 0.2)
        from deskmind_hands.drivers import peekaboo
        from tools.gym import run as gym_run
        self.assertEqual(peekaboo.WAITED_S, d.user_wait_s)          # what the gym reads between steps
        self.assertEqual(gym_run._waited(), round(d.user_wait_s, 1))


if __name__ == "__main__":
    unittest.main()
