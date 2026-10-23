"""A request never outlives its caller's deadline (rc.14, 10-23).

The task was 「打开网易云播放安溥的宝贝」 while a script pressed Shift in another app about once a second. The step was a
set_value in a foreground-only app, so the driver waited for a still Mac (_flash) -- bounded only by
HANDS_FLASH_WAIT_S, which the app sets to 1800 s. harness stopped waiting at 120 s and recorded an error; 18 s later
the search box was filled and Return pressed, with nobody waiting for it, and the executor (one request at a time)
answered nothing else meanwhile.

Now the request carries `deadline_ms`, and every wait for a still Mac ends DEADLINE_MARGIN_S before it: nothing is
done, and the reply says so (`user_busy`, `performed: false`) while the caller still listens. Without a deadline
(`hands do`) the waits are as before.

Fakes only, on a fake clock: no app is driven, nothing is sent. Every test here fails on 3c1ceac (perform and observe
take no deadline there, and the waits run to HANDS_FLASH_WAIT_S)."""
from __future__ import annotations

import io
import json
import os
import sys
import time
import types
import unittest
from unittest import mock

from deskmind_hands.drivers import base, peekaboo
from deskmind_hands.drivers.base import USER_BUSY, ExecResult, Element, Observation
from deskmind_hands.drivers.peekaboo import PeekabooDriver
from deskmind_hands.geometry import ImageTransform, Point, Rect, ScreenGeometry, Size

CALLER_S = 120.0                    # harness src/executor/hands.ts: the time it waits for a reply
APP = "com.netease.163music"
WIN = f"{APP}#7"
ENV = {"HANDS_FLASH_APPS": APP, "HANDS_FLASH_WAIT_S": "1800"}   # as the app sets them (HarnessRun.swift)
# read through getattr so that this file runs, and fails on what it checks, on a head without them (3c1ceac)
DEADLINE_MARGIN_S = getattr(base, "DEADLINE_MARGIN_S", 20.0)
set_wait_sink = getattr(base, "set_wait_sink", lambda sink: None)


def tell_wait(kind, body):
    getattr(base, "tell_wait", lambda k, b: None)(kind, body)


class Clock:
    """time.time / time.sleep for the driver: sleeping moves the clock, nothing really waits."""

    def __init__(self):
        self.t = time.time()        # the executor reads the real clock; the two start together

    def time(self):
        return self.t

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


def fake_quartz():
    q = types.ModuleType("Quartz")
    q.CGEventCreate = lambda _: None
    q.CGEventGetLocation = lambda _: (0, 0)
    q.CGWarpMouseCursorPosition = lambda _: None
    q.CGAssociateMouseAndMouseCursorPosition = lambda _: None
    return q


def _obs(els):
    size = Size(400, 300)
    return Observation(id="obs-1", geometry=ScreenGeometry(logical=size, pixels=size),
                       transform=ImageTransform.identity(size), elements=els, window_title="网易云音乐", dialog=False)


class FlashDriver(PeekabooDriver):
    """The real _flash on a fake desktop: the user presses a key in another app (iTerm2 in front) every second until
    `busy_s` into the test (None: never stops). `acted` holds the clock times the step's input was sent at."""

    def __init__(self, clock: Clock, busy_s: float | None):   # noqa: D107 -- no PeekabooDriver.__init__: no Peekaboo
        self.clock, self.start, self.busy_s = clock, clock.t, busy_s
        self.app = APP
        self._foreground_actions = 0
        self._pinned_window = self._window_id = None
        self.front = "com.googlecode.iterm2"
        self.acted: list[float] = []
        self.waits: list[tuple[str, dict]] = []

    # the desktop
    def _hid_idle(self):
        t = self.clock.t - self.start
        return 0.5 if self.busy_s is None or t < self.busy_s else t - self.busy_s

    def _takeover(self):
        return False

    def _background_input(self, bundle):
        return False

    def _front_bundle(self):
        self.clock.sleep(0.23)                      # one osascript, as in the rc.14 log
        return self.front

    def _bring_forward(self, bundle, browser):
        self.front = bundle
        return True

    def _give_front_back(self, prev, ours=None, late_s=0.0):
        self.front = prev

    @staticmethod
    def _front_taken(bundle, began):
        return None

    @staticmethod
    def _front_given_back(token):
        pass

    # what the executor asks of a driver
    def observe(self):
        return _obs([Element(id="E1", role="textField", label="搜索框", value="", settable=True)])

    def execute(self, action):
        def perform(Q):
            self.acted.append(self.clock.t - self.start)
            return f"entered {action.text!r} into '搜索框' and pressed Return"
        ok, detail = self._flash(self.app, perform, what="type")
        return ExecResult(ok, detail)

    def allow_browser_field(self, eid):
        pass

    def close(self):
        pass


