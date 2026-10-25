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

    def test_was_running_from_the_one_read(self):
        # (the app's state at the read) -> launched?  -- read failed: as running, nothing launched, no opened_window
        rows = [("running, a window", [("Player", "com.example.player")], False),
                ("running hidden / windowless (listed all the same)", [("Player", "com.example.player")], False),
                ("not running", [], True),
                ("the read failed", None, False)]
        for name, apps, launches in rows:
            e = exe.HandsExecutor.__new__(exe.HandsExecutor)
            # the launch branch reads the windows before it launches; the other goes straight to the app's driver
            with mock.patch("deskmind_hands.executor.running_apps", side_effect=lambda strict=False, apps=apps: apps), \
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
    """(4) where a save sheet would save, from its AX values only."""

    @staticmethod
    def node(role, **attrs):
        return {"AXRole": role, **attrs}

    @staticmethod
    def get(el, attr):
        return el.get(attr) if isinstance(el, dict) else None

    def sheet(self, *children):
        return self.node("AXWindow", AXChildren=[self.node("AXSheet", AXChildren=list(children))])

    def test_the_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = str(Path(tmp).resolve())
            resolve = lambda u: folder if u == "file://docs" else None   # noqa: E731
            name = self.node("AXTextField", AXIdentifier="saveAsNameTextField", AXValue="报告.txt", AXFocused=True)
            where = self.node("AXPopUpButton", AXURL="file://docs")
            sidebar = self.node("AXRow", AXURL="file://elsewhere")
            alert = self.node("AXSheet", AXChildren=[self.node("AXButton")])
            rows = [
                ("no sheet", self.node("AXWindow", AXChildren=[]), None),
                ("name only", self.sheet(name), {"name": "报告.txt", "name_focused": True, "folder": None, "path": None, "nested_sheet": False}),
                ("name and folder", self.sheet(name, where, sidebar),
                 {"name": "报告.txt", "name_focused": True, "folder": folder, "path": str(Path(folder) / "报告.txt"), "nested_sheet": False}),
                ("a nested sheet", self.sheet(name, where, alert),
                 {"name": "报告.txt", "name_focused": True, "folder": folder, "path": str(Path(folder) / "报告.txt"), "nested_sheet": True}),
                ("the name field not focused", self.sheet(dict(name, AXFocused=False), where),
                 {"name": "报告.txt", "name_focused": False, "folder": folder, "path": str(Path(folder) / "报告.txt"), "nested_sheet": False}),
                ("no name field", self.sheet(where), None),
                ("a sidebar's place only: no folder", self.sheet(name, sidebar),
                 {"name": "报告.txt", "name_focused": True, "folder": None, "path": None, "nested_sheet": False}),
            ]
            for label, win, want in rows:
                self.assertEqual(axchrome.read_save_sheet(win, get=self.get, resolve=resolve), want, label)

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
        self.assertIsNone(axchrome.read_save_sheet(two, get=self.get, resolve=lambda u: None), "two fields, no identifier: no guess")


if __name__ == "__main__":
    unittest.main()
