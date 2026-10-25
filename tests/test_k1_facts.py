"""K1: the facts harness needs, reported by the side that owns them, from one read -- mock-only, no real app.

1. A step's wait says what it waits for, with the numbers its loop tests (user_wait) and how it ended (user_resumed),
   one of each per wait; the snapshot says the executor's own stillness threshold.
2. An opened_window is reported only for an app this launch's own read said was not running; a failed read fails
   closed; its key set is the contract in fixtures/contracts/opened_window.json.
3. An element carries its AX subrole, read from the window's own buttons, "" when not read -- never from a label.
4. A save sheet reports where it would save from its AX values only: the folder only from one resolved AXURL, the path
   only when both parts were read, a nested sheet as a fact.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands import executor as exe
from deskmind_hands.drivers import axchrome, base
from deskmind_hands.drivers.base import Element, Rect
from deskmind_hands.drivers.peekaboo import PeekabooDriver

REPO = Path(__file__).resolve().parent.parent


class Waits(unittest.TestCase):
    """(1) the wait's own facts, and exactly one resumed per wait."""

    def setUp(self):
        self.told = []
        base.set_wait_sink(lambda kind, body: self.told.append((kind, dict(body))))
        self.d = PeekabooDriver.__new__(PeekabooDriver)

    def tearDown(self):
        base.set_wait_sink(None)

    def test_the_table(self):
        # (wait, quiet_s, needs_front, how it ends) -> what is said
        rows = [("flash", 3.0, True, ""), ("watching", 1.0, True, ""), ("browser in use", None, True, "timed_out"),
                ("background still", 5.0, False, "stopped"), ("flash, stopped", 3.0, True, "stopped")]
        for name, quiet, front, why in rows:
            self.told.clear()
            waited = False
            for _ in range(3):      # the loop announces on every turn: said once
                waited = self.d._announce_wait(waited, "com.example.app", "click 保存", quiet_s=quiet, needs_front=front, gives_up_at=2_000_000_000.0)
            self.d._resumed(why)
            kinds = [k for k, _ in self.told]
            self.assertEqual(kinds, ["user_wait", "user_resumed"], name)
            w, r = self.told[0][1], self.told[1][1]
            self.assertEqual(w["needs_front"], front, name)
            self.assertEqual(w.get("quiet_s"), quiet, name)          # (absent: a wait for the user to leave the app)
            self.assertEqual(w["gives_up_at"], 2_000_000_000.0, name)
            self.assertIn("started_at", w, name)
            self.assertEqual(r["why"], why, name)
            self.assertIn("waited_s", r, name)

    # -- the loops themselves, with mocks, on every way out (hands#70 review: counted, not grepped)

    class Clock:
        def __init__(self):
            self.now = 1000.0
        def time(self):
            return self.now
        def sleep(self, d):
            self.now += d
        def __getattr__(self, k):       # (anything else the module takes from time)
            import time as _t
            return getattr(_t, k)

    def _driver(self, clock, idle, **over):
        """A driver whose flash goes the foreground way: `idle` the HID idle seconds, one per read (the last repeats)."""
        d = PeekabooDriver.__new__(PeekabooDriver)
        seq = list(idle)
        def hid():
            v = seq.pop(0) if len(seq) > 1 else seq[0]
            if isinstance(v, Exception):
                raise v
            return v
        d.__dict__.update(dict(
            _background_input=lambda b: False, _wait_until=lambda s: clock.now + self.give_up_in,
            _too_late=lambda waited: None, _front_bundle=lambda: "com.example.other", _takeover=lambda: False, _hid_idle=hid,
            _front_taken=lambda b, t: object(), _bring_forward=lambda b, browser: True, _wait_front=lambda b: None,
            _give_front_back=lambda *a, **k: None, _front_given_back=lambda t: None, _foreground_actions=0))
        d.__dict__.update(over)
        return d

    def _run(self, fn, clock):
        quartz = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"Quartz": quartz}), \
             mock.patch("deskmind_hands.drivers.peekaboo.time", clock), \
             mock.patch("deskmind_hands.drivers.peekaboo.release_all", lambda: None), \
             mock.patch("deskmind_hands.drivers.peekaboo._ascii_input_source", lambda: None), \
             mock.patch.dict("os.environ", {"HANDS_FLASH_APPS": "com.example.app", "HANDS_FLASH_WAIT_S": "60", "HANDS_BG_STILL_S": "5"}):
            try:
                return fn(), None
            except Exception as exc:  # noqa: BLE001
                return None, exc

    def test_the_flash_loop_closes_every_wait(self):
        from deskmind_hands.grounding import STOP
        busy, still = 0.0, 100.0
        def boom(*a, **k):
            raise RuntimeError("boom")
        # (name, HID idle reads, overrides, give up in, stop after the first wait) -> resumed whys (none: no wait), raised
        rows = [
            ("still at once: no wait", [still], {}, 60, False, [], False),
            ("busy, then still: the step goes on", [busy, still], {}, 60, False, [""], False),
            ("busy, the app's stop", [busy], {}, 60, True, ["stopped"], False),
            ("busy, too late for the caller", [busy, busy], {"_too_late": lambda waited: "late" if waited else None}, 60, False, ["timed_out"], False),
            ("busy past the wait's own end", [busy], {}, 3, False, ["timed_out"], False),
            ("busy, then the window not raised", [busy, still], {"_bring_forward": lambda b, br: False}, 60, False, ["failed"], False),
            ("busy, then the front not arrived", [busy, still], {"_wait_front": lambda b: "the app did not come forward"}, 60, False, ["failed"], False),
            ("busy, then bringing it forward raised", [busy, still], {"_bring_forward": boom}, 60, False, ["failed"], True),
            ("busy, touched while it came forward, then still", [busy, still, busy, still, still], {}, 60, False, [""], False),
        ]
        for name, idle, over, give_up, stop, want, raises in rows:
            self.told.clear()
            clock, self.give_up_in = self.Clock(), give_up
            d = self._driver(clock, idle, **over)
            if stop:
                d._announce_wait = (lambda orig: (lambda *a, **k: (STOP.set(), orig(*a, **k))[1]))(d._announce_wait)
            try:
                _, exc = self._run(lambda: d._flash("com.example.app", lambda q: "done", "click 保存"), clock)
            finally:
                STOP.clear()
            kinds = [k for k, _ in self.told]
            self.assertEqual(kinds, (["user_wait"] if want else []) + ["user_resumed"] * len(want), name)
            self.assertEqual([b["why"] for k, b in self.told if k == "user_resumed"], want, name)
            self.assertEqual(exc is not None, raises, f"{name}: {exc}")
            self.assertNotIn("_wait_t0", d.__dict__, name)

    def test_the_still_wait_closes_every_wait(self):
        from deskmind_hands.grounding import STOP
        rows = [("still at once", [100.0], 60, False, []), ("busy, then still", [0.0, 100.0], 60, False, [""]),
                ("busy, the app's stop", [0.0], 60, True, ["stopped"]), ("busy past its end", [0.0], 3, False, ["timed_out"]),
                ("busy, then reading the idle time raised", [0.0, RuntimeError("io")], 60, False, ["failed"])]
        for name, idle, give_up, stop, want in rows:
            self.told.clear()
            clock, self.give_up_in = self.Clock(), give_up
            d = self._driver(clock, idle)
            if stop:
                d._announce_wait = (lambda orig: (lambda *a, **k: (STOP.set(), orig(*a, **k))[1]))(d._announce_wait)
            try:
                self._run(lambda: d._wait_until_still("com.example.app", "type"), clock)
            finally:
                STOP.clear()
            self.assertEqual([b["why"] for k, b in self.told if k == "user_resumed"], want, name)
            self.assertEqual([k for k, _ in self.told].count("user_wait"), 1 if want else 0, name)
            self.assertNotIn("_wait_t0", d.__dict__, name)

    def test_every_wait_in_the_driver_says_its_numbers_and_every_stop_says_stopped(self):
        src = (REPO / "deskmind_hands/drivers/peekaboo.py").read_text()
        calls = re.findall(r"self\._announce_wait\(waited, bundle, what(.*?)\)\n", src)
        self.assertGreaterEqual(len(calls), 4)
        for c in calls:
            self.assertIn("needs_front=", c)
            self.assertIn("gives_up_at=deadline", c)
            self.assertIn("quiet_s=", c)
        # a resumed right after a stop says stopped
        for m in re.finditer(r"if STOP\.is_set\(\):\n\s+if waited:\n\s+self\._resumed\((.*?)\)", src):
            self.assertEqual(m.group(1), '"stopped"')

    def test_the_snapshot_says_the_executors_stillness(self):
        src = (REPO / "deskmind_hands/executor.py").read_text()
        self.assertIn('"user_quiet_s": self.USER_ACTIVE_S,', src)
        self.assertEqual(exe.HandsExecutor.USER_ACTIVE_S, 3.0)


