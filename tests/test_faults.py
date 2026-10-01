"""The test-only fault switchboard (HANDS_FAULTS): each fault fires where it is told, once, and the harness's own
handling applies to it -- a busy user is waited for, a failed read is read again, a ghost window is not offered."""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deskmind_hands.drivers.peekaboo as pk   # noqa: E402
from deskmind_hands.drivers.peekaboo import PeekabooDriver, _Faults, _transient_read   # noqa: E402


class Switchboard(unittest.TestCase):
    def test_parsing_and_firing(self):
        f = _Faults("user_busy@3, capture_error@2,ghost_window@1")
        self.assertEqual(f.at, {"user_busy": 3, "capture_error": 2, "ghost_window": 1})
        self.assertFalse(f.once("capture_error", 1))
        self.assertTrue(f.once("capture_error", 2))
        self.assertFalse(f.once("capture_error", 2))           # once
        self.assertFalse(f.since("ghost_window", 0))
        self.assertTrue(f.since("ghost_window", 1) and f.since("ghost_window", 7))
        f.observed(2)
        self.assertEqual(f.busy_until, 0.0)
        f.observed(3)
        self.assertGreater(f.busy_until, time.time())
        self.assertFalse(_Faults("").at)
        f.reset()                                              # a new run: they fire again
        self.assertTrue(f.once("capture_error", 2))
        self.assertEqual(f.busy_until, 0.0)

    def test_the_harness_handles_what_each_fault_does(self):
        real = pk.FAULTS
        try:
            pk.FAULTS = _Faults("user_busy@1")
            pk.FAULTS.observed(1)
            self.assertEqual(PeekabooDriver._hid_idle(), 0.0)      # the user is "busy": the driver waits
        finally:
            pk.FAULTS = real
        self.assertTrue(_transient_read("Failed to capture UI state: Desktop observation target changed during "
                                        "capture (injected fault)"))  # read again, not a harness failure
        self.assertTrue(PeekabooDriver._ghost(dict(pk._GHOST)))         # not offered as a window



class OcrRetry(unittest.TestCase):
    def test_a_reader_that_dies_once_is_run_again(self):
        import deskmind_hands.vision as v
        from types import SimpleNamespace
        calls = []

        def fake_run(args, **kw):
            calls.append(args)
            if len(calls) == 1:
                return SimpleNamespace(returncode=1, stdout="")          # e5rt error on first use
            return SimpleNamespace(returncode=0, stdout='{"text": "TOTAL 37.26", "x": 0, "y": 0, "w": 1, "h": 1}\n')
        real_run, real_bin = v.subprocess.run, v.OCR_BIN
        v.subprocess.run, v.OCR_BIN = fake_run, Path(__file__)             # any existing file stands in for it
        try:
            rows = v.ocr(Path("shot.png"))
        finally:
            v.subprocess.run, v.OCR_BIN = real_run, real_bin
        self.assertEqual([r["text"] for r in rows], ["TOTAL 37.26"])
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
