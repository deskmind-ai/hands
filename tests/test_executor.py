"""The harness-facing executor: the JSON mapping on both sides of a driver, with the driver stubbed (no desktop).

unittest, as CI collects it (python -m unittest discover -s tests); pytest runs these too."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from deskmind_hands.actions import ActionKind
from deskmind_hands.drivers.base import Element, ExecResult, Observation
from deskmind_hands.executor import HandsExecutor, allow_flash, installed_apps, resolve_app, sends, takeover_until
from deskmind_hands.geometry import ImageTransform, ScreenGeometry, Size

TE = "com.apple.TextEdit"
W12 = f"{TE}#12"
W13 = f"{TE}#13"


def _win(wid: str, bundle: str = TE, title: str = "未命名", app: str = "文本编辑") -> dict:
    return {"id": f"{bundle}#{wid}", "app": app, "bundle": bundle, "title": title, "pid": 1, "bounds": None}


class StubDriver:
    def __init__(self):
        self.actions = []
        self.allowed = []
        self.follow = None          # a window id the observe lands on instead of the one asked for
        self._pinned_window = None
        self._window_id = None

    def observe(self):
        if self.follow:
            self._pinned_window = self.follow
        els = [Element(id="T1", role="textField", label="正文", value="", settable=True, focused=True),
               Element(id="B2", role="button", label="保存"),
               Element(id="P3", role="popUpButton", label="位置", options=["桌面", "文稿"]),
               Element(id="S4", role="staticText", label="", value="已保存"),
               Element(id="B5", role="button", label="发送"),
               Element(id="B6", role="button", label="回复")]
        size = Size(400, 300)
        return Observation(id="obs-1", geometry=ScreenGeometry(logical=size, pixels=size), transform=ImageTransform.identity(size), elements=els,
                           window_title="未命名", dialog=False)

    def execute(self, action):
        self.actions.append(action)
        if action.kind is ActionKind.KEY:
            return ExecResult(True, "pressed; could not be confirmed", indeterminate=True)
        return ExecResult(True, "done")

    def allow_browser_field(self, eid):
        self.allowed.append(eid)

    def close(self):
        pass


# No LaunchServices queries in these tests (the first one on a Mac can take minutes): which apps are browsers, and
# what an app is called, are given, except where a test says otherwise.
import deskmind_hands.executor as _exe   # noqa: E402
_exe._BROWSERS = set()
_exe.app_display_name = lambda bundle: bundle


def _executor():
    ex = HandsExecutor()
    stub = StubDriver()
    ex._driver = lambda bundle: stub            # noqa: SLF001 -- the point of the tests is the layer above the driver
    ex._document = lambda window, wins: None   # noqa: SLF001 -- no desktop: the app says no document
    ex._last_input_at = lambda: 0.0            # noqa: SLF001 -- the user has not touched the Mac
    return ex, stub


class Mapping(unittest.TestCase):
    def test_observe_maps_elements_to_the_generic_shape(self):
        ex, _ = _executor()
        o = ex.observe(W12)
        by_id = {e["id"]: e for e in o["elements"]}
        self.assertEqual(by_id["T1"]["actions"], ["click", "set_value", "type"])
        self.assertTrue(by_id["T1"]["focused"])
        self.assertEqual(by_id["B2"]["actions"], ["click"])
        self.assertEqual(by_id["P3"]["actions"], ["click", "select"])
        self.assertEqual(by_id["P3"]["options"], ["桌面", "文稿"])
        self.assertEqual(o["text"], "已保存")
        self.assertEqual({k: o["window"][k] for k in ("id", "app", "title")}, {"id": W12, "app": TE, "title": "未命名"})
        self.assertTrue(o["window"]["app_name"])            # what the user calls it, for cards (bundle id when unknown)
        self.assertIsNone(o["modal"])
        self.assertTrue(json.dumps(o, ensure_ascii=False))   # serialisable as it stands

    def test_observe_says_whether_each_element_was_read_from_the_tree_or_the_screenshot(self):
        """An app read from its pixels (vision.py: OCR words, grounded icons, scroll and generic controls) gives
        elements whose label and place are a reading of the image; the layer above is told so, element by element.
        Every element vision_elements makes is marked, whatever its id; the tree's are "ax"."""
        from deskmind_hands import vision
        rows = [{"text": "林夏 纸船", "x": 0.1, "y": 0.4, "w": 0.3, "h": 0.05, "conf": 0.9}]
        with mock.patch.object(vision, "ocr", return_value=rows), mock.patch.object(vision, "_icons", return_value={}):
            pixels = vision.vision_elements("com.example.music", Path("shot.png"), 400, 300, "音乐", "12")
        self.assertTrue(any(e.id.startswith("ocr:") for e in pixels) and any(e.id.startswith("icon:") for e in pixels))
        self.assertEqual({e.source for e in pixels}, {"pixels"})
        ex, stub = _executor()
        tree = stub.observe
        stub.observe = lambda: (lambda o: (o.elements.extend(pixels), o)[1])(tree())
        o = ex.observe(W12)
        by_id = {e["id"]: e for e in o["elements"]}
        self.assertEqual(by_id["T1"]["source"], "ax")
        self.assertEqual(by_id["B2"]["source"], "ax")
        for e in pixels:
            self.assertEqual(by_id[e.id]["source"], "pixels", e.id)
        self.assertEqual(Element(id="x", role="button").source, "ax")    # the default: drivers that do not say
        # kept through a trace: written by to_json only when not "ax", read back by replay
        from deskmind_hands.replay import _element
        px = Element(id="ocr:1", role="listitem", label="发送", source="pixels")
        self.assertEqual(px.to_json()["source"], "pixels")
        self.assertNotIn("source", Element(id="x", role="button").to_json())
        self.assertEqual(_element(px.to_json()).source, "pixels")
        self.assertEqual(_element(Element(id="x", role="button").to_json()).source, "ax")

    def test_perform_maps_actions_and_binds_them_to_the_last_observation(self):
        ex, stub = _executor()
        self.assertEqual(ex.perform({"kind": "click", "window": W12, "target": "B2"})["status"], "error")   # not observed yet
        ex.observe(W12)
        e0 = ex.epoch
        r = ex.perform({"kind": "set_value", "window": W12, "target": "T1", "value": "你好"})
        self.assertEqual(r["status"], "ok")
        self.assertEqual(ex.epoch, e0 + 1)
        a = stub.actions[-1]
        self.assertIs(a.kind, ActionKind.TYPE_TEXT)
        self.assertEqual((a.text, a.clear_first), ("你好", True))
        self.assertEqual((a.binding.observation_id, a.binding.element_id, a.binding.window_id), ("obs-1", "T1", "12"))
        r = ex.perform({"kind": "key", "window": W12, "chord": "cmd+s"})
        self.assertEqual(r["status"], "unverified")
        self.assertEqual(stub.actions[-1].keys, ("cmd", "s"))
        ex.perform({"kind": "select", "window": W12, "target": "P3", "option": "文稿"})
        self.assertEqual(stub.actions[-1].text, "文稿")
        self.assertEqual(ex.perform({"kind": "dance", "window": W12})["status"], "unsupported")


