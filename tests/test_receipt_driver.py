"""The driver's side of the files receipt: the digest is of the content written, the scratch is never reported, and
the command-key path keeps a scripted save's receipt (PR #48 review). unittest, as CI collects it."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from deskmind_hands.actions import Action, ActionKind, Binding
from deskmind_hands.drivers.base import ExecResult
from deskmind_hands.drivers.peekaboo import PeekabooDriver


def _driver(ws: Path):
    d = PeekabooDriver.__new__(PeekabooDriver)
    d.app, d.ws, d._snapshot = "com.apple.TextEdit", ws, "s1"
    return d


class Receipt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_receipt_digests_what_was_written_not_what_is_on_disk_later(self):
        d = _driver(self.ws)
        f = self.ws / "hello.txt"
        f.write_bytes(b"hello")
        self.assertEqual(d._receipt(str(f), b"hello"), ((str(f.resolve()), hashlib.sha256(b"hello").hexdigest()),))
        # without the content, the path alone: never the file read back from disk (PR #16 review, eleventh round)
        self.assertEqual(d._receipt(str(f), None), ((str(f.resolve()), None),))
        # a sync program rewrote it before the receipt: the digest of what the driver wrote no longer matches the file
        f.write_bytes(b"someone else's")
        self.assertNotEqual(d._receipt(str(f), b"hello")[0][1], hashlib.sha256(b"someone else's").hexdigest())
        # relative, or the driver's own scratch: no receipt
        self.assertEqual(d._receipt("hello.txt", b"x"), ())
        scratch = self.ws / ".hands" / "未命名-1"
        scratch.parent.mkdir()
        scratch.write_bytes(b"")
        self.assertEqual(d._receipt(str(scratch), b""), ())

    def test_the_command_key_path_keeps_the_scripted_saves_receipt(self):
        d = _driver(self.ws)
        d._mcp = object()
        d._chord_repeat = {}
        receipt = ((str(self.ws / "hello.txt"), hashlib.sha256(b"x").hexdigest()),)
        d._scripted_command = lambda chord: ExecResult(True, "saved 'hello.txt' (scripted, in the background)", files=receipt)
        r = d._execute_inner(Action(kind=ActionKind.KEY, keys=("cmd", "s"), binding=Binding()))
        self.assertTrue(r.ok)
        self.assertEqual(r.files, receipt)


class Indeterminate(unittest.TestCase):
    def test_a_click_peekaboo_could_not_read_back_is_dispatched_not_failed(self):
        """Peekaboo 4.7: 'The click mutation returned without valid canonical result semantics. Observe the
        intended target before retrying...' -- the click may have landed, so it is unverified, never an error that
        invites a second click (rc.8, 10-20)."""
        d = PeekabooDriver.__new__(PeekabooDriver)
        d.app = "com.apple.Safari"
        r = d._settle(False, "The click mutation returned without valid canonical result semantics. Observe the "
                             "intended target before retrying and update the runtime host.")
        self.assertTrue(r.ok)
        self.assertTrue(r.indeterminate)
        self.assertIn("could not be confirmed", r.detail)


class StopInTheDriver(unittest.TestCase):
    """The app's stop reaches the driver's own waits: a flash waiting for a still Mac, the background route's wait,
    and a pixel click whose target was being placed send nothing (hands #54 review)."""

    def setUp(self):
        from deskmind_hands.grounding import STOP
        self.STOP = STOP
        STOP.clear()
        self.addCleanup(STOP.clear)

    def _driver(self):
        d = PeekabooDriver.__new__(PeekabooDriver)
        d.app = "com.example.player"
        d._takeover = lambda: False
        d._resumed = lambda: None
        d._announce_wait = lambda waited, bundle, what: True
        d._front_bundle = lambda: "com.apple.Terminal"
        d._background_input = lambda bundle: False
        d.BROWSERS = set()
        return d

    def test_a_flash_waiting_for_a_still_mac_ends_on_stop_and_sends_nothing(self):
        import os as _os
        from unittest import mock
        d = self._driver()
        d._hid_idle = lambda: 0.0                  # the user is moving the mouse (to click stop)
        sent = []
        self.STOP.set()
        with mock.patch.dict(_os.environ, {"HANDS_FLASH_APPS": "com.example.player"}):
            ok, why = d._flash("com.example.player", lambda Q: sent.append("click") or "clicked")
        self.assertFalse(ok)
        self.assertIn("stopped", why)
        self.assertEqual(sent, [])

    def test_the_background_routes_wait_ends_on_stop(self):
        d = self._driver()
        d._hid_idle = lambda: 0.0
        self.STOP.set()
        self.assertIn("stopped", d._wait_until_still("com.example.player"))


class BrowserForeground(unittest.TestCase):
    """A browser comes forward only for the run's own window, when the step is allowed to, and nothing is sent
    unless that window is the one in front (rc.10, 10-21)."""

    def _driver(self, own: bool):
        from deskmind_hands.grounding import STOP
        STOP.clear()
        d = PeekabooDriver.__new__(PeekabooDriver)
        d.app = "com.google.Chrome"
        d._window_id = "61"
        d._run_windows = {"61"} if own else set()
        d._takeover = lambda: True                 # the user handed the screen over: no wait
        d._resumed = lambda: None
        d._background_input = lambda bundle: False
        d.BROWSERS = {"com.google.chrome"}
        d.front = ["com.googlecode.iterm2"]        # the user's terminal is in front
        d._front_bundle = lambda: d.front[0]
        d._hid_idle = lambda: 100.0
        d._foreground_actions = 0
        d._browser_front_sid = lambda bundle: None
        d.given_back = []

        def give_back(prev):
            d.given_back.append(prev)
            d.front[0] = prev
        d._give_front_back = give_back
        d.brought = []

        def bring(bundle, browser):
            d.brought.append((bundle, browser))
            d.front[0] = bundle
            return True
        d._bring_forward = bring
        return d

    def test_not_the_runs_own_window_or_not_allowed_nothing_comes_forward(self):
        import os as _os
        from unittest import mock
        for own, allowed in ((False, "com.google.Chrome"), (True, "")):
            d = self._driver(own)
            with mock.patch.dict(_os.environ, {"HANDS_FLASH_APPS": allowed}):
                ok, why = d._flash("com.google.Chrome", lambda Q: "typed")
            self.assertFalse(ok)
            self.assertIn("only for the run's own window", why)
            self.assertEqual(d.brought, [])

    def test_the_runs_own_window_comes_forward_and_nothing_is_sent_unless_it_is_in_front(self):
        import os as _os
        import sys as _sys
        import types
        from unittest import mock
        q = types.ModuleType("Quartz")
        q.CGEventGetLocation = lambda e: (0, 0)
        q.CGEventCreate = lambda x: None
        q.CGWarpMouseCursorPosition = lambda p: None
        q.CGAssociateMouseAndMouseCursorPosition = lambda b: None
        sent = []
        for in_front, expect in ((False, False), (True, True)):
            d = self._driver(True)
            d._own_window_in_front = lambda bundle, f=in_front: f
            with mock.patch.dict(_os.environ, {"HANDS_FLASH_APPS": "com.google.Chrome"}), \
                 mock.patch.dict(_sys.modules, {"Quartz": q}), \
                 mock.patch("deskmind_hands.drivers.peekaboo._ascii_input_source", return_value=types.SimpleNamespace(restore=lambda: None)), \
                 mock.patch("deskmind_hands.drivers.peekaboo.time.sleep"):
                ok, _ = d._flash("com.google.Chrome", lambda Q: sent.append(in_front) or "typed")
            self.assertEqual(ok, expect)
            self.assertEqual(d.brought, [("com.google.Chrome", True)])
        self.assertEqual(sent, [True])                 # only when the run's own window was in front

    def test_not_while_the_user_is_in_that_browser(self):
        """Raising the run's window in the browser the user is reading would cover their page (hands #55 review)."""
        import os as _os
        import sys as _sys
        import types
        from unittest import mock
        d = self._driver(True)
        d._takeover = lambda: False
        d.front[0] = "com.google.Chrome"
        d._announce_wait = lambda waited, bundle, what: True
        with mock.patch.dict(_os.environ, {"HANDS_FLASH_APPS": "com.google.Chrome", "HANDS_FLASH_WAIT_S": "0"}), \
             mock.patch.dict(_sys.modules, {"Quartz": types.ModuleType("Quartz")}), \
             mock.patch("deskmind_hands.drivers.peekaboo.time.sleep"):
            ok, why = d._flash("com.google.Chrome", lambda Q: "typed")
        self.assertFalse(ok)
        self.assertEqual(d.brought, [])

    def test_a_browser_gets_the_front_back_without_a_reopen(self):
        """`open -b` on a browser is a reopen and can open a window of its own (deskmind#73): the front goes back to
        a browser through accessibility only (hands #55 review)."""
        from unittest import mock
        from deskmind_hands.drivers.peekaboo import PeekabooDriver
        d = PeekabooDriver.__new__(PeekabooDriver)
        d.BROWSERS = {"com.google.chrome"}
        made = []
        d._make_frontmost = lambda b: made.append(b) or True
        fronts = iter(["x", "com.google.Chrome"])
        d._front_bundle = lambda: next(fronts, "com.google.Chrome")
        with mock.patch("deskmind_hands.drivers.peekaboo.subprocess.run") as run, \
             mock.patch("deskmind_hands.drivers.peekaboo.time.sleep"):
            d._give_front_back("com.google.Chrome")
        run.assert_not_called()
        self.assertEqual(made[0], "com.google.Chrome")


if __name__ == "__main__":
    unittest.main()
