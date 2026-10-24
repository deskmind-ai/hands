"""`performed: false` only where nothing was sent; never once input may have gone (review of hands#64, 10-24).

harness takes `performed: false` as "nothing happened" (the step is not one that may have taken effect), and anything
else as "may have happened". So a refusal before any input says so, and every point after the first input does not:
not even a later refusal, nor an exception in the reads after the action -- those are unverified, never an error.

A table of (refusal point x performed), from the driver's own paths (the address-bar search, the flash) and the
executor's. Fakes only. Every test fails on a2e423b (ExecResult had no `performed`; the address-bar refusal after
the click said "user busy, nothing done"; a read after the action that threw became an error)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deskmind_hands.actions import Action, ActionKind, Binding   # noqa: E402
from deskmind_hands.drivers import axchrome, skylight   # noqa: E402
from deskmind_hands.drivers.base import USER_BUSY, ExecResult, NothingSent, input_scope   # noqa: E402
from deskmind_hands.drivers.peekaboo import WEB_SEARCH   # noqa: E402
from deskmind_hands.geometry import Rect   # noqa: E402

import test_executor as TE   # noqa: E402
import test_web_search as WS   # noqa: E402


class AddressBar(unittest.TestCase):
    """The address-bar search: click, check the focus, cmd+A, cmd+V, check again, Return."""

    def run_case(self, case: str):
        c = WS.Carried()
        c._cleanups = []
        kw = {"background": case != "no background route", "window": case != "window not found"}
        if case == "focus elsewhere after the click":
            kw["focused"] = "WEB_FIND_FIELD"
        if case == "focus lost after the paste":
            kw["focused"] = ["WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD", "WEB_FIND_FIELD"]
        d = c.searchable(**kw)
        self.addCleanup(c.doCleanups)
        if case != "not judged":
            d.allow_browser_field(WEB_SEARCH)
        if case == "window moved":
            d._window_frame = lambda: (40, 0, 800, 600)
        if case == "no address bar":
            d._address_bar_rect = None
        if case == "user busy before anything":
            d._wait_until_still = lambda bundle, what="": f"{USER_BUSY}; the step was not carried out -- wait"
        if case == "a key refused after the click":
            def refuse(q, text, before_return=None):
                d.events.append(("paste", text))
                raise skylight.WindowKeyRefused("the user came to the app")
            d._paste_and_submit = refuse
        with input_scope():                                         # one step, as execute opens it
            res = c.act(d)
        return res, [e[0] for e in d.events if e[0] != "focus back"]

    def test_table(self):
        # (refusal point, input sent before it, performed, status the executor would give)
        table = [
            ("no address bar", [], False),
            ("not judged", [], False),
            ("no background route", [], False),
            ("window moved", [], False),
            ("user busy before anything", [], False),
            ("window not found", [], False),
            ("focus elsewhere after the click", ["click"], None),
            ("focus lost after the paste", ["click", "paste"], None),
            ("a key refused after the click", ["click", "paste"], None),
        ]
        for case, sent, performed in table:
            with self.subTest(case):
                res, events = self.run_case(case)
                self.assertEqual(events, sent, res.detail)
                self.assertIs(res.performed, performed, res.detail)
        res, _ = self.run_case("a key refused after the click")
        self.assertEqual((res.ok, res.indeterminate, res.deferred), (True, True, False))   # unverified, not "busy"


class Flash(unittest.TestCase):
    def test_refusals_before_the_front_is_taken_say_nothing_was_sent(self):
        from test_caller_deadline import APP, Clock, FlashDriver
        d = FlashDriver(Clock(), busy_s=0.0)
        with mock.patch.dict(os.environ, {"HANDS_FLASH_APPS": ""}), input_scope():
            ok, detail = d._flash(APP, lambda Q: self.fail("sent"), what="type")
            res = ExecResult(ok, detail)
        self.assertEqual((res.ok, res.performed), (False, False), detail)


class Executor(unittest.TestCase):
    def test_table(self):
        ex, stub = TE._executor()
        ex._windows = lambda every=False: [TE._win("12")]          # noqa: SLF001
        self.assertIs(ex.perform({"kind": "click", "window": TE.W12, "target": "B2"}).get("performed"), False)  # not observed
        ex.observe(TE.W12)
        self.assertIs(ex.perform({"kind": "click", "window": "com.apple.TextEdit#13", "target": "B2"}).get("performed"), False)
        self.assertIs(ex.perform({"kind": "dance", "window": TE.W12}).get("performed"), False)
        self.assertIs(ex.perform({"kind": "focus_window", "window": "com.apple.TextEdit#99"}).get("performed"), False)
        # made when the driver answers, inside the step the executor opens around it
        rows = [
            (lambda: ExecResult(False, "moved; observe again", stale=True), False),
            (lambda: ExecResult(False, "drag is not wired", unsupported=True), False),
            (lambda: ExecResult(False, f"{USER_BUSY}; the step was not carried out -- wait"), False),
            (lambda: ExecResult(False, "typing stopped after 3 of 9 chars"), None),
            (lambda: ExecResult(True, "done"), None),
            (lambda: ExecResult(True, "pressed; nothing read back", indeterminate=True), None),
        ]
        for make, performed in rows:
            with self.subTest(make().detail):
                stub.execute = lambda action, make=make: make()
                ex.observe(TE.W12)
                self.assertIs(ex.perform({"kind": "click", "window": TE.W12, "target": "B2"}).get("performed"), performed)


class AfterTheAction(unittest.TestCase):
    """Once the action is handed to the driver, the reply never says nothing went out (the reviewer's probe_after)."""

    def test_a_read_after_the_action_that_throws_is_unverified(self):
        for broken in ("_created", "_document"):
            with self.subTest(broken):
                ex, stub = TE._executor()
                ex.observe(TE.W12)

                def boom(*a, **k):
                    raise RuntimeError("AX read failed after the action")
                if broken == "_created":
                    ex._created = boom                         # noqa: SLF001
                else:
                    calls = {"n": 0}

                    def document(window, wins):
                        calls["n"] += 1
                        if calls["n"] > 1:                     # the read after, not the one before
                            raise RuntimeError("AX read failed after the action")
                        return None
                    ex._document = document                    # noqa: SLF001
                r = ex.perform({"kind": "click", "window": TE.W12, "target": "B5"})
                self.assertEqual(r["status"], "unverified", r)
                self.assertNotIn("performed", r)
                self.assertIn("may have taken effect", r["why"])
                self.assertEqual(len(stub.actions), 1)

    def test_the_driver_throwing_mid_action_is_unverified(self):
        ex, stub = TE._executor()
        ex.observe(TE.W12)

        def boom(action):
            raise RuntimeError("osascript died")
        stub.execute = boom
        r = ex.perform({"kind": "click", "window": TE.W12, "target": "B5"})
        self.assertEqual(r["status"], "unverified", r)
        self.assertNotIn("performed", r)


class AfterPartOfTheStepWent(unittest.TestCase):
    """Round 5: once a route has sent any event, a later refusal is never "nothing was done"."""

    def flash(self, sent_before_refusal: int):
        from test_caller_deadline import APP, Clock, FlashDriver
        from deskmind_hands.drivers.base import counted
        d = FlashDriver(Clock(), busy_s=0.0)
        d._background_input = lambda b: True
        d._wait_until_still = lambda b, w="": None
        d._too_late = lambda waited: None
        d._background_quartz = lambda b, observed_only=False: (object(), object())
        went = []

        def perform(Q_):
            for e in ["click", "cmd+A", "cmd+V"][:sent_before_refusal]:
                counted(lambda e=e: went.append(e))         # each event through the counted post, as _post makes it
            raise skylight.WindowKeyRefused("the user came to the app; Return not sent")
        with mock.patch.dict(os.environ, {"HANDS_FLASH_APPS": ""}), \
                mock.patch.object(skylight, "give_focus_back", lambda t: True), input_scope():
            ok, detail = d._flash(APP, perform, what="type")
            return ExecResult(ok, detail), went

    def test_the_background_route_refused_after_a_click_and_paste_is_unverified(self):
        """The reviewer's probe (rv64_probe_flash): click, cmd+A, cmd+V went, Return refused; it fell through to the
        front route, whose refusal said performed: false."""
        res, went = self.flash(3)
        self.assertEqual(went, ["click", "cmd+A", "cmd+V"])
        self.assertIsNot(res.performed, False, res.detail)
        self.assertTrue(res.indeterminate, res.detail)          # the executor says unverified

    def test_refused_before_anything_went_it_falls_through_as_before(self):
        res, went = self.flash(0)
        self.assertEqual(went, [])
        self.assertIs(res.performed, False, res.detail)         # the front route's own refusal: nothing went

    def test_ok_false_but_indeterminate_is_unverified_not_error(self):
        ex, stub = TE._executor()
        ex.observe(TE.W12)
        stub.execute = lambda action: ExecResult(False, "typing stopped after 3 of 9 chars", indeterminate=True)
        r = ex.perform({"kind": "type", "window": TE.W12, "target": "T1", "text": "123456789"})
        self.assertEqual(r["status"], "unverified", r)
        self.assertNotIn("performed", r)


class InputAtTheInstantTheListWasTaken(unittest.TestCase):
    def test_counts_and_the_window_is_not_claimed(self):
        """test_executor.CreatedWindows' flake: the user's input read at the same microsecond as the list before.
        It cannot be proved to come before the list: counted (>=), so the window that appeared is not claimed."""
        import deskmind_hands.executor as exe
        ex, stub = TE._executor()
        state = {"on": [TE._win("12")]}
        ex._windows = lambda every=False: list(state["on"])     # noqa: SLF001
        ex.observe(TE.W12)

        def execute(action):
            state["on"].append(TE._win("14", title="未命名-9"))
            return ExecResult(True, "done")
        stub.execute = execute
        ex._text = lambda w: ""                                 # noqa: SLF001
        ex._document = lambda window, wins: (None, False) if window.endswith("#14") else None   # noqa: SLF001
        clock = mock.Mock()
        clock.time = lambda: 1000.0
        clock.sleep = lambda s: None
        ex._last_input_at = lambda: 1000.0                      # noqa: SLF001 -- the same instant as `since`
        with mock.patch.object(exe, "time", clock):
            r = ex.perform({"kind": "click", "window": TE.W12, "target": "B2"})
        self.assertNotIn("new_window", r)


class Helpers(unittest.TestCase):
    def test_the_executor_helper_reads_no_real_windows(self):
        """test_executor._executor(): no window server and no `peekaboo app list` (it could hang the suite)."""
        import deskmind_hands.executor as exe
        with mock.patch.object(exe, "Quartz", None), \
                mock.patch("deskmind_hands.executor.subprocess.run", side_effect=AssertionError("ran a real command")):
            ex, _ = TE._executor()
            self.assertEqual(ex._windows(), [])                     # noqa: SLF001
            self.assertIsNone(ex._pk(["app", "list", "--json"]))     # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