class Apps(unittest.TestCase):
    def test_resolve_app_names_no_app_in_code(self):
        running = [("文本编辑", TE), ("访达", "com.apple.finder")]
        installed = [("TextEdit", TE), ("文本编辑", TE), ("Safari", "com.apple.Safari")]
        self.assertEqual(resolve_app(TE, [], []), TE)                       # a bundle id is itself
        self.assertEqual(resolve_app("文本编辑", running, installed), TE)   # as the snapshot shows it
        self.assertEqual(resolve_app("textedit", [], installed), TE)       # installed, any case
        self.assertEqual(resolve_app("Safari", running, installed), "com.apple.Safari")   # not running, installed
        self.assertEqual(resolve_app("没有这个应用", running, installed), "")

    def test_installed_apps_reads_localized_names(self):
        if not os.path.isdir("/System/Applications/TextEdit.app"):
            self.skipTest("not a Mac with TextEdit")
        apps = installed_apps()
        names = {n for n, b in apps if b == TE}
        self.assertIn("TextEdit", names)
        self.assertIn("文本编辑", names)        # the English name and a localization, from the bundle itself

    def test_allow_flash_adds_once(self):
        with mock.patch.dict(os.environ, {"HANDS_FLASH_APPS": "com.apple.Safari"}):
            allow_flash(TE)
            allow_flash("com.apple.textedit")
            self.assertEqual(os.environ["HANDS_FLASH_APPS"], f"com.apple.Safari,{TE}")

    def test_user_active_yields_to_takeover(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"HANDS_TAKEOVER_FILE": str(Path(d) / "takeover-until")}):
            f = Path(d) / "takeover-until"
            self.assertEqual(takeover_until(), 0.0)                       # no file: no takeover
            f.write_text(str(time.time() + 60))
            self.assertGreater(takeover_until(), time.time())
            ex = HandsExecutor.__new__(HandsExecutor)                      # no desktop needed for this check
            self.assertFalse(ex.user_active())                             # handed over: the user is not "in the way"
            f.write_text(str(time.time() - 1))
            self.assertLess(takeover_until(), time.time())                 # expired: back to the keyboard/mouse rule


class Receipts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_perform_carries_the_drivers_files_receipt(self):
        ex, stub = _executor()
        ex.workspace = self.ws
        target = self.ws / "hello.txt"
        target.write_text("hi", encoding="utf-8")
        digest = hashlib.sha256(b"hi").hexdigest()
        stub.execute = lambda action: ExecResult(True, "saved 'hello.txt'", files=((str(target), digest),))
        ex.observe(W12)
        r = ex.perform({"kind": "click", "window": W12, "target": "B2"})
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["files"], [{"path": str(target), "sha256": digest}])
        self.assertNotIn("document", r)
        # a click that wrote nothing the driver knows of, in a window whose document is unknown: no receipt at all
        stub.execute = lambda action: ExecResult(True, "done")
        self.assertNotIn("files", ex.perform({"kind": "click", "window": W12, "target": "B2"}))
        # an action that cannot write a file: the receipt says so (files: [])
        self.assertEqual(ex.perform({"kind": "type", "window": W12, "target": "T1", "text": "x"})["files"], [])
        self.assertEqual(ex.perform({"kind": "wait", "ms": 1})["files"], [])

    def test_the_windows_document_is_credited_only_on_the_apps_own_word_with_the_digest_of_its_text(self):
        ex, stub = _executor()
        ex.workspace = self.ws
        doc = self.ws / "notes.txt"
        doc.write_text("v1", encoding="utf-8")
        state = {"now": (str(doc), True), "text": "v1"}   # what the app's scripting says: the document, unsaved, its text
        ex._document = lambda window, wins: state["now"]   # noqa: SLF001
        ex._content_digest = lambda window, wins: hashlib.sha256(state["text"].encode()).hexdigest() if state["text"] is not None else None   # noqa: SLF001
        ex.observe(W12)
        # a click that saves nothing: still unsaved after -> the document is reported, no file is credited
        r = ex.perform({"kind": "click", "window": W12, "target": "T1"})
        self.assertEqual(r["document"], str(doc))
        self.assertNotIn("files", r)

        # the file changed on disk during the action but the app does not say it saved (a sync program wrote it)
        def disk_only(action):
            doc.write_text("someone else's v2", encoding="utf-8")
            return ExecResult(True, "done")
        stub.execute = disk_only
        r = ex.perform({"kind": "click", "window": W12, "target": "B2"})
        self.assertNotIn("files", r)

        # cmd+S through the app: unsaved before, saved after -> the app's own word, credited with the digest of
        # what the app holds; a sync program writing over it at once makes upstream's match fail
        def save(action):
            doc.write_text("someone else's v4", encoding="utf-8")
            state["now"], state["text"] = (str(doc), False), "v3"
            return ExecResult(True, "pressed; could not be confirmed", indeterminate=True)
        stub.execute = save
        r = ex.perform({"kind": "key", "window": W12, "chord": "cmd+s"})
        self.assertEqual(r["status"], "unverified")
        self.assertEqual(r["files"], [{"path": str(doc), "sha256": hashlib.sha256(b"v3").hexdigest()}])
        self.assertNotEqual(r["files"][0]["sha256"], hashlib.sha256(doc.read_bytes()).hexdigest())
        # saved again with nothing to save: no flip, nothing credited
        stub.execute = lambda action: ExecResult(True, "done")
        self.assertNotIn("files", ex.perform({"kind": "key", "window": W12, "chord": "cmd+s"}))

        # a never-saved document (no path, unsaved) saved through the app's own panel under a new name, the app
        # giving no text: the path alone, told but not credited
        moved = self.ws / "renamed.txt"
        state["now"] = (None, True)

        def save_as(action):
            moved.write_text("v3", encoding="utf-8")
            state["now"], state["text"] = (str(moved), False), None
            return ExecResult(True, "done")
        stub.execute = save_as
        r = ex.perform({"kind": "click", "window": W12, "target": "B2"})
        self.assertEqual(r["files"], [{"path": str(moved)}])
        self.assertEqual(r["document"], str(moved))

        # only the path is known (accessibility, no scripting): never credited, only told
        state["now"] = (str(moved), None)
        stub.execute = disk_only
        r = ex.perform({"kind": "click", "window": W12, "target": "B2"})
        self.assertNotIn("files", r)
        self.assertEqual(r["document"], str(moved))

    def test_a_drivers_scratch_document_is_never_a_file_of_the_task(self):
        ex, stub = _executor()
        ex.workspace = self.ws
        scratch = self.ws / ".hands" / "未命名-1"
        scratch.parent.mkdir()
        scratch.write_text("", encoding="utf-8")
        state = {"now": (str(scratch), True)}
        ex._document = lambda window, wins: state["now"]   # noqa: SLF001 -- a new document's path until it is saved as
        ex._content_digest = lambda window, wins: "x"      # noqa: SLF001
        ex.observe(W12)

        def pressed(action):
            scratch.write_text("typed text", encoding="utf-8")   # cmd+S on a never-saved document went to the scratch
            state["now"] = (str(scratch), False)
            return ExecResult(True, "done")
        stub.execute = pressed
        r = ex.perform({"kind": "key", "window": W12, "chord": "cmd+s"})
        self.assertNotIn("files", r)
        self.assertEqual(r["document"], str(scratch))
        # typing never saves: the document is not even asked for
        ex._document = lambda window, wins: (_ for _ in ()).throw(AssertionError("asked"))   # noqa: SLF001
        r = ex.perform({"kind": "type", "window": W12, "target": "T1", "text": "more"})
        self.assertEqual(r["files"], [])
        self.assertNotIn("document", r)