def _executor(driver):
    """A HandsExecutor over `driver`, with nothing of the real desktop read (as tests/test_executor.py does)."""
    import deskmind_hands.browsers as br
    import deskmind_hands.executor as exe
    from deskmind_hands.executor import HandsExecutor
    for target, name, value in ((br, "_BROWSERS", set()), (br, "_INSTALLED", []),
                                (exe, "app_display_name", lambda b: b), (exe, "playing_apps", lambda: [])):
        p = mock.patch.object(target, name, value)
        p.start()
    ex = HandsExecutor()
    ex._driver = lambda bundle: driver          # noqa: SLF001
    ex._document = lambda window, wins: None   # noqa: SLF001
    ex._text = lambda w: None                  # noqa: SLF001
    ex._last_input_at = lambda: 0.0            # noqa: SLF001
    ex._ax_window_kinds = lambda pid: None      # noqa: SLF001
    ex._windows = lambda every=False: [{"id": WIN, "app": "网易云音乐", "bundle": APP, "title": "网易云音乐", "pid": 1,
                                        "bounds": None}]
    return ex


class PerformNeverOutlivesTheCaller(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.clock = Clock()
        for p in (mock.patch.dict(os.environ, ENV), mock.patch.dict(sys.modules, {"Quartz": fake_quartz()}),
                  mock.patch.object(peekaboo, "time", self.clock),
                  mock.patch.object(peekaboo, "_ascii_input_source", lambda: None)):
            p.start()
        self.waits: list[tuple[str, dict]] = []
        set_wait_sink(lambda kind, body: self.waits.append((kind, body)))
        self.addCleanup(set_wait_sink, None)

    def _perform(self, busy_s, deadline=True):
        d = FlashDriver(self.clock, busy_s)
        ex = _executor(d)
        ex.observe(WIN)
        start = self.clock.t
        kw = {"deadline": start + CALLER_S} if deadline else {}
        r = ex.perform({"kind": "set_value", "window": WIN, "target": "E1", "value": "安溥 宝贝", "foreground_ok": True},
                       **kw)
        return r, self.clock.t - start, d.acted

    def test_busy_past_the_deadline_answers_user_busy_in_time_and_sends_nothing(self):
        """The rc.14 run: the user busy for 140 s. Before: the step waited on and acted at ~144 s, 24 s after the
        caller had given up. Now: answered user_busy before the caller's deadline less the margin, nothing sent."""
        r, took, acted = self._perform(busy_s=140.0)
        self.assertEqual(r["status"], "user_busy", r)
        self.assertIs(r["performed"], False)
        self.assertTrue(r["why"].startswith(USER_BUSY), r["why"])
        self.assertEqual(acted, [])                                   # perform() never ran: no click, no key
        self.assertLessEqual(took, CALLER_S - DEADLINE_MARGIN_S + 2)  # the reply goes while the caller still listens
        self.assertEqual([k for k, _ in self.waits], ["user_wait", "user_resumed"])   # told, and told it ended

    def test_the_flash_itself_gives_up_at_the_callers_deadline(self):
        """The driver alone, given the caller's deadline as the executor gives it: no input after it."""
        d = FlashDriver(self.clock, busy_s=140.0)
        start = self.clock.t
        d.caller_deadline = start + CALLER_S
        ok, detail = d._flash(APP, lambda Q: d.acted.append(self.clock.t - start), what="type")
        self.assertEqual((ok, d.acted), (False, []), detail)
        self.assertLessEqual(self.clock.t - start, CALLER_S - DEADLINE_MARGIN_S + 2)

    def test_never_still_answers_in_time_too(self):
        r, took, acted = self._perform(busy_s=None)
        self.assertEqual((r["status"], acted), ("user_busy", []))
        self.assertLess(took, CALLER_S)

    def test_still_just_before_the_deadline_acts(self):
        """Still 3 s at 95 s: the act starts before the caller's deadline less the margin, and goes through."""
        r, took, acted = self._perform(busy_s=95.0)
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual(len(acted), 1)
        self.assertLess(acted[0], CALLER_S - DEADLINE_MARGIN_S)

    def test_without_a_deadline_the_wait_is_as_before(self):
        """`hands do` sends none: HANDS_FLASH_WAIT_S alone bounds the wait, so a user busy for 140 s is waited out."""
        r, took, acted = self._perform(busy_s=140.0, deadline=False)
        self.assertEqual(r["status"], "ok", r)
        self.assertGreater(acted[0], 140.0)


class EveryKindKeepsTheMargin(unittest.TestCase):
    """open_window (up to ~30 s), close_window, focus_window and wait returned before the step's deadline check: started
    inside the margin, an open_window answered after the caller had given up (review of hands#64). Now no kind starts
    there, and nothing moves (not even the epoch)."""

    def test_no_kind_starts_inside_the_margin(self):
        self.addCleanup(mock.patch.stopall)
        d = FlashDriver(Clock(), busy_s=0.0)
        ex = _executor(d)
        ex.observe(WIN)
        ex._open_window = lambda app, url: self.fail("open_window started inside the margin")   # noqa: SLF001
        ex._pk = lambda args: self.fail("close_window started inside the margin")              # noqa: SLF001
        e0 = ex.epoch
        inside = time.time() + DEADLINE_MARGIN_S / 2
        for action in ({"kind": "open_window", "app": "网易云音乐", "url": "https://music.163.com"},
                       {"kind": "close_window", "window": WIN}, {"kind": "focus_window", "window": WIN},
                       {"kind": "wait", "ms": 20_000},
                       {"kind": "set_value", "window": WIN, "target": "E1", "value": "x", "foreground_ok": True}):
            t0 = time.time()
            r = ex.perform(action, deadline=inside)
            self.assertEqual((r["status"], r.get("performed")), ("error", False), (action, r))
            self.assertLess(time.time() - t0, 1.0)                     # the wait did not sleep either
        self.assertEqual(ex.epoch, e0)
        self.assertEqual(d.acted, [])


class PopupDriver(FlashDriver):
    """A window with three pop-up buttons whose choices are read through accessibility (opening each menu for a
    moment, so only while the Mac is still)."""

    def __init__(self, clock, busy_s):
        super().__init__(clock, busy_s)
        self._auto_vision = set()
        self.read: list[str] = []

    def _pid_of(self, app):
        return 42

    def observe(self):
        els = [Element(id=f"P{i}", role="popUpButton", ax_role="AXPopUpButton", label=f"选项{i}",
                       rect=Rect(10, 10 + 30 * i, 80, 20)) for i in range(3)]
        self._attach_popup_options(els, Point(0, 0))
        return _obs(els)


class ObserveSharesOneDeadline(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.clock = Clock()
        from deskmind_hands.drivers import axpopup
        pops = [{"el": f"pop{i}", "frame": (10, 10 + 30 * i, 80, 20)} for i in range(3)]
        for p in (mock.patch.dict(os.environ, ENV), mock.patch.object(peekaboo, "time", self.clock),
                  mock.patch.object(axpopup, "popups", lambda pid: pops),
                  mock.patch.object(axpopup, "match", lambda frames, found, titles=None: dict(enumerate(found))),
                  mock.patch.object(axpopup, "options", lambda el: ["甲", "乙"])):
            p.start()
        set_wait_sink(lambda kind, body: None)
        self.addCleanup(set_wait_sink, None)

    def test_three_popups_wait_one_deadline_and_say_what_was_not_read(self):
        """The user never still: before, each pop-up waited HANDS_FLASH_WAIT_S in turn (3 x 1800 s). Now the look
        waits once, up to the caller's deadline less the margin, and says which choices were not read."""
        d = PopupDriver(self.clock, busy_s=None)
        ex = _executor(d)
        start = self.clock.t
        o = ex.observe(WIN, deadline=start + CALLER_S)
        took = self.clock.t - start
        self.assertLessEqual(took, CALLER_S - DEADLINE_MARGIN_S + 1)
        self.assertEqual(len(o["elements"]), 3)
        self.assertTrue(all("options" not in e for e in o["elements"]))     # none read: never still
        self.assertEqual(len(o["notes"]), 3)
        self.assertTrue(all("were not read" in n for n in o["notes"]))
        self.assertIsNone(getattr(d, "caller_deadline", None))              # the look's deadline does not linger

    def test_still_in_time_reads_them_all(self):
        d = PopupDriver(self.clock, busy_s=30.0)
        o = _executor(d).observe(WIN, deadline=self.clock.t + CALLER_S)
        self.assertEqual([e.get("options") for e in o["elements"]], [["甲", "乙"]] * 3)
        self.assertNotIn("notes", o)


class TheExecutorsProtocol(unittest.TestCase):
    """The JSON lines: `deadline_ms` reaches the step, a request read after it is not carried out, and the waits for
    the user go out as JSON events (no `id`: a harness that does not know them reads no reply into them)."""

    def _run(self, lines: list[dict], fake):
        from deskmind_hands import executor as exe
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO("".join(json.dumps(m) + "\n" for m in lines))), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(exe, "HandsExecutor", fake):
            exe.main()
        return [json.loads(x) for x in out.getvalue().splitlines()]

    def test_deadline_events_and_late_requests(self):
        seen = []

        class Fake:
            def __init__(self, *a, **k):
                pass

            def snapshot(self):
                return {"epoch": 1}

            def perform(self, action, shot=None, frames=False, deadline=None):
                seen.append(deadline)
                tell_wait("user_wait", {"reason": "user_active", "app": APP, "what": "type"})
                tell_wait("user_resumed", {"waited_s": 1.0})
                return {"status": "user_busy", "performed": False}

        now_ms = time.time() * 1000
        out = self._run([{"id": 1, "op": "begin"},
                         {"id": 2, "op": "perform", "action": {"kind": "click"}, "deadline_ms": now_ms + 120_000},
                         {"id": 3, "op": "perform", "action": {"kind": "click"}, "deadline_ms": now_ms + 10_000},
                         {"id": 4, "op": "observe", "window": WIN, "deadline_ms": now_ms - 1}], Fake)
        self.assertEqual(len(seen), 1)                     # 3 came inside the margin before its deadline: not done
        self.assertAlmostEqual(seen[0], (now_ms + 120_000) / 1000, places=2)
        events = [m for m in out if "event" in m]
        self.assertEqual([(e["event"], e["req"]) for e in events], [("user_wait", 2), ("user_resumed", 2)])
        self.assertTrue(all("id" not in e for e in events))
        by_id = {m["id"]: m for m in out if "id" in m}
        self.assertEqual(by_id[2]["result"]["status"], "user_busy")
        self.assertEqual(by_id[3]["result"], {"status": "error", "performed": False,
                                              "why": "read too late for the caller's deadline; nothing was done"})
        self.assertFalse(by_id[4]["ok"])


if __name__ == "__main__":
    unittest.main()