class OpenedWindow(unittest.TestCase):
    """(2) one read, fail closed, the contract's key set."""

    def test_the_contract(self):
        c = json.loads((REPO / "fixtures/contracts/opened_window.json").read_text())
        self.assertEqual(tuple(c["keys"]), exe.OPENED_WINDOW_KEYS)
        w = exe.opened_window({"id": "7", "app": "Player", "bundle": "com.example.player", "title": "Player"}, "com.example.player")
        self.assertEqual(set(w), set(c["keys"]))
        self.assertIs(w["was_running"], False)
        self.assertEqual(w["bundle"], "com.example.player")
        self.assertEqual(set(c["example"]), set(c["keys"]))

    def test_a_failed_read_is_not_not_running(self):
        fake = mock.MagicMock()
        fake.NSWorkspace.sharedWorkspace.side_effect = RuntimeError("no workspace")
        with mock.patch.dict(sys.modules, {"AppKit": fake}):
            self.assertIsNone(exe.running_apps(strict=True))
            self.assertEqual(exe.running_apps(), [])
        # a read that answers: hidden and windowless apps are listed like any (NSWorkspace lists every app process)
        app = mock.MagicMock(); app.bundleIdentifier.return_value = "com.example.player"; app.localizedName.return_value = "Player"
        ok = mock.MagicMock(); ok.NSWorkspace.sharedWorkspace.return_value.runningApplications.return_value = [app]
        with mock.patch.dict(sys.modules, {"AppKit": ok}):
            self.assertEqual(exe.running_apps(strict=True), [("Player", "com.example.player")])

    def test_the_fresh_read(self):
        # NSWorkspace's list stale (empty), NSRunningApplication's read for the bundle fresh: running
        app = mock.MagicMock()
        fake = mock.MagicMock()
        fake.NSWorkspace.sharedWorkspace.return_value.runningApplications.return_value = []
        fake.NSRunningApplication.runningApplicationsWithBundleIdentifier_.side_effect = lambda b: [app] if b == "com.example.player" else []
        with mock.patch.dict(sys.modules, {"AppKit": fake}):
            self.assertEqual(exe.running_apps(strict=True), [])
            self.assertIs(exe.app_running("com.example.player"), True)
            self.assertIs(exe.app_running("com.example.other"), False)
        broken = mock.MagicMock()
        broken.NSRunningApplication.runningApplicationsWithBundleIdentifier_.side_effect = RuntimeError("no")
        with mock.patch.dict(sys.modules, {"AppKit": broken}):
            self.assertIsNone(exe.app_running("com.example.player"))

    def test_was_running_from_the_one_read(self):
        # (the list, the fresh read for the bundle) -> launched?  -- any read saying it runs, or one that failed: as
        # running, nothing launched, no opened_window
        listed = [("Player", "com.example.player")]
        rows = [("running, listed and read", listed, True, False),
                ("running hidden / windowless (read all the same)", listed, True, False),
                ("started after the list was read (a stale list)", [], True, False),
                ("not running", [], False, True),
                ("the list failed", None, False, False),
                ("the fresh read failed", [], None, False)]
        for name, apps, fresh, launches in rows:
            e = exe.HandsExecutor.__new__(exe.HandsExecutor)
            # the launch branch reads the windows before it launches; the other goes straight to the app's driver
            with mock.patch("deskmind_hands.executor.running_apps", side_effect=lambda strict=False, apps=apps: apps), \
                 mock.patch("deskmind_hands.executor.app_running", return_value=fresh), \
                 mock.patch("deskmind_hands.executor.resolve_app", return_value="com.example.player"), \
                 mock.patch("deskmind_hands.executor.installed_apps", return_value=[]), \
                 mock.patch.object(exe.HandsExecutor, "_known_windows", create=True, side_effect=RuntimeError("launch")), \
                 mock.patch.object(exe.HandsExecutor, "_driver", side_effect=RuntimeError("no launch")):
                with self.assertRaises(RuntimeError) as got:
                    e._open_window_now("Player", None)
            self.assertEqual(str(got.exception) == "launch", launches, f"{name}: {got.exception}")