class Windows(unittest.TestCase):
    def test_an_action_in_a_window_the_driver_is_not_on_is_refused_until_it_is_observed_again(self):
        """observe A, observe B, act in A: the shared driver holds B's snapshot and element ids (PR #48 review)."""
        ex, stub = _executor()
        ex.observe(W12)
        ex.observe(W13)
        r = ex.perform({"kind": "click", "window": W12, "target": "B2"})
        self.assertEqual(r["status"], "error")
        self.assertIn("observe it again", r["why"])
        self.assertEqual(stub.actions, [])
        ex.observe(W12)
        self.assertEqual(ex.perform({"kind": "click", "window": W12, "target": "B2"})["status"], "ok")
        self.assertEqual((stub._pinned_window, stub._window_id), ("12", "12"))
        self.assertEqual(ex.perform({"kind": "click", "window": W13, "target": "B2"})["status"], "error")

    def test_an_observation_that_landed_on_another_window_is_refused(self):
        """The driver followed another window (its read of the one asked for stayed incomplete): not an observation
        of the window asked for, and never filed as one (PR #48 review: observe(#100) answered with #200's content)."""
        ex, stub = _executor()
        stub.follow = "200"
        with self.assertRaises(RuntimeError) as cm:
            ex.observe(W12)
        self.assertIn("#200", str(cm.exception))
        self.assertNotIn(W12, ex.last_obs)
        self.assertEqual(ex.perform({"kind": "click", "window": W12, "target": "B2"})["status"], "error")
        self.assertEqual(stub.actions, [])
        stub.follow = None
        ex.observe(W12)
        self.assertEqual(ex.perform({"kind": "click", "window": W12, "target": "B2"})["status"], "ok")

    def test_only_the_acted_apps_windows_that_did_not_exist_before_are_new(self):
        """A window of another app appearing during the action is not this action's, and neither is a minimised
        window of the acted app that a launch or an `open` brought back (PR #48 review, both rounds)."""
        ex, stub = _executor()
        state = {"on": [_win("12")], "off": [_win("100", title="用户的笔记")]}   # #100: the user's, minimised
        ex._windows = lambda every=False: state["on"] + (state["off"] if every else [])   # noqa: SLF001
        ex.observe(W12)

        def restores(action):   # the action brought the minimised window back and another app opened one
            state["on"] = state["on"] + state["off"] + [_win("77", bundle="com.apple.Safari", title="News", app="Safari")]
            state["off"] = []
            return ExecResult(True, "done")
        stub.execute = restores
        r = ex.perform({"kind": "click", "window": W12, "target": "B2"})
        self.assertNotIn("new_window", r)

        def makes(action):      # a window that did not exist in any form before
            state["on"] = state["on"] + [_win("14", title="未命名 2")]
            return ExecResult(True, "done")
        stub.execute = makes
        r = ex.perform({"kind": "click", "window": W12, "target": "B2"})
        self.assertEqual(r["new_window"]["id"], f"{TE}#14")

        # open_window: the same rule -- the reopen restores the user's minimised window, which is not the task's
        state = {"on": [_win("12")], "off": [_win("101", title="用户的另一篇")]}
        ex._windows = lambda every=False: state["on"] + (state["off"] if every else [])   # noqa: SLF001

        from types import SimpleNamespace

        def opens(*a, **k):
            state["on"] = state["on"] + state["off"]
            state["off"] = []
            return SimpleNamespace(returncode=1, stdout="")
        with mock.patch("deskmind_hands.executor.subprocess.run", side_effect=opens), \
             mock.patch("deskmind_hands.executor.resolve_app", return_value=TE), \
             mock.patch("deskmind_hands.executor.running_apps", return_value=[("文本编辑", TE)]), \
             mock.patch("deskmind_hands.executor.time.sleep"):
            r = ex.perform({"kind": "open_window", "app": "TextEdit"})
        self.assertEqual(r["status"], "unverified")      # nothing the action made appeared
        self.assertNotIn("new_window", r)

    def test_a_failed_window_command_is_not_reported_as_done(self):
        """peekaboo's failure (window not found, timeout, no JSON) is an error upstream, not a closed or focused
        window (PR #48 review)."""
        ex, _ = _executor()
        ex._pk = lambda args: None   # noqa: SLF001 -- the command failed
        self.assertEqual(ex.perform({"kind": "close_window", "window": W12})["status"], "error")
        self.assertEqual(ex.perform({"kind": "focus_window", "window": W12})["status"], "error")
        ex._pk = lambda args: {}   # noqa: SLF001 -- succeeded with nothing to say
        self.assertEqual(ex.perform({"kind": "close_window", "window": W12})["status"], "ok")
        self.assertEqual(ex.perform({"kind": "focus_window", "window": W12})["status"], "ok")

    def test_pk_tells_failure_from_an_empty_answer(self):
        from types import SimpleNamespace
        ex, _ = _executor()
        with mock.patch("deskmind_hands.executor.subprocess.run", return_value=SimpleNamespace(stdout='{"success": false, "error": "Window not found"}')):
            self.assertIsNone(ex._pk(["window", "close"]))   # noqa: SLF001
        with mock.patch("deskmind_hands.executor.subprocess.run", return_value=SimpleNamespace(stdout='{"success": true}')):
            self.assertEqual(ex._pk(["window", "close"]), {})   # noqa: SLF001
        with mock.patch("deskmind_hands.executor.subprocess.run", return_value=SimpleNamespace(stdout="not json")):
            self.assertIsNone(ex._pk(["window", "close"]))   # noqa: SLF001


