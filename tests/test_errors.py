"""The one table of what becomes of each error (deskmind_hands/errors.py), pinned against the real messages it was
written for, and the loop's handling of a read that fails for a moment."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from deskmind_hands.drivers.base import DriverUnavailable   # noqa: E402
from deskmind_hands.drivers.mock import MockDriver   # noqa: E402
from deskmind_hands.errors import Kind, classify   # noqa: E402

#: Real messages from runs (09-30 sweep of 1715 run.json files and the day's traces), and what each must be.
CASES = [
    ("DriverUnavailable: see failed: Failed to capture UI state: Multiple apps match 'ai.deskmind.gymhost'. Did you "
     "mean: AppSSODaemon, AuthenticationServicesHelper (Google Chrome)", Kind.ENVIRONMENT),
    ("DriverUnavailable: see failed: Failed to capture UI state: Capture failed: Screen capture is unavailable while "
     "the macOS GUI session is locked. `screencapture` ...", Kind.ENVIRONMENT),
    ("DriverUnavailable: see failed: Failed to capture UI state: Desktop observation target changed during capture: "
     "the resolved exact window no longer matched its process-generation lane", Kind.TRANSIENT),
    ("see failed: AX tree incomplete (accessibility_enumeration_incomplete)", Kind.TRANSIENT),
    ("TimeoutExpired: Command '['tools/native/ocr', 'obs-0003.png']' timed out after 60 seconds", Kind.TRANSIENT),
    ("the user is active; the step was not carried out -- wait", Kind.USER_BUSY),
    ("Accessibility permission is not granted for this process", Kind.ENVIRONMENT),
    ("KeyError: 'window_id'", Kind.HARNESS),
]


class Table(unittest.TestCase):
    def test_every_real_message(self):
        for message, kind in CASES:
            self.assertIs(classify(message), kind, message)

    def test_an_unmatched_step_result_goes_to_the_planner(self):
        self.assertIs(classify("clicked 'Play' and nothing on screen changed", default=Kind.REJECTED), Kind.REJECTED)


class FlakyReads(MockDriver):
    """The mock desktop whose observations fail with `message` the first `n` times after the first."""

    def __init__(self, n: int, message: str):
        super().__init__(render=False)
        self.n, self.message, self.calls = n, message, 0

    def observe(self):
        self.calls += 1
        if 1 < self.calls <= 1 + self.n:
            raise DriverUnavailable(self.message)
        return super().observe()


def run(driver):
    from deskmind_hands.adapters.scripted import OracleAdapter
    from deskmind_hands.env.workspace import Workspace
    from deskmind_hands.runtime.loop import RunConfig, run_task
    from deskmind_bench.task import load_task
    task = load_task(REPO / "tasks" / "smoke" / "S01-rename.yaml")
    ws = Workspace.create(Path(tempfile.mkdtemp()), REPO / "fixtures")
    return run_task(task, driver, OracleAdapter(), ws, config=RunConfig()), ws


CHANGED = "see failed: Desktop observation target changed during capture"


class LoopReads(unittest.TestCase):
    def setUp(self):
        import deskmind_hands.runtime.loop as loop
        self._sleep, loop.time.sleep = loop.time.sleep, (lambda s: None)

    def tearDown(self):
        import deskmind_hands.runtime.loop as loop
        loop.time.sleep = self._sleep

    def test_a_read_that_fails_for_a_moment_is_made_again(self):
        res, ws = run(FlakyReads(2, CHANGED))
        self.assertEqual(res.state.value, "completed", res.failure)
        self.assertTrue((ws.ws / "final.txt").exists())

    def test_one_that_keeps_failing_is_the_environment(self):
        res, _ = run(FlakyReads(9, CHANGED))
        self.assertEqual(res.failure.cls.value, "environment")

    def test_a_locked_screen_ends_the_run_at_once(self):
        drv = FlakyReads(9, "see failed: Screen capture is unavailable while the macOS GUI session is locked")
        res, _ = run(drv)
        self.assertEqual(res.failure.cls.value, "environment")
        self.assertEqual(drv.calls, 2)                         # not read again


class OcrTimeout(unittest.TestCase):
    def test_a_reader_that_hangs_is_read_again(self):
        import subprocess
        from types import SimpleNamespace
        import deskmind_hands.vision as v
        calls = []

        def fake_run(args, **kw):
            calls.append(args)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(args, 60)
            return SimpleNamespace(returncode=0, stdout='{"text": "OK", "x": 0, "y": 0, "w": 1, "h": 1}\n')
        real_run, real_bin = v.subprocess.run, v.OCR_BIN
        v.subprocess.run, v.OCR_BIN = fake_run, Path(__file__)
        try:
            self.assertEqual([r["text"] for r in v.ocr(Path("shot.png"))], ["OK"])
        finally:
            v.subprocess.run, v.OCR_BIN = real_run, real_bin


if __name__ == "__main__":
    unittest.main()