class Subroles(unittest.TestCase):
    """(3) from the window's own buttons, "" otherwise -- never from the label."""

    def test_the_table(self):
        buttons = [("AXCloseButton", (100.0, 50.0, 14.0, 16.0)), ("AXMinimizeButton", (120.0, 50.0, 14.0, 16.0)),
                   ("AXZoomButton", (140.0, 50.0, 14.0, 16.0))]
        frame = [100.0, 50.0, 800.0, 600.0]
        rows = [("the close button", Rect(0, 0, 14, 16), "关闭", "AXCloseButton"),
                ("the minimise button", Rect(20, 0, 14, 16), "", "AXMinimizeButton"),
                ("the zoom button", Rect(40, 0, 14, 16), "", "AXZoomButton"),
                ("a plain button labelled 关闭", Rect(300, 400, 60, 24), "关闭", ""),
                ("no rect", None, "关闭", "")]
        for name, rect, label, want in rows:
            e = Element(id="e", role="button", ax_role="AXButton", label=label, rect=rect)
            self.assertEqual(exe.element_subrole(e, frame, buttons), want, name)
        # nothing read: "" -- never a guess
        self.assertEqual(exe.element_subrole(Element(id="e", role="button", label="关闭", rect=Rect(0, 0, 14, 16)), frame, []), "")
        # the driver's own reading wins
        self.assertEqual(exe.element_subrole(Element(id="e", role="button", ax_subrole="AXCloseButton"), None, []), "AXCloseButton")
        # a pixel-read element: none
        px = Element(id="e", role="button", rect=Rect(0, 0, 14, 16)); px.source = "pixels"
        self.assertEqual(exe.element_subrole(px, frame, buttons), "")

    def test_observe_always_says_it(self):
        src = (REPO / "deskmind_hands/executor.py").read_text()
        self.assertIn('"ax_subrole": element_subrole(e, frame0, buttons)}', src)