class Epoch(unittest.TestCase):
    def test_the_epoch_moves_when_the_user_or_the_windows_changed(self):
        """A snapshot after the user's input, or after a window appeared, vanished or was renamed, is a new epoch:
        an observation and a consent decided before it are stale upstream (PR #48 review: a delete approved for A
        ran on B the user had selected meanwhile)."""
        ex, stub = _executor()
        state = {"wins": [_win("12")], "input": 0.0}
        ex._windows = lambda every=False: state["wins"]   # noqa: SLF001
        ex._front = lambda: "文本编辑"                      # noqa: SLF001
        ex._last_input_at = lambda: state["input"]        # noqa: SLF001
        ex.observe(W12)                                   # the task operates TextEdit
        e0 = ex.snapshot()["epoch"]
        self.assertEqual(ex.snapshot()["epoch"], e0)                  # nothing happened
        state["input"] = time.time()                                  # the user touched the Mac after the epoch was set
        self.assertEqual(ex.snapshot()["epoch"], e0 + 1)
        self.assertEqual(ex.snapshot()["epoch"], e0 + 1)              # once per input
        ex._front = lambda: "DeskMind"                                 # noqa: SLF001 -- which app is in front is no change
        self.assertEqual(ex.snapshot()["epoch"], e0 + 1)
        state["wins"] = [_win("12", title="hello.txt")]              # a window renamed (a save-as)
        self.assertEqual(ex.snapshot()["epoch"], e0 + 2)
        state["wins"] = [_win("12", title="hello.txt"), _win("15")]   # one appeared
        self.assertEqual(ex.snapshot()["epoch"], e0 + 3)
        self.assertEqual(ex.snapshot()["epoch"], e0 + 3)
        # the action's own input is not the user's: a perform moves the epoch once, and the input it sent during
        # the action does not move it again at the next snapshot
        ex.observe(W12)

        def acts(action):
            state["input"] = time.time()
            return ExecResult(True, "done")
        stub.execute = acts
        ex.perform({"kind": "click", "window": W12, "target": "B2"})
        self.assertEqual(ex.epoch, e0 + 4)
        self.assertEqual(ex.snapshot()["epoch"], e0 + 4)


class TaskScope(unittest.TestCase):
    def test_windows_of_apps_the_task_is_not_operating_do_not_move_the_epoch(self):
        """A terminal retitling its window while the user types, or a window opening in another app, changes nothing
        the task decided on (owner, 10-20)."""
        ex, _ = _executor()
        state = {"wins": [_win("12"), _win("90", bundle="com.googlecode.iterm2", title="zsh", app="iTerm2")]}
        ex._windows = lambda every=False: state["wins"]   # noqa: SLF001
        ex._front = lambda: "iTerm2"                       # noqa: SLF001
        ex._last_input_at = lambda: 0.0                   # noqa: SLF001
        ex.observe(W12)
        e0 = ex.snapshot()["epoch"]
        state["wins"] = [_win("12"), _win("90", bundle="com.googlecode.iterm2", title="vim notes", app="iTerm2"),
                         _win("91", bundle="com.googlecode.iterm2", title="zsh", app="iTerm2")]
        self.assertEqual(ex.snapshot()["epoch"], e0)
        state["wins"] = [_win("12", title="hello.txt")] + state["wins"][1:]    # the task's own app: a change
        self.assertEqual(ex.snapshot()["epoch"], e0 + 1)

    def test_starting_to_operate_an_app_is_no_change(self):
        """begin -> observe -> snapshot: the first observe widens the watched apps; the snapshot after it must not
        stale the observation just returned (PR #49 review)."""
        ex, stub = _executor()
        state = {"wins": [_win("12")]}
        ex._windows = lambda every=False: state["wins"]   # noqa: SLF001
        ex._front = lambda: "文本编辑"                      # noqa: SLF001
        ex._last_input_at = lambda: 0.0                   # noqa: SLF001
        e0 = ex.snapshot()["epoch"]                       # begin: nothing operated yet
        ex.observe(W12)
        self.assertEqual(ex.snapshot()["epoch"], e0)
        self.assertEqual(ex.perform({"kind": "click", "window": W12, "target": "B2"})["status"], "ok")
        e1 = ex.epoch
        self.assertEqual(ex.snapshot()["epoch"], e1)
        state["wins"] = [_win("12"), _win("13", title="未命名 2")]   # then a real change of that app
        self.assertEqual(ex.snapshot()["epoch"], e1 + 1)

    def test_a_change_right_after_the_first_observe_is_seen(self):
        """begin -> first observe -> the window's title changes (a page navigating) -> snapshot: a new epoch, for the
        first app and for one the task starts operating later (PR #49 review)."""
        ex, _ = _executor()
        safari = "com.apple.Safari"
        state = {"wins": [_win("12"), _win("40", bundle=safari, title="A", app="Safari")]}
        ex._windows = lambda every=False: state["wins"]   # noqa: SLF001
        ex._front = lambda: "文本编辑"                      # noqa: SLF001
        ex._last_input_at = lambda: 0.0                   # noqa: SLF001
        e0 = ex.snapshot()["epoch"]
        ex.observe(W12)
        state["wins"] = [_win("12", title="hello.txt"), state["wins"][1]]
        self.assertEqual(ex.snapshot()["epoch"], e0 + 1)
        ex.observe(f"{safari}#40")                        # a second app, first looked at now
        self.assertEqual(ex.snapshot()["epoch"], e0 + 1)  # widening the watch is no change
        state["wins"] = [state["wins"][0], _win("40", bundle=safari, title="B", app="Safari")]
        self.assertEqual(ex.snapshot()["epoch"], e0 + 2)

    def test_a_change_while_the_first_observe_reads_is_seen(self):
        """The page navigates from A to B while the driver reads A: the baseline is the windows before the read, so
        the next snapshot sees B and the observation of A is stale (PR #49 review)."""
        ex, stub = _executor()
        state = {"wins": [_win("12", title="A")]}
        ex._windows = lambda every=False: state["wins"]   # noqa: SLF001
        ex._front = lambda: "文本编辑"                      # noqa: SLF001
        ex._last_input_at = lambda: 0.0                   # noqa: SLF001
        e0 = ex.snapshot()["epoch"]
        real = stub.observe

        def reads_then_navigates():
            obs = real()
            state["wins"] = [_win("12", title="B")]       # after the read, before observe returns
            return obs
        stub.observe = reads_then_navigates
        ex.observe(W12)
        self.assertEqual(ex.snapshot()["epoch"], e0 + 1)

    def test_input_counts_only_when_it_went_to_an_app_the_task_operates(self):
        from deskmind_hands.inputwatch import InputWatch
        ex, _ = _executor()
        del ex._last_input_at                              # the real one, over a watch fed by hand
        w = InputWatch()
        w.active, w.keys = True, True
        w._pid_bundle = {5: "com.googlecode.iterm2", 6: "com.apple.TextEdit", 7: "ai.deskmind.app"}
        w._pid_regular = {5: True, 6: True, 7: True}
        ex._watch, ex._watch_tried = w, True
        ex.observe(W12)
        w.note(5, at=100.0)                                # typing in a terminal
        w.note(7, at=101.0)                                # answering DeskMind
        self.assertEqual(ex._last_input_at(), 0.0)         # noqa: SLF001
        w.note(6, at=102.0)                                # a click in TextEdit, which the task operates
        self.assertEqual(ex._last_input_at(), 102.0)       # noqa: SLF001
        w.note(None, at=103.0)                             # target unknown: counts
        self.assertEqual(ex._last_input_at(), 103.0)       # noqa: SLF001


