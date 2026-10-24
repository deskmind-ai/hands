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
from deskmind_hands.drivers.base import USER_BUSY, ExecResult, NothingSent   # noqa: E402
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
        with mock.patch.dict(os.environ, {"HANDS_FLASH_APPS": ""}):
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
        rows = [
            (ExecResult(False, "moved; observe again", stale=True), False),
            (ExecResult(False, "drag is not wired", unsupported=True), False),
            (ExecResult(False, f"{USER_BUSY}; the step was not carried out -- wait"), False),
            (ExecResult(False, "typing stopped after 3 of 9 chars"), None),
            (ExecResult(True, "done"), None),
            (ExecResult(True, "pressed; nothing read back", indeterminate=True), None),
        ]
        for res, performed in rows:
            with self.subTest(res.detail):
                stub.execute = lambda action, res=res: res
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
        d = FlashDriver(Clock(), busy_s=0.0)
        d._background_input = lambda b: True
        d._wait_until_still = lambda b, w="": None
        d._too_late = lambda waited: None

        class Q:
            sent = 0

        q = Q()
        d._background_quartz = lambda b, observed_only=False: (q, object())
        went = []

        def perform(Q_):
            for e in ["click", "cmd+A", "cmd+V"][:sent_before_refusal]:
                went.append(e)
                q.sent += 1
            raise skylight.WindowKeyRefused("the user came to the app; Return not sent")
        with mock.patch.dict(os.environ, {"HANDS_FLASH_APPS": ""}), \
                mock.patch.object(skylight, "give_focus_back", lambda t: True):
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

    def test_a_typing_route_that_sent_a_key_then_handed_on_makes_a_later_refusal_unverified(self):
        import test_background_keys as BK
        t = BK.TypeText()
        t._cleanups = []
        d, typed = t.run_type(BK.Field("old", takes_insertion=False), clear=True, selects=False, all_selected=False)
        self.assertIsNone(typed)                                # handed on to the next route, as before
        self.assertTrue(d._input_went)                          # but the cmd+A it sent is known
        res = d._went(ExecResult(False, "moved; observe again", stale=True))
        self.assertIsNone(res.performed)
        self.assertTrue(res.indeterminate)

    def test_ok_false_but_indeterminate_is_unverified_not_error(self):
        ex, stub = TE._executor()
        ex.observe(TE.W12)
        stub.execute = lambda action: ExecResult(False, "typing stopped after 3 of 9 chars", indeterminate=True)
        r = ex.perform({"kind": "type", "window": TE.W12, "target": "T1", "text": "123456789"})
        self.assertEqual(r["status"], "unverified", r)
        self.assertNotIn("performed", r)


class AccessibilityCounts(unittest.TestCase):
    """Round 6: an accessibility write the app accepted, or the focus moved into the field, is input -- a route that
    did either and then hands the step on makes a later refusal of the step unverified (the reviewer's repro
    hands_ax_insert_then_handed_on)."""

    def type_(self, **kw):
        import test_background_keys as BK
        t = BK.TypeText()
        t._cleanups = []
        return t.run_type(BK.Field("start", takes_insertion=False), **kw)

    def test_an_accepted_insertion_then_the_key_route_refused(self):
        def enter(pid, wid):
            raise skylight._refuse("previous", "the app's key window cannot be read; nothing sent")
        d, typed = self.type_(enter=enter)
        self.assertIn(("ax insert", "hello 你好"), d.log)
        self.assertIsNone(typed)                                 # handed on
        later = d._went(ExecResult(False, NothingSent("only responds in the foreground, which this run may not use")))
        self.assertIsNot(later.performed, False)
        self.assertTrue(later.indeterminate)

    def test_the_focus_moved_then_select_failed(self):
        d, typed = self.type_(selects=False, clear=False, insert=lambda el, t, r: False)
        self.assertIn("focus", d.log)
        self.assertIsNone(typed)
        self.assertTrue(d._input_went)

    def test_nothing_accepted_and_no_focus_hands_on_as_nothing(self):
        d, typed = self.type_(insert=lambda el, t, r: False, enter=lambda pid, wid: (_ for _ in ()).throw(
            skylight._refuse("previous", "the app's key window cannot be read; nothing sent")))
        self.assertIsNone(typed)
        self.assertFalse(getattr(d, "_input_went", False))


class KeysCount(unittest.TestCase):
    """H4: BackgroundQuartz counts the keys it sends -- a real one, keys only (cmd+A goes, cmd+V refused)."""

    def test_keys_only_then_refused_counts_what_went(self):
        import test_background_keys as BK
        from deskmind_hands.drivers.peekaboo import PeekabooDriver
        posted = []
        key = skylight.WindowKey(BK.PID, BK.TARGET, BK.PREV, "psn", "records")
        key.lost = lambda: None
        q, _ = BK.background_quartz(key)
        with mock.patch.object(skylight, "_load", lambda: None), \
                mock.patch.object(skylight, "post_key_event", lambda pid, ev, auth=True: posted.append(ev)), \
                mock.patch.object(PeekabooDriver, "_key_event",       # the fake Quartz's events, never a real one
                                  classmethod(lambda cls, Q, code, down: Q.CGEventCreateKeyboardEvent(None, code, down))):
            PeekabooDriver._post_key(q, 0, q.kCGEventFlagMaskCommand)      # cmd+A: down, up
            key.lost = lambda: "window 200 is no longer the app's key window"
            with self.assertRaises(skylight.WindowKeyRefused):
                PeekabooDriver._post_key(q, 9, q.kCGEventFlagMaskCommand)  # cmd+V refused
        self.assertEqual(len(posted), 2)
        self.assertEqual(q.sent, 2)


class WentOverUserBusy(unittest.TestCase):
    def test_a_user_busy_refusal_after_input_went_is_unverified_not_nothing_done(self):
        """H10: _went over a USER_BUSY refusal: __post_init__ sees the words again and sets deferred; performed must
        still not become False (the harness would keep the step off its uncertain list)."""
        from deskmind_hands.drivers.peekaboo import PeekabooDriver
        d = PeekabooDriver.__new__(PeekabooDriver)
        d._input_went = True
        r = d._went(ExecResult(False, NothingSent(f"{USER_BUSY}; the step was not carried out before the caller's deadline")))
        self.assertTrue(r.indeterminate)
        self.assertIsNone(r.performed)


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