class SaveSheets(unittest.TestCase):
    """(4) where a save sheet would save, from its AX values only; a sheet is always reported, unknown as None."""

    @staticmethod
    def node(role, **attrs):
        return {"AXRole": role, **attrs}

    @staticmethod
    def get(el, attr):
        return el.get(attr) if isinstance(el, dict) else None

    def sheet(self, *children):
        return self.node("AXWindow", AXChildren=[self.node("AXSheet", AXChildren=list(children))])

    def want(self, **k):
        w = {"read": True, "name": None, "stem": None, "name_focused": False, "folder": None, "path": None, "nested_sheet": False}
        w.update(k)
        self.assertEqual(set(w), set(axchrome.SAVE_SHEET_KEYS))
        return w

    def test_the_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = str(Path(tmp).resolve())
            resolve = lambda u: folder if u == "file://docs" else None   # noqa: E731
            def name(v, focused=True):
                return self.node("AXTextField", AXIdentifier="saveAsNameTextField", AXValue=v, AXFocused=focused)
            where = self.node("AXPopUpButton", AXURL="file://docs")
            sidebar = self.node("AXRow", AXURL="file://elsewhere")
            alert = self.node("AXSheet", AXChildren=[self.node("AXButton")])
            shown = self.node("AXCheckBox", AXIdentifier=axchrome.HIDE_EXTENSION_IDS[0], AXValue=0)
            hidden = self.node("AXCheckBox", AXIdentifier=axchrome.HIDE_EXTENSION_IDS[0], AXValue=1)
            full = str(Path(folder) / "报告.docx")
            rows = [
                ("no sheet", self.node("AXWindow", AXChildren=[]), None),
                ("name only", self.sheet(name("报告.docx")), self.want(name="报告.docx", stem="报告", name_focused=True)),
                ("name, folder, extension shown", self.sheet(name("报告.docx"), where, shown, sidebar),
                 self.want(name="报告.docx", stem="报告", name_focused=True, folder=folder, path=full)),
                ("the extension hidden: no path, the stem", self.sheet(name("报告"), where, hidden),
                 self.want(name="报告", stem="报告", name_focused=True, folder=folder)),
                ("the box not read: no path", self.sheet(name("报告.docx"), where),
                 self.want(name="报告.docx", stem="报告", name_focused=True, folder=folder)),
                ("no extension, the box unchecked: no path", self.sheet(name("报告"), where, shown),
                 self.want(name="报告", stem="报告", name_focused=True, folder=folder)),
                ("a name with /: no name", self.sheet(name("a/b.txt"), where, shown), self.want(name_focused=True, folder=folder)),
                ("a nested sheet", self.sheet(name("报告.docx"), where, shown, alert),
                 self.want(name="报告.docx", stem="报告", name_focused=True, folder=folder, path=full, nested_sheet=True)),
                ("not focused", self.sheet(name("报告.docx", False), where, shown),
                 self.want(name="报告.docx", stem="报告", folder=folder, path=full)),
                ("a sheet with no name field: reported, unknown", self.sheet(where), self.want(read=False, name_focused=None, folder=folder)),
                ("a sidebar's place only: no folder", self.sheet(name("报告.docx"), sidebar, shown),
                 self.want(name="报告.docx", stem="报告", name_focused=True)),
            ]
            for label, win, want in rows:
                self.assertEqual(axchrome.read_save_sheet(win, get=self.get, resolve=resolve), want, label)

    def test_a_sheet_too_big_to_read_whole(self):
        # the name field near the top is still read (breadth first); folder, nested and path unknown
        filler = [self.node("AXGroup", AXChildren=[self.node("AXStaticText") for _ in range(50)]) for _ in range(100)]
        name = self.node("AXTextField", AXIdentifier="saveAsNameTextField", AXValue="a.txt")
        got = axchrome.read_save_sheet(self.sheet(name, self.node("AXPopUpButton", AXURL="u"), *filler), get=self.get, resolve=lambda u: "/tmp")
        self.assertEqual((got["read"], got["name"], got["folder"], got["path"], got["nested_sheet"]), (False, "a.txt", None, None, None))

    def test_two_folders_read_is_no_folder(self):
        name = self.node("AXTextField", AXIdentifier="saveAsNameTextField", AXValue="a.txt")
        win = self.sheet(name, self.node("AXPopUpButton", AXURL="a"), self.node("AXPopUpButton", AXURL="b"))
        got = axchrome.read_save_sheet(win, get=self.get, resolve=lambda u: "/tmp/" + u)
        self.assertIsNone(got["folder"])
        self.assertIsNone(got["path"])

    def test_the_only_settable_field_when_no_identifier(self):
        f = self.node("AXTextField", AXValueSettable=True, AXValue="x.txt")
        got = axchrome.read_save_sheet(self.sheet(f), get=self.get, resolve=lambda u: None)
        self.assertEqual(got["name"], "x.txt")
        two = self.sheet(f, self.node("AXTextField", AXValueSettable=True, AXValue="y"))
        got = axchrome.read_save_sheet(two, get=self.get, resolve=lambda u: None)
        self.assertEqual((got["read"], got["name"]), (False, None), "two fields, no identifier: no guess, still a sheet")

    def test_could_not_read_is_not_no_sheet(self):
        with mock.patch.object(axchrome, "_the_window", side_effect=RuntimeError("AX")):
            self.assertEqual(axchrome.save_sheet(1, (0, 0, 1, 1)), axchrome._unknown_sheet())
        with mock.patch.object(axchrome, "_the_window", return_value=None):
            self.assertEqual(axchrome.save_sheet(1, (0, 0, 1, 1))["read"], False)
        self.assertEqual(set(axchrome._unknown_sheet()), set(axchrome.SAVE_SHEET_KEYS))


if __name__ == "__main__":
    unittest.main()