class EpochReason(unittest.TestCase):
    def test_every_snapshot_says_why_the_epoch_last_moved(self):
        """An action of the task's, the user's input to an app the task operates (which), or a window list that
        changed (what): a stale step upstream can say what made it stale (rc.9, 10-20)."""
        from deskmind_hands.inputwatch import InputWatch
        ex, _ = _executor()
        del ex._last_input_at
        w = InputWatch()
        w.active, w.keys = True, True
        w._pid_bundle = {6: "com.apple.TextEdit"}
        w._pid_regular = {6: True}
        ex._watch, ex._watch_tried = w, True
        state = {"wins": [_win("12")]}
        ex._windows = lambda every=False: state["wins"]   # noqa: SLF001
        ex._front = lambda: "文本编辑"                      # noqa: SLF001
        ex.snapshot()
        ex.observe(W12)
        ex.perform({"kind": "click", "window": W12, "target": "B2"})
        r = ex.snapshot()["epoch_reason"]
        self.assertEqual((r["by"], r["kind"], r["window"]), ("action", "click", W12))
        time.sleep(0.01)
        w.note(6, at=time.time())                          # the user clicked in TextEdit
        s = ex.snapshot()
        self.assertEqual(s["epoch_reason"]["by"], "input")
        self.assertEqual(s["epoch_reason"]["to"], "com.apple.textedit")
        self.assertEqual(s["epoch_reason"]["epoch"], s["epoch"])
        time.sleep(0.01)
        w.note(None, at=time.time())                       # a banner, a drag: unknown target
        self.assertEqual(ex.snapshot()["epoch_reason"]["to"], "unknown")
        state["wins"] = [_win("12", title="hello.txt"), _win("13", title="未命名 2")]
        r = ex.snapshot()["epoch_reason"]
        self.assertEqual(r["by"], "windows")
        self.assertEqual(r["app"], "com.apple.textedit")
        self.assertEqual(r["appeared"], [f"{TE}#13"])
        self.assertEqual(r["renamed"], [{"id": W12, "from": "未命名", "to": "hello.txt"}])


class UserActive(unittest.TestCase):
    def test_the_user_is_in_the_way_only_when_their_input_goes_to_an_app_the_task_operates(self):
        """Typing in a terminal held a task's first step for two minutes (rc.9, 10-20): with the keyboard watched by
        target, only input to the task's apps, or to an unknown target, counts."""
        from deskmind_hands.inputwatch import InputWatch
        ex, _ = _executor()
        w = InputWatch()
        w.active, w.keys = True, True
        w._pid_bundle = {5: "com.apple.Terminal", 6: "com.apple.TextEdit"}
        w._pid_regular = {5: True, 6: True}
        ex._watch, ex._watch_tried = w, True
        ex.observe(W12)                                    # the task operates TextEdit
        with mock.patch.dict(os.environ, {"HANDS_TAKEOVER_FILE": ""}):
            w.note(5, at=time.time())                      # typing in the terminal
            self.assertFalse(ex.user_active())
            w.note(6, at=time.time())                      # a click in TextEdit
            self.assertTrue(ex.user_active())
            w.last_by_bundle.clear()
            w.note(None, at=time.time())                   # a banner
            self.assertTrue(ex.user_active())
            w.last_unknown_at = time.time() - 10           # all of it long ago
            self.assertFalse(ex.user_active())
            w.keys = False                                 # no Input Monitoring: the global idle time, as before
            with mock.patch("deskmind_hands.executor.Quartz") as q:
                q.CGEventSourceSecondsSinceLastEventType.return_value = 0.5
                self.assertTrue(ex.user_active())


class NeedsForeground(unittest.TestCase):
    def test_apps_whose_steps_come_to_the_front_are_named_in_the_snapshot(self):
        ex, stub = _executor()
        ex._windows = lambda every=False: [_win("12")]   # noqa: SLF001
        ex._front = lambda: "文本编辑"                      # noqa: SLF001
        ex._last_input_at = lambda: 0.0                   # noqa: SLF001
        ex.drivers = {TE: stub}
        stub._foreground_actions = 0
        stub._vision_app = lambda app=None: False
        self.assertEqual(ex.snapshot()["needs_foreground"], [])
        stub._vision_app = lambda app=None: True          # read from its pixels
        self.assertEqual(ex.snapshot()["needs_foreground"], [TE])
        stub._vision_app = lambda app=None: False
        stub._foreground_actions = 1                      # a step had to bring it forward
        self.assertEqual(ex.snapshot()["needs_foreground"], [TE])


class ForegroundOK(unittest.TestCase):
    def test_a_step_the_layer_above_allows_may_bring_its_app_forward_for_that_step_only(self):
        """The app no longer names the task's apps (deskmind #96): an app that only takes input in front is allowed
        a flash for a step whose window is the task's or the user's consent covers (foreground_ok), and only then."""
        ex, stub = _executor()
        seen = []
        stub.execute = lambda action: seen.append(os.environ.get("HANDS_FLASH_APPS", "")) or ExecResult(True, "done")
        ex.observe(W12)
        with mock.patch.dict(os.environ, {"HANDS_FLASH_APPS": "com.apple.Safari"}):
            ex.perform({"kind": "click", "window": W12, "target": "B2", "foreground_ok": True})
            ex.perform({"kind": "click", "window": W12, "target": "B2"})
            self.assertEqual(os.environ["HANDS_FLASH_APPS"], "com.apple.Safari")   # put back
        self.assertEqual(seen, [f"com.apple.Safari,{TE}", "com.apple.Safari"])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HANDS_FLASH_APPS", None)
            ex.perform({"kind": "click", "window": W12, "target": "B2", "foreground_ok": True})
            self.assertNotIn("HANDS_FLASH_APPS", os.environ)
        self.assertEqual(seen[-1], TE)


class StopBeforeBegin(unittest.TestCase):
    def test_a_stop_that_comes_before_begin_is_applied_when_the_executor_is_made(self):
        """Read on its own thread, a stop can come while begin is still making the executor: kept, not "begin first"
        and lost (hands #54 review)."""
        import io
        import sys as _sys
        from deskmind_hands import executor as exe
        from deskmind_hands.grounding import STOP
        self.addCleanup(STOP.clear)
        lines = io.StringIO('{"id": 1, "op": "stop"}\n{"id": 2, "op": "begin"}\n{"id": 3, "op": "perform", "action": {"kind": "click", "window": "x#1", "target": "e"}}\n')
        out = io.StringIO()
        made = []

        class Fake:
            def __init__(self, *a, **k):
                self.stopped = False
                made.append(self)

            def stop(self):
                self.stopped = True
                return {"stopped": True}

            def snapshot(self):
                return {"epoch": 1}

            def perform(self, action):
                return {"status": "error", "why": "stopped by the app"} if self.stopped else {"status": "ok"}
        with mock.patch.object(_sys, "stdin", lines), mock.patch.object(_sys, "stdout", out), \
             mock.patch.object(exe, "HandsExecutor", Fake):
            exe.main()
        replies = [json.loads(l) for l in out.getvalue().splitlines()]
        by_id = {r["id"]: r for r in replies}
        self.assertTrue(by_id[1]["ok"])
        self.assertTrue(made[0].stopped)                          # applied as the executor was made
        self.assertEqual(by_id[3]["result"]["status"], "error")


class Sends(unittest.TestCase):
    def test_send_controls_are_known_by_their_verb(self):
        for role, label in [("button", "发送"), ("button", "Send"), ("button", "Send message"), ("button", "发送消息"),
                            ("button", "提交"), ("button", "Submit"), ("button", "发布"), ("button", "Post"),
                            ("button", "支付"), ("button", "Pay"), ("button", "下单"), ("button", "Place order"),
                            ("menuItem", "发送…"), ("button", "Tweet")]:
            self.assertTrue(sends(role, label), label)
        for role, label in [("button", "回复"), ("button", "Reply"), ("button", "新邮件"), ("button", "New Message"),
                            ("button", "保存"), ("button", "Sender"), ("textField", "发送"), ("staticText", "Send"),
                            ("button", ""), ("button", "Sent items")]:
            self.assertFalse(sends(role, label), label)

    def test_observe_exports_sends_on_a_send_control(self):
        ex, _ = _executor()
        by_id = {e["id"]: e for e in ex.observe(W12)["elements"]}
        self.assertTrue(by_id["B5"].get("sends"))       # 发送
        self.assertNotIn("sends", by_id["B6"])          # 回复 opens a form, sends nothing
        self.assertNotIn("sends", by_id["B2"])          # 保存


class BrowserFields(unittest.TestCase):
    def test_an_approved_write_into_a_field_is_the_drivers_one_step_allowance(self):
        """The layer above judged the step; the driver's guard against writing into a browser's own field takes
        that as the allowance it asks for (PR #48 review: an approved address-bar search was always refused)."""
        ex, stub = _executor()
        ex.observe(W12)
        ex.perform({"kind": "set_value", "window": W12, "target": "T1", "value": "weather"})
        ex.perform({"kind": "type", "window": W12, "target": "T1", "text": " singapore"})
        ex.perform({"kind": "select", "window": W12, "target": "P3", "option": "文稿"})
        self.assertEqual(stub.allowed, ["T1", "T1", "P3"])
        ex.perform({"kind": "click", "window": W12, "target": "B2"})
        self.assertEqual(stub.allowed, ["T1", "T1", "P3"])   # a click allows nothing


class OpenWindow(unittest.TestCase):
    """A Mac with one app: launched, it brings its last session's windows back (late, after the launch looked
    settled); asked by its scripting, it makes a document and says which window."""

    def _mac(self, ex, running: bool, restored: list[dict], answers: bool = True, late: bool = False, documents=None):
        from types import SimpleNamespace
        state = {"running": running, "on": [], "off": [], "n": 30000, "events": [], "pending": list(restored), "late": late}
        ex._windows = lambda every=False: state["on"] + (state["off"] if every else [])   # noqa: SLF001
        ex.LAUNCH_WAIT_S = 0.3
        # a restored window shows its document (the last session's file); `documents` overrides, by window id
        docs = documents if documents is not None else {w["id"]: (f"/Users/me/{w['title']}.txt", False) for w in restored}
        ex._document = lambda window, wins: docs.get(window)   # noqa: SLF001

        def run(argv, **k):
            state["events"].append(argv[0] if argv[0] != "open" else argv[:3])
            if argv[:2] == ["open", "-b"] and not state["running"]:
                state["running"] = True
                if not state["late"]:
                    state["on"], state["pending"] = state["on"] + state["pending"], []
            elif argv[0] == "osascript" and "make new document" in argv[-1]:
                # the session comes back now, late, whether or not the app answers the ask
                state["on"], state["pending"] = state["on"] + state["pending"], []
                if answers:
                    state["n"] += 1
                    state["on"] = state["on"] + [_win(str(state["n"]), title="未命名")]
                    return SimpleNamespace(returncode=0, stdout=f"{state['n']}\n")
                return SimpleNamespace(returncode=1, stdout="")
            return SimpleNamespace(returncode=0, stdout="")
        running_apps = lambda: [("文本编辑", TE)] if state["running"] else []   # noqa: E731
        return state, run, running_apps

    def _open(self, ex, run, running):
        with mock.patch("deskmind_hands.executor.subprocess.run", side_effect=run), \
             mock.patch("deskmind_hands.executor.running_apps", side_effect=running), \
             mock.patch("deskmind_hands.executor.resolve_app", return_value=TE), \
             mock.patch("deskmind_hands.executor.time.sleep"):
            return ex.perform({"kind": "open_window", "app": "文本编辑"})

    def test_the_window_the_app_says_it_made_is_the_tasks_a_restored_one_is_not(self):
        """PR #48 review, third and fourth rounds: the restored #18000 was taken for the task's; only the window the
        app's scripting returned for the document it made is."""
        ex, stub = _executor()
        state, run, running = self._mac(ex, running=False, restored=[_win("18000", title="上次的文稿")], late=True)
        r = self._open(ex, run, running)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["new_window"]["id"], f"{TE}#30001")          # the document asked for, not #18000
        self.assertEqual(state["events"][0], ["open", "-b", TE])         # launched on its own first
        self.assertIn("30001", stub.__dict__.get("_run_windows", set()))
        self.assertNotIn("18000", stub.__dict__.get("_run_windows", set()))

    def test_a_window_that_appeared_after_a_launch_without_proof_is_not_claimed(self):
        """The ask failed and the session came back late: the window is there to observe, but it is not the task's
        (PR #48 review, fourth round)."""
        ex, stub = _executor()
        state, run, running = self._mac(ex, running=False, restored=[_win("18000", title="上次的文稿")], answers=False, late=True)
        r = self._open(ex, run, running)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["window"], f"{TE}#18000")
        self.assertNotIn("new_window", r)
        self.assertNotIn("18000", stub.__dict__.get("_run_windows", set()))

    def test_a_window_that_appeared_in_an_app_already_running_is_the_tasks(self):
        """A running app brings no session back: a window that was not there before the ask was made by it."""
        ex, stub = _executor()
        state, run, running = self._mac(ex, running=True, restored=[], answers=False)
        state["on"] = [_win("12")]

        def keystroke_n(argv, **k):
            r = run(argv, **k)
            if argv[0] == "osascript" and "keystroke" in argv[-1]:
                state["on"] = state["on"] + [_win("14", title="未命名")]
            return r
        r = self._open(ex, keystroke_n, running)
        self.assertEqual(r["new_window"]["id"], f"{TE}#14")
        self.assertIn("14", stub.__dict__["_run_windows"])

    def test_an_app_slow_to_start_shows_a_window_the_task_opened_but_its_content_stays_the_users(self):
        """A music player, launched by this open: its main window came after the 8 s this used to wait (10-20).
        Waited for, and reported as opened_window -- the task may work in it and bring the app forward -- but not
        new_window: what is in it (a library, a mailbox) is the user's (hands #54 review). No new document is asked
        for, no cmd+N sent."""
        ex, stub = _executor()
        player = "com.example.player"
        state, run, running = self._mac(ex, running=False, restored=[_win("700", bundle=player, title="Player", app="Player")],
                                         answers=False, documents={})
        with mock.patch("deskmind_hands.executor.subprocess.run", side_effect=run), \
             mock.patch("deskmind_hands.executor.running_apps", side_effect=lambda: [("Player", player)] if state["running"] else []), \
             mock.patch("deskmind_hands.executor.resolve_app", return_value=player), \
             mock.patch("deskmind_hands.executor.is_browser", return_value=False), \
             mock.patch.dict(os.environ, {"HANDS_FLASH_APPS": ""}), \
             mock.patch("deskmind_hands.executor.time.sleep"):
            r = ex.perform({"kind": "open_window", "app": "Player"})
            self.assertIn(player, os.environ["HANDS_FLASH_APPS"])   # may be brought forward for its steps
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["window"], f"{player}#700")
        self.assertEqual(r["opened_window"]["id"], f"{player}#700")
        self.assertNotIn("new_window", r)
        self.assertNotIn("700", stub.__dict__.get("_run_windows", set()))
        self.assertNotIn("osascript", state["events"])

    def test_a_browser_launch_is_not_an_opened_window(self):
        ex, _ = _executor()
        state, run, running = self._mac(ex, running=False, restored=[_win("701", bundle="com.example.web", title="Tabs", app="Web")],
                                         answers=False, documents={})
        with mock.patch("deskmind_hands.executor.subprocess.run", side_effect=run), \
             mock.patch("deskmind_hands.executor.running_apps", side_effect=lambda: [("Web", "com.example.web")] if state["running"] else []), \
             mock.patch("deskmind_hands.executor.resolve_app", return_value="com.example.web"), \
             mock.patch("deskmind_hands.executor.is_browser", return_value=True), \
             mock.patch("deskmind_hands.executor.time.sleep"):
            r = ex.perform({"kind": "open_window", "app": "Web"})
        self.assertNotIn("opened_window", r)
        self.assertNotIn("new_window", r)

    def test_a_launch_that_makes_no_window_is_unverified(self):
        ex, _ = _executor()
        state, run, running = self._mac(ex, running=False, restored=[], answers=False)
        r = self._open(ex, run, running)
        self.assertEqual(r["status"], "unverified")

    def test_a_second_address_is_loaded_in_the_tasks_own_browser_window_not_ignored(self):
        """The driver gives the run's own browser window back as it is: the page asked for has to be loaded in it
        and checked, and it is not a new window (PR #48 review, fifth round)."""
        ex, stub = _executor()
        stub.BROWSERS = {"com.apple.safari"}
        pages = {"now": ""}
        state = {"on": [], "running": True}
        safari = "com.apple.Safari"

        def own_window(bundle, url=""):        # the driver: a window of the run's own, made on the first ask, kept after
            if not state["on"]:
                state["on"] = [_win("500", bundle=safari, title="A", app="Safari")]
                pages["now"] = url
                stub._own_browser_script = {"com.apple.safari": "500"}   # Safari's script id is the window server's
            return "500"
        stub._own_browser_window = own_window
        stub._load_in_own_window = lambda wid, url: pages.__setitem__("now", url) or True
        ex._windows = lambda every=False: state["on"]   # noqa: SLF001
        ex._page_of = lambda bundle, script_id: pages["now"] if script_id == "500" else ""  # noqa: SLF001

        def run(argv, **k):
            from types import SimpleNamespace
            return SimpleNamespace(returncode=0, stdout="")
        with mock.patch("deskmind_hands.executor.subprocess.run", side_effect=run), \
             mock.patch("deskmind_hands.executor.running_apps", return_value=[("Safari", safari)]), \
             mock.patch("deskmind_hands.executor.resolve_app", return_value=safari), \
             mock.patch("deskmind_hands.executor.is_browser", return_value=True), \
             mock.patch("deskmind_hands.executor.time.sleep"):
            # first: the driver opened the window on A -- a new window, the task's
            r1 = ex.perform({"kind": "open_window", "app": "Safari", "url": "https://a.example/"})
            self.assertEqual(r1["new_window"]["id"], f"{safari}#500")
            # second: the same window given back -- B is loaded in it and checked; not a new window
            r2 = ex.perform({"kind": "open_window", "app": "Safari", "url": "https://b.example/"})
            self.assertEqual(r2["status"], "ok")
            self.assertEqual(r2["window"], f"{safari}#500")
            self.assertNotIn("new_window", r2)
            self.assertEqual(pages["now"], "https://b.example/")
            # a load that does not go is said so
            stub._load_in_own_window = lambda wid, url: False
            r3 = ex.perform({"kind": "open_window", "app": "Safari", "url": "https://c.example/"})
            self.assertEqual(r3["status"], "unverified")
            self.assertEqual(r3["window"], f"{safari}#500")
            # a local page: the driver serves it over http, once, and that is the address expected back; a loader
            # given the file again would serve it again, on another port (sixth and seventh rounds)
            ports = iter([52689, 52705, 52801])

            def serve(url):
                self.assertTrue(url.startswith("file:"))
                return f"http://127.0.0.1:{next(ports)}/page.html"
            given = []

            def load(wid, url):
                given.append(url)
                pages["now"] = serve(url) if url.startswith("file:") else url
                return True
            stub._serve_page, stub._load_in_own_window = serve, load
            r4 = ex.perform({"kind": "open_window", "app": "Safari", "url": "file:///ws/page.html"})
            self.assertEqual(r4["status"], "ok")
            self.assertEqual(given, ["http://127.0.0.1:52689/page.html"])      # the served address, not the file

    def test_a_chromium_window_is_addressed_by_the_browsers_own_id_never_the_window_servers(self):
        """Chrome's AppleScript `window id` is the browser's own number: the window server's id names some other
        window there (PR #48 review, sixth round: the user's window with script id 42 was navigated)."""
        ex, stub = _executor()
        chrome = "com.google.Chrome"
        stub.BROWSERS = {"com.google.chrome"}
        state = {"on": []}
        pages = {}                                     # script id -> page
        sent = []

        def own_window(bundle, url=""):
            if not state["on"]:
                state["on"] = [_win("42", bundle=chrome, title="A", app="Google Chrome")]   # window server id 42
                stub._own_browser_script = {"com.google.chrome": "1234"}                       # Chrome's own id 1234
                pages["1234"] = url
                pages["42"] = "https://the-users-window.example/"                              # another window, script id 42
            return "42"
        stub._own_browser_window = own_window
        ex._windows = lambda every=False: state["on"]   # noqa: SLF001

        def run(argv, **k):
            from types import SimpleNamespace
            import re
            script = argv[-1]
            m = re.search(r'set URL of (?:active|current) tab of window id (\d+) to "([^"]+)"', script)
            if m:
                sent.append(m.group(1))
                pages[m.group(1)] = m.group(2)
                return SimpleNamespace(returncode=0, stdout="")
            m = re.search(r"URL of (?:current|active) tab of window id (\d+)", script)
            if m:
                return SimpleNamespace(returncode=0, stdout=pages.get(m.group(1), "") + "\n")
            return SimpleNamespace(returncode=0, stdout="")
        with mock.patch("deskmind_hands.executor.subprocess.run", side_effect=run), \
             mock.patch("deskmind_hands.executor.running_apps", return_value=[("Google Chrome", chrome)]), \
             mock.patch("deskmind_hands.executor.resolve_app", return_value=chrome), \
             mock.patch("deskmind_hands.executor.is_browser", return_value=True), \
             mock.patch("deskmind_hands.executor.time.sleep"):
            r1 = ex.perform({"kind": "open_window", "app": "Chrome", "url": "https://a.example/"})
            self.assertEqual(r1["new_window"]["id"], f"{chrome}#42")
            r2 = ex.perform({"kind": "open_window", "app": "Chrome", "url": "https://b.example/"})
        self.assertEqual(r2["status"], "ok")
        self.assertEqual(sent, ["1234"])                                   # the task's window, by Chrome's own id
        self.assertEqual(pages["42"], "https://the-users-window.example/")   # the user's window untouched
        self.assertEqual(pages["1234"], "https://b.example/")
        # without the script id nothing is sent, and the load is not claimed
        stub._own_browser_script = {}
        with mock.patch("deskmind_hands.executor.subprocess.run", side_effect=run), \
             mock.patch("deskmind_hands.executor.running_apps", return_value=[("Google Chrome", chrome)]), \
             mock.patch("deskmind_hands.executor.resolve_app", return_value=chrome), \
             mock.patch("deskmind_hands.executor.is_browser", return_value=True), \
             mock.patch("deskmind_hands.executor.time.sleep"):
            r3 = ex.perform({"kind": "open_window", "app": "Chrome", "url": "https://c.example/"})
        self.assertEqual(r3["status"], "unverified")
        self.assertEqual(sent, ["1234"])

    def test_a_window_an_action_opened_is_registered_with_the_driver(self):
        """The driver started after the window was made had it among the user's windows and refused to use it
        (PR #48 review, third round)."""
        ex, stub = _executor()
        stub._preexisting = {"14"}        # what the driver, started late, took for the user's
        state = {"on": [_win("12")]}
        ex._windows = lambda every=False: state["on"]   # noqa: SLF001
        ex.observe(W12)

        def makes(action):
            state["on"] = state["on"] + [_win("14", title="未命名 2")]
            return ExecResult(True, "done")
        stub.execute = makes
        r = ex.perform({"kind": "click", "window": W12, "target": "B2"})
        self.assertEqual(r["new_window"]["id"], f"{TE}#14")
        self.assertIn("14", stub.__dict__["_run_windows"])
        self.assertNotIn("14", stub._preexisting)


if __name__ == "__main__":
    unittest.main()
