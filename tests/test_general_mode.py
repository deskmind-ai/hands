"""General mode (`hands do`, the app's path) and the gym: pure logic, no desktop, no model.

What 09-28/29 fixed or added, each pinned by the failure it came from: other apps' windows offered to a `do` run
(a Safari-to-TextEdit request), the run's own documents told apart from same-titled ones, a three-app cycle stopped, a newly opened
window followed, the ambiguity question's distinct lines, ANSWER candidates split into fields, vision controls kept
out of the page text, the decision panel's top operations -- and the gym oracles and traps.
"""
from __future__ import annotations

import os
import sys
import types
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.adapters.systemone import SystemOneAdapter, ambiguity_question, answer_candidates, top_operations
from deskmind_hands.cli import task_apps
from deskmind_hands.drivers.base import Element, ExecResult, NothingSent
from deskmind_hands.drivers.peekaboo import PeekabooDriver, fresh_windows, parse_textedit_windows, window_key
from deskmind_hands.geometry import Rect
from deskmind_hands.runtime.loop import is_cycling


class DoRunWindows(unittest.TestCase):
    def test_every_named_app_is_offered(self):
        # Safari -> TextEdit had no way to TextEdit (0/5); the same strings in the task harness 3/3.
        apps = {"Safari": "com.apple.Safari", "TextEdit": "com.apple.TextEdit"}
        self.assertEqual(task_apps([], apps, "com.apple.Safari"), ["com.apple.TextEdit"])
        self.assertEqual(task_apps(["TextEdit"], {"TextEdit": "com.apple.TextEdit"}, "com.apple.finder"),
                         ["TextEdit", "com.apple.TextEdit"])
        self.assertEqual(task_apps([], {}, "com.apple.Safari"), [])

    def test_own_documents_by_path_and_position(self):
        # The user's own files are open before the run; an older run's parts.csv shares the title.
        root = tempfile.mkdtemp()
        mine, other = os.path.join(root, "parts.csv"), "/tmp/elsewhere/parts.csv"
        out = f"parts.csv\t{mine}\t214\t112\nparts.csv\t{other}\t185\t83\nuntitled\t\t10\t10\n"
        ours = parse_textedit_windows(out, root)
        self.assertEqual(ours, {("parts.csv", 214, 112)})
        self.assertIn(window_key({"window_title": "parts.csv", "bounds": {"x": 214, "y": 112}}), ours)
        self.assertNotIn(window_key({"window_title": "parts.csv", "bounds": {"x": 185, "y": 83}}), ours)
        self.assertEqual(parse_textedit_windows("garbage\nx\ty\n", root), set())

    def test_prefix_of_the_folder_is_not_inside_it(self):
        root = tempfile.mkdtemp()
        out = f"a.csv\t{root}-other/a.csv\t1\t1\n"
        self.assertEqual(parse_textedit_windows(out, root), set())

    def test_follow_the_window_an_action_opened(self):
        # A folder double-click opened a second Finder window; the pinned one stayed "AX tree incomplete".
        self.assertEqual(fresh_windows({"1", "2", "5"}, {"1", "2"}, "1"), ["5"])
        self.assertEqual(fresh_windows({"1"}, {"1"}, "1"), [])
        self.assertEqual(fresh_windows({"1", "7", "9"}, {"1"}, "1"), ["7", "9"])   # newest last


class UsersDocumentsKept(unittest.TestCase):
    """Clearing TextEdit before a staged task closes only documents in the runs directory: `close every document
    saving no` threw away the user's unsaved documents from the app's Try examples (10-02)."""

    def _driver(self, ws):
        d = PeekabooDriver.__new__(PeekabooDriver)
        d.ws = Path(ws)
        d.scripts = []
        d._osa = lambda script: d.scripts.append(script) or (True, "")
        return d

    def test_only_documents_under_the_runs_directory(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {"DESKMIND_RUNS_DIR": ""}), \
                mock.patch("deskmind_hands.drivers.peekaboo._app_running", lambda name: True), \
                mock.patch("time.sleep", lambda s: None):
            runs = Path(t) / "work" / "runs"
            d = self._driver(runs / "20261002-run" / "G04-chinese-exact-r1" / "ws")
            d._close_all_windows("com.apple.TextEdit")
        self.assertTrue(d.scripts)
        for script in d.scripts:
            self.assertNotIn("close every document saving no", script)
            self.assertIn("whose path starts with", script)
        self.assertTrue(any(f'"{runs}/"' in sc or f'"{os.path.realpath(runs)}/"' in sc for sc in d.scripts))

    def test_the_runs_directory_from_the_environment(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"DESKMIND_RUNS_DIR": "/tmp/x/runs"}):
            d = self._driver("/elsewhere/ws")
            self.assertIn("/tmp/x/runs/", d._runs_roots())


class Cycling(unittest.TestCase):
    def test_three_app_cycle_is_stuck(self):
        cycle = [("focus_app", "ncm", None), ("type_text", "PN-5581", "搜索框"), ("focus_app", "textedit", None),
                 ("focus_window", "safari", None)]
        self.assertTrue(is_cycling(cycle * 4))

    def test_progress_is_not(self):
        steps = [("type_text", f"row {i}", "doc") for i in range(20)]
        self.assertFalse(is_cycling(steps))
        self.assertFalse(is_cycling([("click", None, "x")] * 11))            # too short to judge
        mixed = [("click", None, "a")] * 6 + [("type_text", "new", "b")]
        self.assertFalse(is_cycling(mixed * 2))


class Questions(unittest.TestCase):
    def test_a_record_shown_twice_is_one_choice(self):
        t = "客户,单号,金额\n李娜,R-2291,1340\n李娜,R-3307,96\n李娜,R-2291,1340\n李娜,R-3307,96"
        q = ambiguity_question("请把「李娜的那笔订单」追加进 ledger.csv", t)
        self.assertEqual(q, "「李娜的那笔订单」在屏幕上不止一处：李娜,R-2291,1340；李娜,R-3307,96。应该用哪一个？")

    def test_a_unique_record_shown_twice_is_not_ambiguous(self):
        self.assertIsNone(ambiguity_question("请把「王磊的那笔订单」追加", "王磊,R-1180,560\n王磊,R-1180,560"))


class Answers(unittest.TestCase):
    def test_fields_and_values_are_candidates(self):
        c = answer_candidates(["歌手：逃跑计划 · 单曲：65 粉丝：125.4万", "光年之外 · 03:55"])
        for want in ("逃跑计划", "歌手：逃跑计划", "光年之外", "03:55", "歌手：逃跑计划 · 单曲：65 粉丝：125.4万"):
            self.assertIn(want, c)

    def test_long_lines_and_duplicates_out(self):
        c = answer_candidates(["x" * 80, "a", "a", ""])
        self.assertEqual(c, ["a"])


class PageText(unittest.TestCase):
    def test_hands_own_controls_are_not_text(self):
        obs = SimpleNamespace(elements=[
            Element(id="icon:scroll_up", role="button", label="向上滚动页面"),
            Element(id="icon:搜索框", role="textField", label="搜索框", value="林夏 纸船"),
            Element(id="gen:播放按钮", role="button", label="播放按钮"),
            Element(id="ocr:1", role="listitem", label="纸船 · 林夏")])
        text = SystemOneAdapter._page_text(obs)
        self.assertNotIn("向上滚动页面", text)
        self.assertNotIn("播放按钮", text)
        self.assertIn("林夏 纸船", text)          # what the search box holds is on the screen
        self.assertIn("纸船 · 林夏", text)


class WorksReadElsewhere(unittest.TestCase):
    def test_the_song_named_in_an_email_is_a_value(self):
        # "Play the song X recommended in her email": the title is in the email, typed in the music app's search.
        # Offered only the goal's words, the planner could search for nothing but the email's subject.
        from deskmind_hands.adapters.systemone import value_candidates
        goal = "沈一舟在Mailora里给我推荐了一首歌（“路上听的”那封），去Chimevale里播放它"
        vals = value_candidates(goal, "收件箱\n推荐你听：《灯塔》— 唐川\n谢谢")
        self.assertIn("唐川 灯塔", vals)
        self.assertLess(vals.index("路上听的"), vals.index("唐川 灯塔"))      # the goal's own first
        en = value_candidates('Wren Ash recommended a song in Mailora (the "A song for you" email): play it in '
                              'Chimevale', 'Hi,\nYou should hear "Lanterns" by Mira Stone\nThanks')
        self.assertIn("Mira Stone Lanterns", en)

    def test_not_for_other_goals(self):
        from deskmind_hands.adapters.systemone import value_candidates
        self.assertNotIn("灯塔", value_candidates("把 notes.txt 里的内容复制到 a.txt", "《灯塔》— 唐川"))


class ScreenRects(unittest.TestCase):
    def test_window_local_rect_on_the_screen(self):
        # A recording of the whole screen is drawn on afterwards (a dot where each action landed): the trace keeps
        # the target in screen points, the window's origin added to the element's window-local rect.
        from deskmind_hands.geometry import Point
        self.assertEqual(Rect(1025, 368.25, 117, 18).on_screen(Point(58, 35)), [1083.0, 403.2, 117.0, 18.0])
        d = PeekabooDriver.__new__(PeekabooDriver)
        d._rects, d._origin = {"ocr:31": Rect(10, 20, 30, 40)}, Point(100, 200)
        self.assertEqual(d.screen_rect("ocr:31"), [110.0, 220.0, 30.0, 40.0])
        self.assertIsNone(d.screen_rect("ocr:99"))
        self.assertIsNone(d.screen_rect(None))


class OverlayPaths(unittest.TestCase):
    def test_runs_tasks_and_fixtures_from_the_environment(self):
        # A private overlay drives this package and keeps its runs, task sets and fixtures in its own repo.
        import importlib
        import deskmind_hands.cli as cli
        keys = ("DESKMIND_RUNS_DIR", "DESKMIND_TASKS_DIR", "DESKMIND_FIXTURES_DIR")
        saved = {k: os.environ.get(k) for k in keys}
        try:
            with tempfile.TemporaryDirectory() as d:
                for k, sub in zip(keys, ("runs", "tasks", "fixtures")):
                    os.environ[k] = os.path.join(d, sub)
                importlib.reload(cli)
                self.assertEqual((cli.RUNS_DIR, cli.TASKS_DIR, cli.FIXTURES_DIR),
                                 tuple(Path(d) / s for s in ("runs", "tasks", "fixtures")))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            importlib.reload(cli)
        self.assertEqual(cli.RUNS_DIR, cli.REPO / "runs")


class BrowserPage(unittest.TestCase):
    def test_controls_only_is_not_the_page(self):
        # The first read of Safari in the app held its toolbar and tabs only; the table came a moment later.
        d = PeekabooDriver.__new__(PeekabooDriver)
        d.app = "com.apple.Safari"
        chrome = {"ui_elements": [{"role": r} for r in ("button", "textField", "group", "window", "tabGroup", "toolbar",
                                                          "menuButton")]}
        self.assertTrue(d._page_not_read(chrome))
        page = {"ui_elements": chrome["ui_elements"] + [{"role": "other", "label": "PN-7713"}]}
        self.assertFalse(d._page_not_read(page))
        d.app = "com.apple.TextEdit"
        self.assertFalse(d._page_not_read(chrome))


class BackgroundInput(unittest.TestCase):
    def test_webkit_apps_only_and_switchable(self):
        # Safari and the gym's host take background input; the music app (CEF) and the chat app (Electron) do not.
        d = PeekabooDriver.__new__(PeekabooDriver)
        from deskmind_hands.drivers import skylight
        on = skylight.available()
        self.assertEqual(d._background_input("com.apple.Safari"), on)
        self.assertEqual(d._background_input("ai.deskmind.gymhost"), on)
        self.assertFalse(d._background_input("com.netease.163music"))
        self.assertFalse(d._background_input("com.tinyspeck.slackmacgap"))
        os.environ["HANDS_BG_INPUT"] = "0"
        try:
            self.assertFalse(d._background_input("com.apple.Safari"))
        finally:
            del os.environ["HANDS_BG_INPUT"]


class TruncatedTree(unittest.TestCase):
    def test_a_cut_tree_is_said_to_the_planner(self):
        from deskmind_hands.drivers.peekaboo import TRUNCATED_NOTE, _mcp_see_as_cli_data, ax_truncation_note
        # The CLI's truncation object, and the MCP text's warning line.
        self.assertEqual(ax_truncation_note({"truncation": {"max_element_count_reached": True,
                                                            "max_depth_reached": False}}), TRUNCATED_NOTE)
        mcp = _mcp_see_as_cli_data({"ui_elements": []}, "Application: X\nWarning: AX tree truncated at element "
                                                        "count 5. Retry with larger max_elements tool arguments.")
        self.assertEqual(ax_truncation_note(mcp), TRUNCATED_NOTE)
        # A whole tree says nothing.
        self.assertIsNone(ax_truncation_note({"truncation": {"max_element_count_reached": False}}))
        self.assertIsNone(ax_truncation_note(_mcp_see_as_cli_data({"ui_elements": []}, "Application: X")))


class ReleasedPeekabooRoles(unittest.TestCase):
    def test_a_coarse_role_is_named_from_the_ax_role(self):
        from deskmind_hands.drivers.peekaboo import _mcp_see_as_cli_data
        # Released Peekaboo: role is a category ("other" for text, rows, tables), ax_role the AX name.
        released = [{"id": "elem_1", "role": "other", "ax_role": "AXRow", "label": "row"},
                    {"id": "elem_2", "role": "other", "ax_role": "AXStaticText", "label": "旧信"},
                    {"id": "elem_3", "role": "button", "ax_role": "AXButton", "label": "搜索"}]
        # The builds before it: role is the AX name itself.
        earlier = [{"id": "elem_1", "role": "AXRow", "label": "row"}]
        roles = [(e["role"], e["ax_role"]) for e in _mcp_see_as_cli_data({"ui_elements": released}, "")["ui_elements"]]
        self.assertEqual(roles, [("row", "AXRow"), ("staticText", "AXStaticText"), ("button", "AXButton")])
        self.assertEqual(_mcp_see_as_cli_data({"ui_elements": earlier}, "")["ui_elements"][0]["role"], "row")
        # No AX name at all: left as it came.
        self.assertEqual(_mcp_see_as_cli_data({"ui_elements": [{"role": "other"}]}, "")["ui_elements"][0]["role"], "other")


class WindowCapture(unittest.TestCase):
    def test_screencapturekit_first_screencapture_if_it_fails(self):
        from unittest import mock
        from deskmind_hands.drivers import capture
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "w.png"
            with mock.patch.object(capture, "capture_sck", return_value=True) as sck, \
                    mock.patch.object(capture, "capture_screencapture", return_value=True) as sc:
                self.assertTrue(capture.capture_window("42", out))
                sck.assert_called_once_with(42, out)
                sc.assert_not_called()
            with mock.patch.object(capture, "capture_sck", return_value=False), \
                    mock.patch.object(capture, "capture_screencapture", return_value=True) as sc:
                self.assertTrue(capture.capture_window(42, out))
                sc.assert_called_once_with(42, out)
            with mock.patch.object(capture, "_sck", return_value=None):
                self.assertFalse(capture.capture_sck(42, out))   # no ScreenCaptureKit: not tried


class TruncatedTree(unittest.TestCase):
    def test_a_cut_tree_is_said_to_the_planner(self):
        from deskmind_hands.drivers.peekaboo import TRUNCATED_NOTE, _mcp_see_as_cli_data, ax_truncation_note
        # The CLI's truncation object, and the MCP text's warning line.
        self.assertEqual(ax_truncation_note({"truncation": {"max_element_count_reached": True,
                                                            "max_depth_reached": False}}), TRUNCATED_NOTE)
        mcp = _mcp_see_as_cli_data({"ui_elements": []}, "Application: X\nWarning: AX tree truncated at element "
                                                        "count 5. Retry with larger max_elements tool arguments.")
        self.assertEqual(ax_truncation_note(mcp), TRUNCATED_NOTE)
        # A whole tree says nothing.
        self.assertIsNone(ax_truncation_note({"truncation": {"max_element_count_reached": False}}))
        self.assertIsNone(ax_truncation_note(_mcp_see_as_cli_data({"ui_elements": []}, "Application: X")))


class WindowCapture(unittest.TestCase):
    def test_screencapturekit_first_screencapture_if_it_fails(self):
        from unittest import mock
        from deskmind_hands.drivers import capture
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "w.png"
            with mock.patch.object(capture, "capture_sck", return_value=True) as sck, \
                    mock.patch.object(capture, "capture_screencapture", return_value=True) as sc:
                self.assertTrue(capture.capture_window("42", out))
                sck.assert_called_once_with(42, out)
                sc.assert_not_called()
            with mock.patch.object(capture, "capture_sck", return_value=False), \
                    mock.patch.object(capture, "capture_screencapture", return_value=True) as sc:
                self.assertTrue(capture.capture_window(42, out))
                sc.assert_called_once_with(42, out)
            with mock.patch.object(capture, "_sck", return_value=None):
                self.assertFalse(capture.capture_sck(42, out))   # no ScreenCaptureKit: not tried


class GymDisplay(unittest.TestCase):
    def test_host_on_a_given_display(self):
        from tools.gym.run import host_args
        self.assertEqual(host_args(["http://a"], env={}), ["http://a"])
        self.assertEqual(host_args(["http://a", "http://b"], env={"HANDS_GYM_DISPLAY": "4"}),
                         ["--display", "4", "http://a", "http://b"])
        self.assertEqual(host_args(["http://a"], env={"HANDS_GYM_DISPLAY": "x"}), ["http://a"])   # not an id: ignored


class FormFromReceipt(unittest.TestCase):
    """D5: a form filled in from a receipt image. OCR gives words; the values are lines, a label's value, dates."""

    def _receipt(self):
        from deskmind_hands.geometry import Rect
        words = [("BLUE", 100, 50), ("HARBOR", 170, 50), ("CAFE", 270, 51), ("Subtotal", 60, 400), ("34.50", 300, 400),
                 ("TOTAL", 40, 450), ("37.26", 320, 451), ("Printed", 60, 480), ("2026-09-28", 160, 480),
                 ("Date", 60, 500), ("of", 110, 500), ("visit", 140, 500), ("2026-09-27", 200, 501),
                 ("Category:", 180, 560), ("Meals", 300, 560)]
        return [Element(id=f"ocr:{i}", role="text", label=w, rect=Rect(x, y, 40, 20)) for i, (w, x, y) in enumerate(words)]

    def test_words_back_into_lines(self):
        from deskmind_hands.adapters.systemone import ocr_lines
        lines = ocr_lines(self._receipt()).splitlines()
        self.assertIn("BLUE HARBOR CAFE", lines)
        self.assertIn("Date of visit 2026-09-27", lines)
        self.assertIn("Category: Meals", lines)
        self.assertEqual(ocr_lines([Element(id="elem_1", role="button", label="Submit")]), "")   # not OCR: nothing
        # A block joined by the vision reader is placed by its first piece; the rest are lines of their own.
        from deskmind_hands.geometry import Rect
        blocks = [Element(id="ocr:2", role="text", label="GREEN", rect=Rect(188, 109, 138, 38)),
                  Element(id="ocr:3", role="text", label="LEAF · Court, Westfield · Operated by: Westfield Group",
                          rect=Rect(366, 109, 107, 38)),
                  Element(id="ocr:4", role="text", label="BISTRO", rect=Rect(509, 109, 167, 38))]
        got = ocr_lines(blocks).splitlines()
        self.assertEqual(got[0], "GREEN LEAF BISTRO")
        self.assertIn("Operated by: Westfield Group", got)

    def test_form_values_offered(self):
        from deskmind_hands.adapters.systemone import ocr_lines, value_candidates
        vals = value_candidates("Preview has a receipt open. Fill in the expense report in Safari from it and submit.",
                                ocr_lines(self._receipt()), limit=20)
        for want in ("Blue Harbor Cafe", "2026-09-27", "2026-09-28", "Meals", "37.26", "34.50"):
            self.assertIn(want, vals)
        self.assertNotIn("Total 37.26", vals)   # a heading with a number is not a name

    def test_dates_in_the_other_forms(self):
        from deskmind_hands.adapters.systemone import _date_forms
        self.assertEqual(sorted(_date_forms("2026年9月27日")), ["09/27/2026", "2026-09-27"])
        self.assertEqual(_date_forms("09/27/2026"), ["2026-09-27"])
        self.assertEqual(_date_forms("2026-09-27"), ["09/27/2026"])
        self.assertEqual(_date_forms("2026-13-40"), [])

    def test_only_for_form_goals(self):
        from deskmind_hands.adapters.systemone import ocr_lines, value_candidates
        vals = value_candidates("Rename the file to notes.txt", ocr_lines(self._receipt()), limit=20)
        self.assertNotIn("Blue Harbor Cafe", vals)


class WebFieldTyping(unittest.TestCase):
    """A web field in an app that takes background input is clicked and pasted into, not written through
    accessibility: the accessibility write went first and its fallback paste landed in the previously focused field
    (the merchant got the date, D5)."""

    def _driver(self, app, background):
        from types import SimpleNamespace
        d = PeekabooDriver.__new__(PeekabooDriver)
        d.app, d._rects, d._element_role = app, {"elem_12": object()}, {"elem_12": "textField"}
        d._element_value, d._labels_by_id = {"elem_12": ""}, {"elem_12": "Merchant"}
        d._snapshot, d.calls = "s1", []
        d._background_input = lambda bundle: background
        d._vision_act = lambda action, submit=True: d.calls.append(("paste", action.text, submit)) or "pasted"
        d._mcp = SimpleNamespace(tool=lambda name, args: d.calls.append((name, args.get("value"))) or
                                 (True, {"effect": "confirmed"}, "", ""))
        d._settle = lambda ok, detail: (ok, detail)
        return d

    def _type(self, d):
        from deskmind_hands.actions import Action, ActionKind, Binding
        return d._execute_inner(Action(kind=ActionKind.TYPE_TEXT, text="Blue Harbor Cafe", clear_first=True,
                                       binding=Binding(element_id="elem_12")))

    def test_background_web_field_is_pasted(self):
        d = self._driver("com.apple.Safari", True)
        self.assertEqual(self._type(d), "pasted")
        self.assertEqual(d.calls, [("paste", "Blue Harbor Cafe", False)])   # no accessibility write, no Return

    def test_other_apps_keep_the_accessibility_write(self):
        d = self._driver("com.apple.TextEdit", False)
        self._type(d)
        self.assertEqual(d.calls, [("set_value", "Blue Harbor Cafe")])


class PopupSelect(unittest.TestCase):
    """A web <select> reads as an AXPopUpButton with only its value: its choices are read through accessibility and
    offered for SELECT, and a SELECT is made in its menu (D5's category could never be chosen)."""

    def _driver(self):
        from deskmind_hands.geometry import Point
        d = PeekabooDriver.__new__(PeekabooDriver)
        d.app, d._popups, d._labels_by_id = "com.apple.Safari", {}, {"elem_22": "Category"}
        d._vision_app = lambda app=None: False
        d._pid_of = lambda bundle, all_wins=None: 123
        d._settle = lambda ok, detail: (ok, detail)
        d._hid_idle, d._takeover = (lambda: 10.0), (lambda: False)   # the user is not using the Mac
        return d, Point(2688, 30)

    def _stub(self, calls):
        from unittest import mock
        from deskmind_hands.drivers import axpopup
        pops = [{"el": "E", "title": "Category", "value": "Choose…", "frame": (2736.0, 575.0, 520.0, 57.0)}]
        return mock.patch.multiple(
            axpopup, popups=lambda pid: pops,
            options=lambda el: calls.append(("options", el)) or ["Choose…", "Travel", "Meals"],
            select=lambda el, opt: calls.append(("select", el, opt)) or (True, f"chose {opt!r} (verified)"))

    def _popup(self):
        from deskmind_hands.geometry import Rect
        return Element(id="elem_22", role="button", ax_role="AXPopUpButton", label="Category", value="Choose…",
                       rect=Rect(48, 545, 520, 57), window_id="w1")

    def test_options_attached_without_the_prompt_and_cached(self):
        d, origin = self._driver()
        calls = []
        with self._stub(calls):
            e = self._popup()
            d._attach_popup_options([e], origin)
            self.assertEqual(e.options, ["Travel", "Meals"])
            self.assertIn("elem_22", d._popups)
            e2 = self._popup()
            d._attach_popup_options([e2], origin)   # the same pop-up again: its menu is not opened twice
            self.assertEqual(e2.options, ["Travel", "Meals"])
        self.assertEqual([c[0] for c in calls], ["options"])

    def test_select_goes_to_the_menu(self):
        from deskmind_hands.actions import Action, ActionKind, Binding
        d, origin = self._driver()
        calls = []
        with self._stub(calls):
            d._attach_popup_options([self._popup()], origin)
            d._snapshot, d._mcp = "s1", object()
            d._execute_projected = lambda action: None
            ok, detail = d._execute_inner(Action(kind=ActionKind.TYPE_TEXT, text="Meals", clear_first=True,
                                                 binding=Binding(element_id="elem_22")))
        self.assertTrue(ok)
        self.assertIn(("select", "E", "Meals"), calls)

    def test_a_look_that_misses_the_popup_keeps_its_choices(self):
        """The accessibility read came back without the pop-up (10-02: the expense category, five looks with its
        choices and then none, and an oracle drive gave up): the choices it had stay on it."""
        from unittest import mock
        from deskmind_hands.drivers import axpopup
        d, origin = self._driver()
        calls = []
        with self._stub(calls):
            d._attach_popup_options([self._popup()], origin)
        with mock.patch.object(axpopup, "popups", lambda pid: []):
            e = self._popup()
            d._attach_popup_options([e], origin)
        self.assertEqual(e.options, ["Travel", "Meals"])
        self.assertIn("elem_22", d._popups)
        with mock.patch.object(axpopup, "popups", lambda pid: []):
            other = Element(id="elem_9", role="button", ax_role="AXPopUpButton", label="Currency", value="Choose…",
                            rect=self._popup().rect, window_id="w1")
            d._attach_popup_options([other], origin)
        self.assertEqual(other.options, [])   # never read: nothing to keep

    def test_not_opened_while_the_user_is_active(self):
        """A user who keeps working past the wait: nothing is opened, the choices are read at a later look."""
        import os
        from unittest import mock
        d, origin = self._driver()
        d._hid_idle = lambda: 0.2
        with self._stub([]), mock.patch.dict(os.environ, {"HANDS_FLASH_WAIT_S": "0"}), \
                mock.patch("time.sleep", lambda s: None):
            e = self._popup()
            d._attach_popup_options([e], origin)
            self.assertEqual(e.options, [])
            self.assertNotIn("elem_22", d._popups)

    def test_waits_for_the_user_then_reads(self):
        """Busy for a moment, then still: the choices are read in that look, not left out of it (09-30: the expense
        category was never offered while the user typed, and an oracle drive gave the task up)."""
        from unittest import mock
        d, origin = self._driver()
        idle = iter([0.2, 0.4, 1.0, 9.0])
        d._hid_idle = lambda: next(idle, 9.0)
        calls = []
        with self._stub(calls), mock.patch("time.sleep", lambda s: None):
            e = self._popup()
            d._attach_popup_options([e], origin)
        self.assertEqual(e.options, ["Travel", "Meals"])
        self.assertEqual([c[0] for c in calls], ["options"])

    def test_off_in_vision_mode_and_by_switch(self):
        d, origin = self._driver()
        calls = []
        with self._stub(calls):
            d._vision_app = lambda app=None: True
            e = self._popup()
            d._attach_popup_options([e], origin)
            self.assertEqual(e.options, [])
        self.assertEqual(calls, [])

    def test_match_by_position(self):
        from deskmind_hands.drivers.axpopup import match
        found = [{"frame": (100, 200, 50, 20)}, {"frame": (100, 400, 50, 20)}]
        self.assertIs(match([(0, (101, 401, 50, 20))], found)[0], found[1])
        self.assertEqual(match([(0, (300, 300, 50, 20))], found), {})
        # Off by more than the tolerance: the one pop-up with the element's title.
        named = [{"frame": (100, 200, 50, 20), "title": "Date"}, {"frame": (100, 400, 50, 20), "title": "Category"}]
        self.assertIs(match([(0, (100, 430, 50, 20))], named, titles={0: "Category"})[0], named[1])
        twins = named + [{"frame": (100, 600, 50, 20), "title": "Category"}]
        self.assertEqual(match([(0, (100, 430, 50, 20))], twins, titles={0: "Category"}), {})   # ambiguous: none


class GymExpense(unittest.TestCase):
    """The expense family: a receipt image in Preview, a web expense form in the host. Oracle on synthetic states."""

    def task(self, seed=30003):
        from tools.gym import expense
        t = expense.make_task(seed)
        t["receipt_file"] = f"receipt-s{seed}.png"
        return t

    def request(self, t, fields=None, here=None, values=(), options=True, errors=None):
        """A request as hands' SystemOne adapter builds it for the form (or the receipt when here is the viewer)."""
        tx = t["page"]["texts"]
        on_form = (here or t["app"]) == t["app"]
        els, idx = [], 1
        if on_form:
            for f in ("merchant", "date", "amount", "notes"):
                els.append({"index": str(idx), "label": tx[f], "role": "textField", "operations": ["CLICK", "TYPE_TEXT"]})
                idx += 1
            cat = {"index": str(idx), "label": tx["category"], "role": "button", "operations": ["CLICK", "SELECT"]}
            if options:
                cat["options"] = [{"index": f"{idx}:{k + 1}", "label": f"{tx['category']} → {o}", "value": o}
                                  for k, o in enumerate(t["page"]["options"])]
            els.append(cat)
            idx += 1
            els.append({"index": str(idx), "label": tx["submit"], "role": "button", "operations": ["CLICK"]})
        ops = ["CLICK", "TYPE_TEXT", "SELECT", "FOCUS_APP", "DONE"] if on_form else ["CLICK", "FOCUS_APP", "DONE"]
        q = {"operation": {"criteria": {o: o for o in ops}},
             "click_target": {"criteria": {e["index"]: e["label"] for e in els}},
             "type_text_target": {"criteria": {e["index"]: e["label"] for e in els if "TYPE_TEXT" in e["operations"]}},
             "type_text_value": {"criteria": {str(i + 1): {"value": v} for i, v in enumerate(values)}},
             "select_target": {"criteria": {o["index"]: o for e in els for o in e.get("options", [])}},
             "focus_app_target": {"criteria": {"com.apple.Preview": "x", "ai.deskmind.gymhost": "y"}}}
        state = {"page": {"title": here or t["app"]}, "elements": els}
        app_state = {"fields": {"merchant": "", "date": "", "amount": "", "category": "", "notes": ""},
                     "errors": errors or {}, "submitted": None}
        app_state["fields"].update(fields or {})
        return {"state": state, "questions": q}, app_state

    @staticmethod
    def chosen(label, head):
        return label[head]["choice"]

    def all_values(self, t):
        r = t["receipt"]
        from tools.gym import expense
        import datetime as dt
        printed = dt.date.fromisoformat(r["printed"].split()[0])
        return [r["merchant"], r["operator"], t["want"]["date"], expense.fmt_date(printed, t["page"]["date_format"]),
                f"{r['subtotal']:.2f}", t["want"]["amount"]]

    def test_read_the_receipt_first(self):
        from tools.gym import expense
        t = self.task()
        req, st = self.request(t, values=[])      # nothing read yet: the right merchant is not a choice
        label, why = expense.oracle(t, st, req)
        self.assertEqual(self.chosen(label, "operation"), "FOCUS_APP")
        self.assertEqual(self.chosen(label, "focus_app_target"), "com.apple.Preview")
        req, st = self.request(t, here=t["receipt_file"])
        label, _ = expense.oracle(t, st, req)
        self.assertEqual(self.chosen(label, "focus_app_target"), "ai.deskmind.gymhost")   # read: back to the form

    def test_date_total_and_merchant_not_the_distractors(self):
        from tools.gym import expense
        t = self.task()
        vals = self.all_values(t)
        req, st = self.request(t, values=vals)
        label, _ = expense.oracle(t, st, req)
        self.assertEqual(vals[int(self.chosen(label, "type_text_value")) - 1], t["receipt"]["merchant"])
        req, st = self.request(t, values=vals, fields={"merchant": t["want"]["merchant"]})
        label, why = expense.oracle(t, st, req)
        self.assertEqual(vals[int(self.chosen(label, "type_text_value")) - 1], t["want"]["date"])      # (a) visit date
        req, st = self.request(t, values=vals, fields={"merchant": t["want"]["merchant"], "date": t["want"]["date"]})
        label, why = expense.oracle(t, st, req)
        self.assertEqual(vals[int(self.chosen(label, "type_text_value")) - 1], t["want"]["amount"])    # (b) TOTAL

    def test_select_the_category(self):
        from tools.gym import expense
        t = self.task()
        req, st = self.request(t, values=self.all_values(t),
                               fields={f: t["want"][f] for f in ("merchant", "date", "amount")})
        label, _ = expense.oracle(t, st, req)                                                        # (c)
        self.assertEqual(self.chosen(label, "operation"), "SELECT")
        opt = req["questions"]["select_target"]["criteria"][self.chosen(label, "select_target")]
        self.assertEqual(opt["value"], t["want"]["category"])

    def test_no_submit_until_right_and_fix_before_submit(self):
        from tools.gym import expense
        t = self.task()
        wrong = {f: t["want"][f] for f in ("merchant", "date", "category")}
        wrong["amount"] = f"{t['receipt']['subtotal']:.2f}"
        req, st = self.request(t, values=self.all_values(t), fields=wrong)
        label, why = expense.oracle(t, st, req)                                                      # fix, not submit
        self.assertEqual(self.chosen(label, "operation"), "TYPE_TEXT")
        self.assertIn("fix", why)
        req, st = self.request(t, values=self.all_values(t), fields=dict(t["want"]))
        label, _ = expense.oracle(t, st, req)
        self.assertEqual(self.chosen(label, "operation"), "CLICK")                                   # (d) all right
        submit = next(e for e in req["state"]["elements"] if e["label"] == t["page"]["texts"]["submit"])
        self.assertEqual(self.chosen(label, "click_target"), submit["index"])

    def test_correct_fields_not_retyped_and_done_after_submit(self):
        from tools.gym import expense
        t = self.task()
        req, st = self.request(t, values=self.all_values(t), fields={"merchant": t["want"]["merchant"]})
        label, _ = expense.oracle(t, st, req)
        date_box = next(e["index"] for e in req["state"]["elements"] if e["label"] == t["page"]["texts"]["date"])
        self.assertEqual(self.chosen(label, "type_text_target"), date_box)                            # not the merchant
        st["submitted"] = dict(t["want"])
        label, _ = expense.oracle(t, st, req)
        self.assertEqual(self.chosen(label, "operation"), "DONE")
        self.assertTrue(expense.passed(t, st))
        st["submitted"] = dict(t["want"], amount=f"{t['receipt']['subtotal']:.2f}")
        self.assertEqual(expense.oracle(t, st, req)[0], None)
        self.assertFalse(expense.passed(t, st))

    def test_traps(self):
        from tools.gym import expense
        kinds = {}
        for seed in range(30000, 30004):
            t = self.task(seed)
            fields = {f: t["want"][f] for f in ("merchant", "date", "amount")} if seed % 4 == 3 else {}
            req, st = self.request(t, values=self.all_values(t), fields=fields)
            got = expense.trap(t, st, req)
            self.assertIsNotNone(got, f"seed {seed}: no trap")
            kinds[seed % 4] = got[1]
        self.assertIn("subtotal", kinds[0]); self.assertIn("printed", kinds[1])
        self.assertIn("operator", kinds[2]); self.assertIn("category empty", kinds[3])

    def test_receipt_values_are_value_choices(self):
        from tools.gym import expense
        from deskmind_hands.adapters.systemone import value_candidates
        for seed in range(30000, 30020):
            t = expense.make_task(seed)
            v = value_candidates(t["goal"], "\n".join(l for l, _ in expense.receipt_lines(t)), limit=20)
            for f in ("merchant", "date", "amount"):
                self.assertTrue(any(expense._same(f, x, t["want"][f]) for x in v), f"seed {seed} {f}: {v}")

    def test_receipt_renders_readable(self):
        import shutil, subprocess, tempfile, json, os
        from pathlib import Path
        from tools.gym import expense
        ocr = Path(__file__).resolve().parents[1] / "tools" / "native" / "ocr"
        if not ocr.exists():
            self.skipTest("tools/native/ocr not built")
        t = expense.make_task(30003)
        from deskmind_hands.vision import ocr as read
        with tempfile.TemporaryDirectory() as d:
            p = expense.render_receipt(t, Path(d) / "r.png")
            text = " ".join(r.get("text", "") for r in read(p))   # the harness's own reader, retry included
        self.assertIn(t["want"]["amount"], text)


class RareHan(unittest.TestCase):
    def test_a_name_with_rare_characters_is_a_candidate(self):
        from deskmind_hands.adapters.systemone import value_candidates
        c = value_candidates("用预览里那张收据报销：填商户", "绿叶䬸厅\n𠮷野家\n消费日期：2026年8月5日")
        self.assertIn("绿叶䬸厅", c)       # extension A
        self.assertIn("𠮷野家", c)         # extension B

    def test_chinese_receipts_read_right_at_the_sizes_they_are_shown(self):
        """The receipt font is one the OCR reads: in Hiragino Sans GB it read 餐 as 䬸 at most of these sizes (20 of
        48 renderings of 8 receipts x 6 sizes, 09-30); in STHeiti Light, 1 of 48 -- this receipt's heading at 0.7x,
        which is why 0.7 is not among the sizes asserted here."""
        import tempfile
        from pathlib import Path
        from PIL import Image
        from tools.gym import expense
        from deskmind_hands.vision import OCR_BIN, ocr
        if not OCR_BIN.exists():
            self.skipTest("tools/native/ocr not built")
        t = expense.make_task(30001, 2)
        self.assertEqual(t["want"]["merchant"], "绿叶餐厅")
        with tempfile.TemporaryDirectory() as d:
            im = Image.open(expense.render_receipt(t, Path(d) / "r.png"))
            for s in (0.9, 0.8, 0.6):
                p = Path(d) / f"r{s}.png"
                im.resize((int(im.width * s), int(im.height * s)), Image.LANCZOS).save(p)
                text = "".join(r.get("text", "") for r in ocr(p)).replace(" ", "")
                self.assertIn("绿叶餐厅", text, s)
                self.assertIn("餐饮", text, s)


class FocusCourtesy(unittest.TestCase):
    """Background input borrows the keyboard focus from the user's app: never while they are typing or moving the
    mouse, and always given back -- kept, it left the user typing into nothing while a gym run went on."""

    def _driver(self, idle):
        d = PeekabooDriver.__new__(PeekabooDriver)
        d.app = "com.apple.Safari"
        d._hid_idle = lambda: idle
        d._takeover = lambda: False
        d._background_input = lambda bundle: True
        return d

    def test_waits_while_the_user_is_active_and_gives_up(self):
        d = self._driver(idle=0.1)
        os.environ["HANDS_FLASH_WAIT_S"] = "0.3"
        try:
            ok, detail = d._flash("com.apple.Safari", lambda q: "clicked", what="click")
        finally:
            del os.environ["HANDS_FLASH_WAIT_S"]
        self.assertFalse(ok)
        self.assertIn("user is active", detail)

    def test_focus_given_back_even_when_the_input_fails(self):
        from deskmind_hands.drivers import skylight
        d = self._driver(idle=10)
        given = []
        d._background_quartz = lambda bundle: (object(), "token")
        real = skylight.give_focus_back
        skylight.give_focus_back = lambda token: given.append(token) or True
        try:
            def boom(q):
                raise RuntimeError("input failed")
            with self.assertRaises(RuntimeError):
                d._flash("com.apple.Safari", boom)
            ok, detail = d._flash("com.apple.Safari", lambda q: "clicked")
        finally:
            skylight.give_focus_back = real
        self.assertEqual(given, ["token", "token"])
        self.assertTrue(ok and "in the background" in detail)

    def test_takeover_skips_the_wait(self):
        d = self._driver(idle=0.0)
        d._takeover = lambda: True
        self.assertIsNone(d._wait_until_still("com.apple.Safari"))


class WindowPingPong(unittest.TestCase):
    """A-B-A-B window switches are a loop only when nothing was done between them: reading in one window and acting
    in another goes back and forth too, and the guard took away the switch the two-app gym task still needed."""

    def _h(self, *steps):
        from deskmind_hands.adapters.base import Turn
        out = []
        for s in steps:
            kind, ok, changed = (s + (True, None))[:3] if isinstance(s, tuple) else (s, True, None)
            out.append(Turn({"kind": "focus_window", "text": kind} if kind in "AB" else {"kind": kind}, ok, "",
                            changed))
        return out

    def test_bare_back_and_forth_is_a_loop(self):
        from deskmind_hands.adapters.systemone import window_ping_pong
        self.assertTrue(window_ping_pong(self._h("A", "B", "A", "B")))
        self.assertTrue(window_ping_pong(self._h("A", "B", ("key", False), "A", "B")))       # refused: not work
        self.assertTrue(window_ping_pong(self._h("A", ("click", True, False), "B", "A", "B")))  # nothing changed

    def test_work_between_the_switches_is_not(self):
        from deskmind_hands.adapters.systemone import window_ping_pong
        self.assertFalse(window_ping_pong(self._h("A", "click", "B", "A", "B")))
        self.assertFalse(window_ping_pong(self._h("A", "B", "type_text", "A", "B")))

    def test_too_few_switches(self):
        from deskmind_hands.adapters.systemone import window_ping_pong
        self.assertFalse(window_ping_pong(self._h("A", "B", "A")))
        self.assertFalse(window_ping_pong(self._h("A", "A", "B")))


class ClickedAgain(unittest.TestCase):
    def test_a_second_click_on_what_already_worked_is_not_text(self):
        # A song double-clicked played; the planner double-clicked it again, nothing changed, and it was told "it is
        # text, not a control" -- so it went to the studio cut next to it, and back, and said DONE on the wrong one.
        d = PeekabooDriver.__new__(PeekabooDriver)
        d._ocr_worked = [("纸船 （Live） · VIP原唱 林夏 · 最近听过 万人收麗》", Rect(1025, 368, 117, 18))]
        # Read again a moment later: another id, a character or two different, a point lower.
        self.assertTrue(d._took_effect_before("纸船 （Live） · VI 原唱林夏 · 最近听过 万人收藏》", Rect(1025, 369, 117, 17)))
        # The row next to it, and the same words somewhere else, are other things.
        self.assertFalse(d._took_effect_before("纸船", Rect(318, 371, 70, 15)))
        self.assertFalse(d._took_effect_before("纸船 （Live） · VIP原唱 林夏 · 最近听过 万人收藏》", Rect(1025, 600, 117, 18)))
        self.assertFalse(PeekabooDriver.__new__(PeekabooDriver)._took_effect_before("纸船", Rect(318, 371, 70, 15)))

    def test_the_item_just_opened_is_the_same_spot(self):
        # The previous step opened the live cut and it started playing; a second double-click on it paused it, and
        # the run said DONE over a paused song. The same row read again is refused; the studio cut beside it is not.
        d = PeekabooDriver.__new__(PeekabooDriver)
        opened = ("纸船 （Live） · 林夏 · 最近听过", Rect(1025, 368, 117, 18))
        self.assertTrue(d._same_spot(opened, "纸船 （Live） · 林夏 · 最近听迂", Rect(1025, 369, 117, 17)))
        self.assertFalse(d._same_spot(opened, "纸船", Rect(318, 371, 70, 15)))


class Decision(unittest.TestCase):
    def test_top_three(self):
        a = {"operation": {"probabilities": {"CLICK": 0.1, "OPEN": 0.7, "DONE": 0.15, "SCROLL": 0.05}}}
        self.assertEqual(top_operations(a), [["OPEN", 0.7], ["DONE", 0.15], ["CLICK", 0.1]])
        self.assertEqual(top_operations({}), [])


def _req(els, ops=("CLICK", "OPEN", "TYPE_TEXT", "DONE", "FOCUS_WINDOW"), values=(), windows=None):
    for i, e in enumerate(els):
        e["index"] = str(i + 1)
        e.setdefault("operations", ["CLICK", "OPEN"])
    keys = {e["index"]: {} for e in els}
    q = {"operation": {"criteria": {o: "" for o in ops}}, "click_target": {"criteria": keys},
         "open_target": {"criteria": keys}, "type_text_target": {"criteria": keys},
         "type_text_value": {"criteria": {str(i + 1): {"value": v} for i, v in enumerate(values)}}}
    if windows:
        q["focus_window_target"] = {"criteria": windows}
    return {"state": {"elements": els, "page": {"title": ""}}, "questions": q}


def _top(label):
    return {h: max(v["probabilities"], key=v["probabilities"].get) for h, v in label.items()}


class GymMusic(unittest.TestCase):
    def setUp(self):
        from tools.gym import music
        self.m = music

    def _rows(self, t):
        return [{"role": "row", "label": f"{x['title']} · {x['artist']} · {x['album']}"} for x in t["page"]["catalog"]]

    def test_exact_row_not_the_live_cut(self):
        t = next(self.m.make_task(s) for s in range(40) if "(Live)" not in self.m.make_task(s)["target"]["title"])
        lab, why = self.m.oracle(t, {"results": [t["target"]], "playing": {}}, _req(self._rows(t)))
        rows = self._rows(t)
        chosen = rows[int(_top(lab)["open_target"]) - 1]["label"]
        self.assertTrue(chosen.startswith(f"{t['target']['title']} · {t['target']['artist']}"), chosen)

    def test_live_version_at_the_end_of_the_goal(self):
        t = next(self.m.make_task(s) for s in range(30000, 30100)
                 if "(Live)" in self.m.make_task(s)["target"]["title"])
        self.assertIn("现场版" if t["lang"] == "zh" else "live version", t["goal"])
        rows = self._rows(t)
        lab, _ = self.m.oracle(t, {"results": [t["target"]], "playing": {}}, _req(rows))
        self.assertIn("(Live)", rows[int(_top(lab)["open_target"]) - 1]["label"])
        trap, why = self.m.trap(t, {"results": [t["target"]], "playing": {}}, _req(self._rows(t)))[:2]
        self.assertNotIn("(Live)", why)          # the trap opens the studio cut

    def test_vision_item_through_ocr_misreads(self):
        t = {"title": "海风", "artist": "温晚"}
        els = [{"id": "ocr:1", "index": "1", "label": "海风 · 叶屿", "operations": ["OPEN"]},
               {"id": "ocr:2", "index": "2", "label": "海风（Live） · 溫晚", "operations": ["OPEN"]},
               {"id": "ocr:3", "index": "3", "label": "海风 · 溫晚・海风", "operations": ["OPEN"]}]
        self.assertEqual(self.m._vision_item(els, t)["index"], "3")
        self.assertIsNone(self.m._vision_item(els[:1], t))

    def test_v1_seeds_unchanged(self):
        self.assertEqual(self.m.make_task(2)["goal"], "打开Tunebox，播放沈岚舟的《归途》")


class GymSettings(unittest.TestCase):
    def setUp(self):
        from tools.gym import settings
        self.s = settings

    def test_done_only_with_the_setting_on_screen(self):
        t = next(self.s.make_task(x) for x in range(30000, 30200)
                 if self.s.make_task(x)["already"] and self.s.make_task(x)["kind"] != "appearance"
                 and self.s.make_task(x)["page"]["start"] != self.s.make_task(x)["target"]["section"])
        secs = list(t["page"]["sections"])
        els = [{"role": "button", "label": x} for x in secs]
        lab, why = self.s.oracle(t, {"section": t["page"]["start"], "values": dict(t["page"]["values"])}, _req(els))
        self.assertEqual(_top(lab)["operation"], "CLICK")        # open its section first, not DONE
        sw = [{"role": "checkbox", "label": n} for n in t["page"]["sections"][t["target"]["section"]]]
        lab, why = self.s.oracle(t, {"section": t["target"]["section"], "values": dict(t["page"]["values"])},
                                 _req(els + sw))
        self.assertEqual(_top(lab)["operation"], "DONE")

    def test_trap_flips_the_neighbour_and_the_oracle_flips_it_back(self):
        t = next(self.s.make_task(x) for x in range(30000, 30200) if self.s.make_task(x)["kind"] != "appearance")
        sec = t["target"]["section"]
        sw = [{"role": "checkbox", "label": n} for n in t["page"]["sections"][sec]]
        trap = self.s.trap(t, {"section": sec}, _req([dict(e) for e in sw]))
        flipped = trap[1].split("'")[1]
        self.assertNotEqual(flipped, t["target"]["setting"])
        values = dict(t["page"]["values"]); values[flipped] = not values[flipped]
        lab, why = self.s.oracle(t, {"section": sec, "values": values}, _req([dict(e) for e in sw]))
        self.assertIn(flipped, why)


class GymMail(unittest.TestCase):
    def setUp(self):
        from tools.gym import mail
        self.m = mail

    def test_flag_on_the_wrong_message_is_put_back(self):
        for s in range(1, 300):
            t = self.m.make_task(s)
            if t["kind"] != "flag" or t["already"]:
                continue
            decoys = [m for m in t["page"]["inbox"] if m["id"] != t["target"]["id"]
                      and m["id"] not in t["page"]["flagged"]
                      and (m["from"] == t["target"]["from"] or m["subject"] == t["target"]["subject"])]
            if decoys:
                break
        tx, dec = t["page"]["texts"], decoys[0]
        rows = [{"role": "row", "label": f"{m['from']} · {m['subject']} · {m['date']}"} for m in t["page"]["inbox"]]
        st = {"selected": dec["id"], "flagged": list(t["page"]["flagged"]), "deleted": [], "replies": []}
        trap = self.m.trap(t, st, _req(rows + [{"role": "button", "label": tx["flag"]}]))
        self.assertIn("flagged the wrong message", trap[1])
        st["flagged"] = st["flagged"] + [dec["id"]]
        lab, why = self.m.oracle(t, st, _req(rows + [{"role": "button", "label": tx["unflag"]}]))
        self.assertIn("put back the flag", why)


class GymMailMusic(unittest.TestCase):
    def test_read_then_switch_then_play(self):
        from tools.gym import mailmusic
        t = mailmusic.make_task(5)
        mail_app, music_app = t["apps"]
        msg = next(x for x in t["pages"]["mail"]["inbox"] if x["id"] == t["message"])
        rows = [{"role": "row", "label": f"{m['from']} · {m['subject']} · {m['date']}"}
                for m in t["pages"]["mail"]["inbox"]]
        req = _req(rows, windows={"w2": music_app})
        req["state"]["page"]["title"] = mail_app
        lab, why = mailmusic.oracle(t, {"mail": {"selected": None}, "music": {}}, req)
        self.assertEqual(rows[int(_top(lab)["click_target"]) - 1]["label"].split(" · ")[:2], [msg["from"], msg["subject"]])
        lab, why = mailmusic.oracle(t, {"mail": {"selected": t["message"]}, "music": {}}, req)
        self.assertEqual(_top(lab), {"operation": "FOCUS_WINDOW", "focus_window_target": "w2"})
        self.assertIn(t["target"]["title"], [x for x in msg["body"] if t["target"]["title"] in x][0])


class KeyEventsFromAChild(unittest.TestCase):
    """Key events come from a child process as data, so this process never asks for the keyboard layout."""

    class _Q:
        def __init__(self): self.made = []
        def CGEventCreateKeyboardEvent(self, _src, keycode, down):   # noqa: N802 -- Quartz's name
            self.made.append((keycode, down)); return ("here", keycode, down)
        def CGEventCreateFromData(self, _alloc, data):   # noqa: N802
            return ("rebuilt", bytes(data))

    def setUp(self):
        from deskmind_hands.drivers.peekaboo import PeekabooDriver
        self.D, self._saved = PeekabooDriver, PeekabooDriver._key_blobs
        PeekabooDriver._key_blobs = {}

    def tearDown(self):
        self.D._key_blobs = self._saved

    def test_the_child_is_asked_once_for_every_key(self):
        import base64, json
        from unittest import mock
        try:
            import Foundation  # noqa: F401
        except ImportError:
            self.skipTest("pyobjc not installed")
        out = json.dumps({f"{k}:{d}": base64.b64encode(f"ev{k}{d}".encode()).decode() for k in (0, 9, 36) for d in (0, 1)})
        q = self._Q()
        with mock.patch("deskmind_hands.drivers.peekaboo.subprocess.run",
                        return_value=mock.Mock(stdout=out)) as run:
            self.assertEqual(self.D._key_event(q, 9, True), ("rebuilt", b"ev91"))
            self.assertEqual(self.D._key_event(q, 36, False), ("rebuilt", b"ev360"))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(q.made, [])   # nothing asked of the layout in this process

    def test_without_the_child_events_are_made_here(self):
        from unittest import mock
        q = self._Q()
        import contextlib, io
        err = io.StringIO()
        with mock.patch("deskmind_hands.drivers.peekaboo.subprocess.run", side_effect=OSError("no python")) as run, \
                contextlib.redirect_stderr(err):
            self.assertEqual(self.D._key_event(q, 9, True), ("here", 9, True))
            self.assertEqual(self.D._key_event(q, 0, False), ("here", 0, False))
        self.assertEqual(run.call_count, 1)   # not retried for every key
        self.assertEqual(err.getvalue().count("Dock tiles"), 1)   # and said once, not per key

    def test_output_that_is_not_the_events_counts_as_failure(self):
        from unittest import mock
        import contextlib, io
        q, err = self._Q(), io.StringIO()
        with mock.patch("deskmind_hands.drivers.peekaboo.subprocess.run",
                        return_value=mock.Mock(stdout="a warning, not JSON")), contextlib.redirect_stderr(err):
            self.assertEqual(self.D._key_event(q, 36, True), ("here", 36, True))
        self.assertIn("Dock tiles", err.getvalue())


class GrounderToken(unittest.TestCase):
    """The app's grounding server needs its token: every grounder request carries HANDS_GROUNDER_TOKEN when set."""

    def test_header(self):
        from unittest import mock
        from deskmind_hands.grounding import grounder_headers
        with mock.patch.dict(os.environ, {"HANDS_GROUNDER_TOKEN": "abc"}):
            self.assertEqual(grounder_headers()["Authorization"], "Bearer abc")
        with mock.patch.dict(os.environ, {"HANDS_GROUNDER_TOKEN": ""}):
            self.assertNotIn("Authorization", grounder_headers())


if __name__ == "__main__":
    unittest.main()


class GymSplit(unittest.TestCase):
    """Split 2 (tools/gym/split.py): concept-disjoint held-out; split 1 seeds exactly as they were."""
    NEW = ("split_version", "template_id", "template_heldout", "concepts")
    #: sha256[:16] of make_task(seed) before split 2 existed (the new fields left out).
    OLD = {
        "music": {1: "fb8f5b8247132684", 123: "2e566a20b359781c", 30000: "025ccd4cc20f0a0b", 30091: "d8977ac87a5ebc1c",
                  30199: "0b696dbaf024bd4e"},
        "mail": {1: "e98c38587df6eb54", 123: "f3327a3a04aebb96", 30000: "1e6336def85deddc", 30091: "ed5568586e4e258d",
                 30199: "eb902dab355035a2"},
        "settings": {1: "9dc50546cde30504", 123: "8b3fe443aecb7b44", 30000: "3e51b699f5765dee",
                     30091: "128dd36488b66e1d", 30199: "99c0dae8bf57b992"},
        "mailmusic": {1: "3a964dced7fea781", 123: "1d95ac8a172b3862", 30000: "c4e89e6a55171edb",
                      30091: "1eec0b3cfecda500", 30199: "758ce21a79994e0e"},
    }

    @staticmethod
    def family(name):
        import importlib
        return importlib.import_module(f"tools.gym.{name}")

    def strip(self, t):
        t = {k: v for k, v in t.items() if k not in self.NEW}
        if "music_task" in t:
            t["music_task"] = {k: v for k, v in t["music_task"].items() if k not in self.NEW}
        return t

    def test_split_1_seeds_unchanged(self):
        import hashlib, json
        for name, seeds in self.OLD.items():
            fam = self.family(name)
            for seed, want in seeds.items():
                got = hashlib.sha256(json.dumps(self.strip(fam.make_task(seed)), ensure_ascii=False,
                                                sort_keys=True).encode()).hexdigest()[:16]
                self.assertEqual(got, want, f"{name} seed {seed} changed")
                self.assertEqual(fam.make_task(seed)["split_version"], 1)

    def test_split_2_train_never_held_out_and_held_out_always(self):
        from tools.gym import split
        for name in ("music", "mail", "settings", "mailmusic", "expense"):
            fam = self.family(name)
            counts = {"train": 0, "heldout": 0}
            for seed in range(30000, 30160):
                t = fam.make_task(seed, 2)
                self.assertEqual(t["split_version"], 2)
                self.assertEqual(t["split"], fam.make_task(seed)["split"], f"{name} {seed}: the skin decides the split")
                counts[t["split"]] += 1
                if t["split"] == "train":
                    self.assertFalse(split.uses_heldout(t), f"{name} {seed}: a held-out concept in training: {t}")
                else:
                    self.assertTrue(split.uses_heldout(t), f"{name} {seed}: held-out task with nothing held out")
            self.assertTrue(counts["train"] and counts["heldout"], f"{name}: {counts}")

    def test_split_2_train_pages_avoid_held_out_music_values(self):
        # Not only the goal: decoys and fillers in a training task never show a held-out title.
        from tools.gym import music, split
        held = split.heldout_values("music", "zh_words", music.ZH_WORDS) | \
            split.heldout_values("music", "en_words", music.EN_WORDS)
        for seed in range(30000, 30100):
            t = music.make_task(seed, 2)
            if t["split"] != "train":
                continue
            titles = {x["title"].replace(" (Live)", "").replace("海边的", "").replace("Beyond the ", "")
                      for x in t["page"]["catalog"] + t["page"]["home"]}
            self.assertFalse(titles & held, f"seed {seed}: {titles & held}")

    def test_held_out_sets(self):
        from tools.gym import music, split
        held = split.heldout_values("music", "zh_words", music.ZH_WORDS)
        self.assertEqual(len(held), 4)                  # a fifth of 20
        self.assertEqual(held, split.heldout_values("music", "zh_words", music.ZH_WORDS))   # the same every time
        self.assertEqual(split.heldout_template("music", "zh", ["music/zh/0", "music/zh/1"]),
                         split.heldout_template("music", "zh", ["music/zh/1", "music/zh/0"]))

    def test_split_2_reproducible(self):
        for name in ("music", "mail", "settings", "mailmusic", "expense"):
            fam = self.family(name)
            self.assertEqual(fam.make_task(30123, 2), fam.make_task(30123, 2))


def held_window_key(case, lost=None):
    """A WindowKey (the type the background Gate insists on) whose lost() is the case's `window_lost`."""
    from deskmind_hands.drivers import skylight
    key = skylight.WindowKey.__new__(skylight.WindowKey)
    key.lost = lost or (lambda: case.window_lost)
    key.note = lambda: ""
    return key


class _KeysFixture(unittest.TestCase):
    """A text element whose value is not settable, its focus, a clipboard and the key posts, all faked."""

    def setUp(self):
        from deskmind_hands.drivers import axfield, base
        self.shown = {"value": "~ % "}
        self.el = {"role": "AXTextArea", "focused": False, "can_focus": True, "focus_takes": True,
                   "lose_during_wait": False}
        self.log = []
        el = self.el

        from deskmind_hands.drivers import skylight
        from deskmind_hands.grounding import STOP
        self.addCleanup(STOP.clear)
        self.front = 4242                               # the app in front (the foreground gate asks)
        self.window_lost = None                         # why the held window stopped being key (background gate)
        self.hooks = {}                                 # after a send site: what happens next (a condition flips)
        self.select_takes = True

        def after(site):
            hook = self.hooks.get(site)
            if hook:
                hook()

        def focus(f, wait=0.5):
            self.log.append("focus")
            base.ax_counted(lambda: 0)                  # an accessibility write: counted as sent
            if el["focus_takes"]:
                el["focused"] = True
            after("focus")
            return el["focus_takes"]

        def select(f, all_, wait=0.3):
            self.log.append(("select", all_))
            base.ax_counted(lambda: 0)                  # an accessibility write: counted as sent
            after("select")
            return self.select_takes
        self.after = after
        for name, fn in {"value": lambda f: self.shown["value"],
                         "role": lambda f: el["role"],
                         "is_focused": lambda f: after("focus read") or el["focused"],     # a read that can wait
                         "can_set": lambda f, attr: el["can_focus"],
                         "focus": focus,
                         "select": select,
                         "wanted": lambda f, all_: after("length read") or (0, 4),     # the length: a read
                         "select_range": lambda f, want, wait=0.3: select(f, True)}.items():
            p = mock.patch.object(axfield, name, fn, create=True)     # can_set: new here
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(skylight, "front_pid", lambda: self.front)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(PeekabooDriver, "AX_INSERT_WAIT_S", 0.0)
        p.start()
        self.addCleanup(p.stop)

    def _driver(self, settable, reflects=False, keys_land=True, field=True, late_write=False, foreground=True,
                fail_paste=False):
        from types import SimpleNamespace
        from deskmind_hands.geometry import Point, Rect
        from deskmind_hands.drivers import base, peekaboo as P
        d = PeekabooDriver.__new__(PeekabooDriver)
        d.app = "com.example.term"
        d._rects, d._element_role = {"elem_3": Rect(0, 30, 580, 350)}, {"elem_3": "textArea"}
        d._element_value, d._labels_by_id = {"elem_3": "~ % "}, {"elem_3": "Shell"}
        d._element_settable = {"elem_3": settable}
        d._origin, d._snapshot = Point(100, 100), "s1"
        d._background_input = lambda bundle: False
        d._type_in_background = lambda eid, text, clear: self.log.append("ax insertion route") and None
        d._keys_in_background = lambda *a: None                                  # this Mac: foreground keys
        d._moved_since_seen = lambda: None
        d._field_now = lambda eid: "FIELD" if field else None
        d._pid_of = lambda bundle, all_wins=None: 4242
        pending = {}

        def tool(name, args):
            self.log.append((name, args.get("value")))
            if reflects:
                self.shown["value"] = args.get("value")
            if late_write:
                pending["value"] = args.get("value")            # lands later, after the read-back
            return True, {"effect": "accepted"}, "", ""
        d._mcp = SimpleNamespace(tool=tool)
        q = SimpleNamespace(kCGEventFlagMaskCommand=1 << 20, kCGHIDEventTap=0)
        self.board = board = {"text": "", "items": [{"public.utf8-plain-text": "the user's own"}]}
        d._post_click = lambda Q, x, y, count=1: self.log.append("click")

        def post_key(Q, code, flags=0):
            self.log.append(("key", code, flags))
            if fail_paste and code == 9:
                raise RuntimeError("the paste could not be posted")
            if keys_land and code == 9 and flags:
                self.shown["value"] += board["text"]
            elif keys_land and code == P.keys.RETURN:
                self.shown["value"] += "\n"
        d._post_key = post_key
        d._keycode = lambda Q, ch: {"a": 0, "v": 9}[ch]
        # the press primitives: events built beforehand are ("down"|"up", code, flags); a down is the key, as typed
        d._key_pair = lambda Q, code, flags=0: (("down", code, flags), ("up", code, flags), ("key", code))

        def post(Q, tap, ev, press=None, release=None, **_how):
            if ev[0] == "down":
                base.counted(lambda: None)                      # a key went: counted as sent
                post_key(Q, ev[1], ev[2])
                self.after(("key", ev[1]))
        p = mock.patch.object(P, "_post", post)
        p.start()
        self.addCleanup(p.stop)

        class Item:
            def __init__(self, data=None):
                self.data = dict(data or {})

            @classmethod
            def alloc(cls):
                return cls()

            def init(self):
                return self

            def types(self):
                return list(self.data)

            def dataForType_(self, t):
                return self.data[t]

            def setData_forType_(self, data, t):
                self.data[t] = data

        class PB:
            def pasteboardItems(self):
                return [Item(i) for i in board["items"]]

            def clearContents(self):
                board["text"], board["items"] = "", []

            def setString_forType_(self, s, t):
                board["text"] = s
                board["items"] = [{t: s}]

            def writeObjects_(self, items):
                board["items"] = [dict(i.data) for i in items]
        appkit = types.ModuleType("AppKit")
        appkit.NSPasteboard = SimpleNamespace(generalPasteboard=lambda: PB())
        appkit.NSPasteboardItem = Item
        appkit.NSPasteboardTypeString = "public.utf8-plain-text"
        self.workspace_front = None                             # NSWorkspace's frontmost app: not told by default
        appkit.NSWorkspace = SimpleNamespace(sharedWorkspace=lambda: SimpleNamespace(
            frontmostApplication=lambda: None if self.workspace_front is None else SimpleNamespace(
                processIdentifier=lambda: self.workspace_front)))
        appkit.NSRunningApplication = SimpleNamespace(
            runningApplicationsWithBundleIdentifier_=lambda b: getattr(self, "bundle_apps", []))
        p = mock.patch.dict(sys.modules, {"AppKit": appkit})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(P.time, "sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

        def flash(app, perform, what=""):
            if not foreground:
                return False, NothingSent("com.example.term only responds in the foreground, which this run is not "
                                          "allowed to use")
            if "value" in pending:                              # the write lands while the app comes forward
                self.shown["value"] = pending.pop("value")
            if self.el["lose_during_wait"]:
                self.el["focused"] = False                       # the user clicked another field meanwhile
            return True, perform(q)
        d._flash = flash
        d._settle = lambda ok, detail: ExecResult(ok, detail)
        return d

    def _type(self, d, text="ls -la", clear=False):
        from deskmind_hands.actions import Action, ActionKind, Binding
        from deskmind_hands.drivers.base import input_scope
        with input_scope(None):                                   # one step, as the executor runs it
            return d._execute_inner(Action(kind=ActionKind.TYPE_TEXT, text=text, clear_first=clear,
                                           binding=Binding(element_id="elem_3")))

    def sent(self):
        return [x for x in self.log if x not in ("ax insertion route",)]

    def pastes(self):
        return [x for x in self.log if isinstance(x, tuple) and x[0] == "key"]


class TypingWhereNoValueIsTaken(_KeysFixture):
    """rc.15 (10-24): typing into a terminal's text view ended "unverified" every time -- the accessibility write
    ("set_value ... without valid canonical result semantics") was accepted and never showed, and each uncertain step
    put every later action to the user. The way in is chosen from the element, once, before anything is sent, never
    by app: a settable value is written (unconfirmed: unverified, no keys after); a text element that takes no value
    gets keys, only while it holds the keyboard focus at the moment they are sent; a newline is never a bare Return
    key -- pasted as text into a multi-line element, refused for a one-line one; anything else is refused with nothing
    sent. Read back once after. Two tables (focus; newline x field kind), then the rest."""

    # -- table 1: focus at the check -> during the wait -> right before the keys
    def test_focus_kept_throughout_is_left_alone_and_the_caret_kept(self):
        self.el["focused"] = True
        res = self._type(self._driver(settable=False))
        self.assertNotIn("click", self.log)
        self.assertNotIn("focus", self.log)
        self.assertFalse([x for x in self.log if isinstance(x, tuple) and x[0] == "select"])
        self.assertTrue(res.ok and not res.indeterminate)

    def test_focus_lost_during_the_wait_is_given_back_before_the_keys(self):
        """Codex P1 @5fb2651: the focus read before the wait was trusted at the send, and the paste went into the
        field the user had moved to."""
        self.el.update(focused=True, lose_during_wait=True)
        res = self._type(self._driver(settable=False))
        self.assertEqual(self.sent(), ["focus", ("key", 9, 1 << 20)])         # focused again, then pasted
        self.assertTrue(res.ok and not res.indeterminate)

    def test_focus_lost_during_the_wait_and_not_settable_is_refused_before_the_keys(self):
        self.el.update(focused=True, lose_during_wait=True, can_focus=False)
        res = self._type(self._driver(settable=False))
        self.assertIs(res.performed, False)
        self.assertEqual(self.pastes(), [])

    def test_not_focused_is_focused_by_accessibility_then_typed(self):
        res = self._type(self._driver(settable=False))
        self.assertEqual(self.sent(), ["focus", ("key", 9, 1 << 20)])
        self.assertTrue(res.ok and not res.indeterminate)

    def test_a_focus_write_that_does_not_read_back_is_unverified_not_nothing_sent(self):
        """The accessibility focus write is a send (base.ax_counted): refused after it, the step is unverified."""
        self.el["focus_takes"] = False
        res = self._type(self._driver(settable=False))
        self.assertEqual(self.pastes(), [])
        self.assertIsNone(res.performed)
        self.assertTrue(res.indeterminate)

    def test_not_focused_and_not_settable_is_refused_before_the_wait(self):
        self.el["can_focus"] = False
        res = self._type(self._driver(settable=False))
        self.assertIs(res.performed, False)
        self.assertEqual(self.sent(), [])

    # -- table 2: newline x field kind
    def test_no_newline_in_a_multi_line_element_is_one_paste(self):
        from deskmind_hands.drivers import peekaboo as P
        res = self._type(self._driver(settable=False))
        self.assertEqual(self.pastes(), [("key", 9, 1 << 20)])
        self.assertNotIn(("key", P.keys.RETURN, 0), self.log)
        self.assertTrue(res.ok and not res.indeterminate)

    def test_a_newline_in_a_multi_line_element_is_pasted_as_text_never_the_return_key(self):
        """Codex P1 @5fb2651: in a chat box Return sends -- "one\ntwo" sent "one". The newline goes in as text."""
        from deskmind_hands.drivers import peekaboo as P
        d = self._driver(settable=False)
        bg = []
        d._keys_in_background = lambda field, text, *a: bg.append(text)
        res = self._type(d, text="one\ntwo")
        self.assertEqual(bg, [])                                               # not the background keys
        self.assertEqual(self.pastes(), [("key", 9, 1 << 20)])                 # one paste, no Return key
        self.assertNotIn(("key", P.keys.RETURN, 0), self.log)
        self.assertTrue(self.shown["value"].endswith("one\ntwo"))
        self.assertTrue(res.ok and not res.indeterminate)

    def test_a_newline_where_the_foreground_cannot_be_used_is_refused(self):
        res = self._type(self._driver(settable=False, foreground=False), text="one\ntwo")
        self.assertIs(res.performed, False)
        self.assertIn("only pasted", res.detail)
        self.assertEqual(self.sent(), [])

    def test_a_newline_in_a_one_line_field_is_refused_not_submitted(self):
        self.el["role"] = "AXTextField"
        res = self._type(self._driver(settable=False), text="one\ntwo")
        self.assertIs(res.performed, False)
        self.assertIn("press Return as its own step", res.detail)
        self.assertEqual(self.sent(), [])

    def test_a_newline_paste_stopped_part_way_in_the_background_stays_unverified(self):
        """Review of #66 @e04edab: the WebKit background branch of _flash -- the focus written, the paste posted, then
        a key refused -- answers MaybeSent; the newline wrapper relabelled it "nothing sent", and outside a step
        (`hands do`) that invited a retry that pasted twice."""
        from deskmind_hands.drivers import base, skylight
        d = self._driver(settable=False)
        del d._flash                                                          # the real one, its background branch
        d._background_input = lambda bundle: True
        d._wait_until_still = lambda bundle, what: None
        d._too_late = lambda waited: None
        token = types.SimpleNamespace(note=lambda: "")
        d._background_quartz = lambda bundle: (types.SimpleNamespace(kCGEventFlagMaskCommand=1 << 20,
                                                                     kCGHIDEventTap=0), token)
        from deskmind_hands.drivers import peekaboo as P

        def post(Q, tap, ev, press=None, release=None, **_how):
            if ev[:2] == ("down", 9):
                base.counted(lambda: None)                                   # the paste went
                raise skylight.WindowKeyRefused("the user came back to the app")
        p = mock.patch.object(P, "_post", post)
        p.start()
        self.addCleanup(p.stop)
        from deskmind_hands.actions import Action, ActionKind, Binding
        d._require_session = lambda: None
        d.CONFIRM_BY_DIFF = False
        with mock.patch.object(skylight, "give_focus_back", lambda t: None):
            res = d.execute(Action(kind=ActionKind.TYPE_TEXT, text="one\ntwo",       # its own step, as `hands do`
                                   binding=Binding(element_id="elem_3")))
        self.assertIsInstance(res.detail, base.MaybeSent)                     # passed through, not relabelled
        self.assertIn("may have taken effect", res.detail)
        self.assertIsNot(res.performed, False)
        self.assertTrue(res.indeterminate, res.detail)

    def test_a_webkit_app_the_user_is_in_goes_on_to_the_foreground_route(self):
        """Review of #66 @24fbf21: in the WebKit app the user is in, _background_quartz holds no window; the paste
        refused with a type _flash does not know and the step ended there. The same refusal as before
        (WindowKeyRefused): _flash goes on to its foreground route -- here, its rule that the app is not allowed in
        front."""
        from deskmind_hands.drivers import skylight
        d = self._driver(settable=False)
        del d._flash                                                          # the real one
        d._background_input = lambda bundle: True
        d._wait_until_still = lambda bundle, what: None
        d._too_late = lambda waited: None
        bq = skylight.BackgroundQuartz.__new__(skylight.BackgroundQuartz)
        bq._q, bq.pid, bq.wid = types.SimpleNamespace(kCGEventFlagMaskCommand=1 << 20, kCGHIDEventTap=0), 4242, 200
        bq.key = skylight.NoWindowKey.__new__(skylight.NoWindowKey)          # the user's front app: no key held
        d._background_quartz = lambda bundle: (bq, bq.key)
        self.el["focused"] = True
        with mock.patch.object(skylight, "give_focus_back", lambda t: None), \
                mock.patch.dict(os.environ, {"HANDS_FLASH_APPS": ""}):
            res = self._type(d, text="one\ntwo")
        self.assertIn("only responds in the foreground", res.detail)
        self.assertIs(res.performed, False, res.detail)
        self.assertEqual(self.pastes(), [])

    # -- the rest
    def test_a_button_is_not_a_keyboard_target(self):
        self.el["role"] = "AXButton"
        res = self._type(self._driver(settable=False))
        self.assertIs(res.performed, False)
        self.assertIn("not a text target", res.detail)
        self.assertEqual(self.sent(), [])

    def test_no_text_element_found_is_refused(self):
        res = self._type(self._driver(settable=False, field=False))
        self.assertIs(res.performed, False)
        self.assertEqual(self.sent(), [])

    def test_a_late_write_is_unverified_and_typed_once(self):
        d = self._driver(settable=True, late_write=True)
        res = self._type(d)
        self.assertTrue(res.indeterminate)
        self.assertIn("unverified", res.detail)
        self.assertEqual([x for x in self.sent() if x[0] != "set_value"], [])     # no keys after the write
        d._flash("x", lambda q: "")                                               # the write lands later
        self.assertEqual(self.shown["value"].count("ls -la"), 1)

    def test_replacing_selects_all_then_types(self):
        self._type(self._driver(settable=False), clear=True)
        self.assertEqual(self.sent()[:2], ["focus", ("select", True)])

    def test_keys_that_do_not_show_are_unverified_and_nothing_more_is_sent(self):
        res = self._type(self._driver(settable=False, keys_land=False))
        self.assertTrue(res.indeterminate)
        self.assertEqual(self.sent(), ["focus", ("key", 9, 1 << 20)])

    def test_a_settable_value_that_shows_is_verified_with_no_key(self):
        res = self._type(self._driver(settable=True, reflects=True))
        self.assertEqual([x for x in self.sent() if isinstance(x, tuple)], [("set_value", "~ % ls -la")])
        self.assertTrue(res.ok and not res.indeterminate)

    def test_the_users_clipboard_is_put_back_after_the_paste_and_after_a_failure_in_it(self):
        self._type(self._driver(settable=False))
        self.assertEqual(self.board["items"], [{"public.utf8-plain-text": "the user's own"}])
        self.el["focused"] = False
        with self.assertRaises(RuntimeError):
            self._type(self._driver(settable=False, fail_paste=True))
        self.assertEqual(self.board["items"], [{"public.utf8-plain-text": "the user's own"}])


class FocusMovesWhilePreparing(_KeysFixture):
    """Codex on #66 @3f82851 (the second of the focus family): the key code for cmd+V was looked up after the focus
    check, and on a cold cache that is a child process (up to 20 s); the focus moved meanwhile and the text went into
    another field. Split by structure: everything slow is prepared before the first check, and between a check and
    its press there is nothing that can wait. Here every preparing call -- the layout read on a cold cache, the
    clipboard saved, each key event built, the window's event source, the text cut in chunks -- moves the focus to
    another field: nothing may land there, and the result is a refocus (then typed) or nothing sent."""

    def steal(self, *_a, **_k):
        self.el["focused"] = False                        # the user clicked into another field
        self.log.append("focus moved")

    def cold_cache(self):
        from deskmind_hands.drivers import peekaboo as P
        for name, value in (("_key_blobs", {}), ("_letter_codes", {}), ("_letters_read_at", 0.0)):
            p = mock.patch.object(PeekabooDriver, name, value)
            p.start()
            self.addCleanup(p.stop)

        def child(*a, **k):                               # the layout read: a child process, on a cold cache
            self.steal()
            return types.SimpleNamespace(stdout='{"events": {}, "table": {}}', returncode=0)
        p = mock.patch.object(P.subprocess, "run", child)
        p.start()
        self.addCleanup(p.stop)

    def driver(self, background=False, can_focus=True):
        from deskmind_hands.drivers import peekaboo as P, skylight
        self.el.update(focused=True, can_focus=can_focus)
        self.other = {"value": ""}
        d = self._driver(settable=False)
        del d._keycode                                    # the real one: a cold cache reads the layout
        self.cold_cache()
        pair = d._key_pair

        def stealing_pair(Q, code, flags=0):
            self.steal()
            return pair(Q, code, flags)
        d._key_pair = stealing_pair
        d._unicode_pair = lambda Q, chunk: self.steal() or (("down", "u", chunk), ("up", "u", chunk), ("key", 0))

        def post(Q, tap, ev, press=None, release=None, **_how):
            if not (isinstance(ev, tuple) and ev[0] == "down"):
                return
            into = self.shown if self.el["focused"] else self.other
            self.log.append(("key", ev[1], ev[2]))
            if ev[1] == 9:
                into["value"] += self.board["text"]       # the paste
            elif ev[1] in ("u", 0):
                into["value"] += ev[2] if isinstance(ev[2], str) else "?"
        p = mock.patch.object(P, "_post", post)
        p.start()
        self.addCleanup(p.stop)
        d._post_key = lambda Q, code, flags=0: post(Q, 0, ("down", code, flags))  # a key pressed whole: the same
        if background:
            self.front = 1111                              # the user is in another app
            del d._keys_in_background                      # the real background route
            d._background_keys = lambda: True
            d._ascii_input = lambda: True
            d._key_target = lambda: (4242, 200)
            key = held_window_key(self)

            class Enter:
                def __enter__(self_):
                    return key

                def __exit__(self_, *a):
                    return False
            outer = self

            class StealingQuartz(skylight.BackgroundQuartz):      # the window's event source: made while preparing
                def __init__(self_, pid, wid, origin, key=None):
                    outer.steal()
                    self_._q, self_.pid, self_.wid, self_.key = types.SimpleNamespace(
                        kCGEventFlagMaskCommand=1 << 20, kCGHIDEventTap=0,
                        CGEventCreateKeyboardEvent=lambda src, code, down: ("down" if down else "up", code, 0),
                        CGEventSetFlags=lambda ev, f: None, CGEventKeyboardSetUnicodeString=lambda ev, n, t: None
                    ), pid, wid, key
            chunks = skylight.text_chunks
            for name, fn in (("user_on", lambda pid: False), ("enter_window_key", lambda pid, wid: Enter()),
                             ("BackgroundQuartz", StealingQuartz),
                             ("text_chunks", lambda t, *a: self.steal() or chunks(t, *a))):
                p = mock.patch.object(skylight, name, fn)
                p.start()
                self.addCleanup(p.stop)
        return d

    def assert_nothing_in_the_other_field(self, res):
        self.assertEqual(self.other["value"], "", self.log)
        self.assertIn("focus moved", self.log)            # the preparing calls did move it
        self.assertFalse(res.ok and res.indeterminate, res.detail)   # never typed elsewhere then "unverified"

    def test_foreground_paste_refocuses_after_preparing(self):
        res = self._type(self.driver())
        self.assert_nothing_in_the_other_field(res)
        self.assertTrue(res.ok and not res.indeterminate, res.detail)
        self.assertTrue(self.shown["value"].endswith("ls -la"))

    def test_foreground_paste_with_no_way_to_refocus_sends_nothing(self):
        res = self._type(self.driver(can_focus=False))
        self.assert_nothing_in_the_other_field(res)
        self.assertIs(res.performed, False)
        self.assertEqual(self.pastes(), [])

    def test_foreground_replace_refocuses_after_preparing(self):
        d = self.driver()
        self.shown["value"] = ""                          # replacing: what the selection held is gone (the fake)
        res = self._type(d, clear=True)
        self.assert_nothing_in_the_other_field(res)
        self.assertTrue(res.ok and not res.indeterminate, res.detail)

    def test_background_keys_refocus_after_preparing(self):
        res = self._type(self.driver(background=True))
        self.assert_nothing_in_the_other_field(res)
        self.assertTrue(res.ok and not res.indeterminate, res.detail)

    def test_background_keys_with_no_way_to_refocus_send_nothing(self):
        res = self._type(self.driver(background=True, can_focus=False))
        self.assert_nothing_in_the_other_field(res)
        self.assertIs(res.performed, False)


class GateAtEverySendSite(_KeysFixture):
    """Review of #66 @8086268, the third of the "check before send" family: each send site picked its own checks --
    the WebKit background paste asked the focus but not the window (cmd+V went after the user came back to the app,
    reported verified), and a STOP during the preparing was asked only at the keys (the focus was still written).
    One Gate per route now, asked before every send. The table: every send site x {STOP, the window lost (or, in the
    foreground, the app no longer in front), the focus lost}, the condition arising right before that site. Nothing
    is sent there, and the result is honest: nothing sent when nothing went before, else unverified."""

    CMD = 1 << 20

    prepare_hook = staticmethod(lambda: None)                   # a row sets what happens while preparing

    def route(self, kind):
        from deskmind_hands.drivers import skylight
        d = self._driver(settable=False)
        pair = d._key_pair
        d._key_pair = lambda Q, code, flags=0: self.prepare_hook() or pair(Q, code, flags)
        d._unicode_pair = lambda Q, chunk: self.prepare_hook() or (("down", "u", chunk), ("up", "u", chunk), ("key", 0))
        q = types.SimpleNamespace(kCGEventFlagMaskCommand=self.CMD, kCGHIDEventTap=0)
        bq = skylight.BackgroundQuartz.__new__(skylight.BackgroundQuartz)
        bq._q, bq.pid, bq.wid, bq.key = q, 4242, 200, held_window_key(self)
        self.held = bq.key
        if kind != "foreground":
            self.front = 1111                                   # held in the background: the user is in another app
        if kind == "webkit":                                   # _flash's background branch: a held window
            d._flash = lambda app, perform, what="": (True, perform(bq))
        elif kind == "background":                             # _keys_in_background
            del d._keys_in_background
            d._background_keys = lambda: True
            d._ascii_input = lambda: True
            d._key_target = lambda: (4242, 200)

            class Enter:
                def __enter__(self_):
                    return bq.key

                def __exit__(self_, *a):
                    return False
            for name, fn in (("user_on", lambda pid: False), ("enter_window_key", lambda pid, wid: Enter())):
                p = mock.patch.object(skylight, name, fn)
                p.start()
                self.addCleanup(p.stop)

            def held(self_, pid, wid, origin, key=None):        # the real type, made without Quartz
                self_._q, self_.pid, self_.wid, self_.key = q, pid, wid, key
            p = mock.patch.object(skylight.BackgroundQuartz, "__init__", held)
            p.start()
            self.addCleanup(p.stop)
        return d

    def condition(self, kind, what):
        from deskmind_hands.grounding import STOP

        def flip():
            if what == "stop":
                STOP.set()
            elif what == "window" and kind == "foreground":
                self.front = 9999                               # the user brought another app forward
            elif what == "window":
                self.window_lost = "the user brought the app forward"
            elif what == "focus":
                self.el["focused"] = False                      # the user clicked another field
        return flip                                             # what None: the control, nothing happens

    @staticmethod
    def once(flip):
        fired = []

        def hook():
            if not fired:
                fired.append(1)
                flip()
        return hook

    def run_row(self, kind, site, what):
        self.prepare_hook = lambda: None
        flip = self.condition(kind, what)
        text, clear = ("x" * 40 if kind == "background" else "ls -la"), False
        if site == "focus write":
            self.el["focused"] = False
            self.prepare_hook = self.once(flip)                              # while preparing (Codex: STOP)
        elif site == "select":
            self.el["focused"], clear = False, True
            self.hooks["focus"] = flip
        elif site == "cmd+A":
            self.el["focused"], clear, self.select_takes = True, True, False
            self.hooks["select"] = flip
        elif site == "cmd+V":
            self.el["focused"], clear = True, True
            self.hooks["select"] = flip
        elif site == "chunk":
            self.el["focused"] = True
            self.hooks[("key", "u")] = self.once(flip)                       # after the first chunk
        d = self.route(kind)
        res = self._type(d, text=text, clear=clear)
        return res

    def sent_at(self, site):
        if site == "focus write":
            return "focus" in self.log
        if site == "select":
            return ("select", True) in self.log
        if site == "cmd+A":
            return ("key", 0, self.CMD) in self.log
        if site == "cmd+V":
            return ("key", 9, self.CMD) in self.log
        return len([x for x in self.log if isinstance(x, tuple) and x[:2] == ("key", "u")]) > 1

    def test_every_site_under_every_condition(self):
        from deskmind_hands.grounding import STOP
        sites = {"foreground": ("focus write", "select", "cmd+A", "cmd+V"),
                 "webkit": ("focus write", "select", "cmd+A", "cmd+V"),
                 "background": ("focus write", "select", "cmd+A", "chunk")}
        for kind, kind_sites in sites.items():
            for site in kind_sites:
                for what in ("stop", "window", "focus"):
                    if site == "focus write" and what == "focus":
                        continue                                # the focus is what that write sets
                    with self.subTest(route=kind, site=site, condition=what):
                        STOP.clear()
                        self.setUp()
                        res = self.run_row(kind, site, what)
                        self.assertFalse(self.sent_at(site), (res.detail, self.log))
                        if site == "focus write":               # nothing went before it
                            self.assertIs(res.performed, False, res.detail)
                        else:
                            self.assertIsNot(res.performed, False, res.detail)
                            self.assertTrue(res.indeterminate, res.detail)
                        STOP.clear()

    def test_the_control_every_site_is_reached_when_nothing_happens(self):
        from deskmind_hands.grounding import STOP
        for kind, site in (("foreground", "focus write"), ("foreground", "select"), ("foreground", "cmd+A"),
                           ("foreground", "cmd+V"), ("webkit", "focus write"), ("webkit", "select"),
                           ("webkit", "cmd+A"), ("webkit", "cmd+V"), ("background", "focus write"),
                           ("background", "select"), ("background", "cmd+A"), ("background", "chunk")):
            with self.subTest(route=kind, site=site):
                STOP.clear()
                self.setUp()
                self.run_row(kind, site, None)
                self.assertTrue(self.sent_at(site), self.log)

    # -- review of #66 @24fbf21
    def test_the_length_read_before_the_select_all_is_before_the_gate(self):
        """axfield.select read the text's length (it can wait) between the gate and the write: the focus moved
        there, and the selection was still written. The length is read first; the gate right before the write."""
        for kind in ("foreground", "webkit", "background"):
            with self.subTest(route=kind):
                self.setUp()
                self.el["focused"] = True
                self.hooks["length read"] = self.condition(kind, "focus")
                d = self.route(kind)
                res = self._type(d, text="x" * 40 if kind == "background" else "ls -la", clear=True)
                self.assertNotIn(("select", True), self.log)
                self.assertIs(res.performed, False, res.detail)

    def test_an_app_with_no_window_is_refused_cleanly_or_found_by_its_bundle(self):
        self.el["focused"] = True
        d = self.route("foreground")
        d._pid_of = lambda bundle, all_wins=None: None          # no layer-0 window
        res = self._type(d)                                     # nor a process of the bundle
        self.assertIs(res.performed, False, res.detail)
        self.assertIn("could not be found", res.detail)
        self.assertEqual(self.pastes(), [])
        self.setUp()
        self.el["focused"] = True
        self.bundle_apps = [types.SimpleNamespace(processIdentifier=lambda: 4242)]
        d = self.route("foreground")
        d._pid_of = lambda bundle, all_wins=None: None
        res = self._type(d)                                     # its process by bundle id: typed
        self.assertTrue(res.ok and not res.indeterminate, res.detail)

    def test_the_front_app_without_the_window_server_falls_back_to_nsworkspace(self):
        from deskmind_hands.drivers import skylight
        self.el["focused"] = True
        with mock.patch.object(skylight, "front_pid", lambda: None):    # the window server does not tell
            d = self.route("foreground")
            self.workspace_front = 4242                                    # NSWorkspace does
            res = self._type(d)
            self.assertTrue(res.ok and not res.indeterminate, res.detail)
        self.setUp()
        self.el["focused"] = True
        with mock.patch.object(skylight, "front_pid", lambda: None):
            d = self.route("foreground")
            self.workspace_front = None                                    # neither tells: refused
            res = self._type(d)
        self.assertIs(res.performed, False, res.detail)
        self.assertEqual(self.pastes(), [])

    # -- Codex on #66 @79cd02d: what can wait is asked first, the instant checks last
    def nth(self, n, flip):
        count = []

        def hook():
            count.append(1)
            if len(count) == n:
                flip()
        return hook

    def test_codex_a_stop_during_the_focus_read_before_the_paste_sends_nothing(self):
        from deskmind_hands.grounding import STOP
        self.el["focused"] = True
        self.hooks["focus read"] = self.nth(3, STOP.set)        # the read in cmd+V's gate (before: the route's
                                                                 # choice, then ready's)
        res = self._type(self.route("foreground"))
        self.assertEqual(self.pastes(), [])
        self.assertIs(res.performed, False, res.detail)

    def test_codex_the_front_app_changing_during_the_focus_read_sends_nothing(self):
        self.el["focused"] = True

        def away():
            self.front = 9999
        self.hooks["focus read"] = self.nth(3, away)
        res = self._type(self.route("foreground"))
        self.assertEqual(self.pastes(), [])
        self.assertIs(res.performed, False, res.detail)

    def test_a_stop_during_the_window_read_before_the_focus_write_writes_nothing(self):
        from deskmind_hands.grounding import STOP
        self.el["focused"] = False
        d = self.route("webkit")
        reads = self.nth(3, STOP.set)                            # the window read right before the focus write

        def lost():
            reads()
            return None
        self.held.lost = lost
        res = self._type(d)
        self.assertNotIn("focus", self.log)
        self.assertIs(res.performed, False, res.detail)

    # -- Codex on #66 @0838225: every condition with an instant form asked again last, on both routes
    def test_codex_the_user_coming_to_the_app_during_the_background_focus_read_sends_nothing(self):
        for kind in ("background", "webkit"):
            with self.subTest(route=kind):
                self.setUp()
                self.el["focused"] = True
                d = self.route(kind)

                def come_back():
                    self.front = 4242                           # the user brought the target app forward
                self.hooks["focus read"] = self.nth(3, come_back)   # the read in the first key's gate
                res = self._type(d, text="x" * 40 if kind == "background" else "ls -la")
                self.assertEqual(self.pastes(), [])
                self.assertEqual([x for x in self.log if isinstance(x, tuple) and x[1] == "u"], [])
                self.assertIs(res.performed, False, res.detail)

    def test_the_user_coming_to_the_app_during_the_window_read_before_the_focus_write_writes_nothing(self):
        self.el["focused"] = False
        d = self.route("webkit")

        def come_back():
            self.front = 4242
        reads = self.nth(3, come_back)                          # the window read right before the focus write

        def lost():
            reads()
            return None
        self.held.lost = lost
        res = self._type(d)
        self.assertNotIn("focus", self.log)
        self.assertIs(res.performed, False, res.detail)

    def test_every_instant_condition_on_every_route_during_the_last_read(self):
        """One row per instant condition, per route: it arises during the last read before the first key (the
        focus read in its gate) -- nothing is sent."""
        from deskmind_hands.grounding import STOP
        rows = (("foreground", "STOP", STOP.set), ("foreground", "the app no longer in front", "away"),
                ("webkit", "STOP", STOP.set), ("webkit", "the user came to the app", "back"),
                ("background", "STOP", STOP.set), ("background", "the user came to the app", "back"))
        for kind, name, flip in rows:
            with self.subTest(route=kind, condition=name):
                STOP.clear()
                self.setUp()
                self.el["focused"] = True
                d = self.route(kind)
                if flip == "away":
                    flip = lambda: setattr(self, "front", 9999)          # noqa: E731
                elif flip == "back":
                    flip = lambda: setattr(self, "front", 4242)          # noqa: E731
                self.hooks["focus read"] = self.nth(3, flip)
                res = self._type(d, text="x" * 40 if kind == "background" else "ls -la")
                self.assertEqual([x for x in self.log if isinstance(x, tuple) and x[0] == "key"], [])
                self.assertIs(res.performed, False, res.detail)

    def test_codex_the_webkit_paste_after_the_user_came_back_is_not_sent(self):
        res = self.run_row("webkit", "cmd+V", "window")
        self.assertNotIn(("key", 9, self.CMD), self.log)
        self.assertFalse(res.ok and not res.indeterminate, res.detail)          # never "verified"

    def test_codex_a_stop_while_preparing_writes_nothing(self):
        res = self.run_row("foreground", "focus write", "stop")
        self.assertNotIn("focus", self.log)
        self.assertIs(res.performed, False, res.detail)


class CheckedPressPostsWithoutAskingAgain(unittest.TestCase):
    """Review of #66 @92fbf45: a checked press (_press_checked: the window, then the focus) went on to
    BackgroundQuartz.CGEventPost, whose key check asks key.lost() again -- an accessibility read of the focused window
    that can wait its whole timeout, between the focus check and the key-down. Posted through post_checked now; every
    other post keeps the check."""

    def setUp(self):
        from deskmind_hands.drivers import skylight
        self.log = []
        q = types.ModuleType("Quartz")
        q.kCGEventKeyDown, q.kCGEventKeyUp, q.kCGEventFlagsChanged, q.kCGHIDEventTap = 10, 11, 12, 0
        q.CGEventGetType = lambda ev: ev[0]
        p = mock.patch.dict(sys.modules, {"Quartz": q})
        p.start()
        self.addCleanup(p.stop)
        for name, fn in (("_load", lambda: {}),
                         ("post_key_event", lambda pid, ev, auth=True: self.log.append(("posted", ev[0])))):
            p = mock.patch.object(skylight, name, fn)
            p.start()
            self.addCleanup(p.stop)
        key = skylight.WindowKey.__new__(skylight.WindowKey)
        key.lost = lambda: self.log.append("key.lost: an accessibility read that can wait") or None
        self.q = skylight.BackgroundQuartz(4242, 200, (0, 0), key=key)
        from deskmind_hands.drivers import peekaboo as P
        p = mock.patch.object(P, "_front_app_pid", lambda: 1111)          # the user is in another app
        p.start()
        self.addCleanup(p.stop)

    def test_a_checked_press_is_not_asked_again_between_the_check_and_the_down(self):
        from deskmind_hands.drivers import axfield, base
        from deskmind_hands.drivers.peekaboo import Gate
        gate = Gate.background(self.q, "FIELD")
        with base.input_scope(None), mock.patch.object(axfield, "is_focused",
                                                       lambda f: self.log.append("focus read") or True):
            why = PeekabooDriver._press_checked(self.q, (10,), (11,), ("key", 0), gate)
        self.assertIsNone(why)
        self.assertEqual(self.log, ["key.lost: an accessibility read that can wait", "focus read",   # the gate
                                    ("posted", 10), ("posted", 11)])                              # not asked again

    def test_a_check_that_is_not_a_gate_is_refused(self):
        with self.assertRaises(TypeError):
            PeekabooDriver._press_checked(self.q, (10,), (11,), ("key", 0), lambda: None)

    def test_post_checked_with_a_gate_of_another_window_asks_the_window(self):
        from deskmind_hands.drivers import skylight
        from deskmind_hands.drivers.peekaboo import Gate
        other = skylight.BackgroundQuartz(4242, 201, (0, 0), key=held_window_key(self, lambda: None))
        self.q.post_checked((10,), Gate.background(other, "FIELD"))
        self.assertEqual(self.log, ["key.lost: an accessibility read that can wait", ("posted", 10)])

    def test_any_other_key_down_keeps_the_check(self):
        from deskmind_hands.drivers.peekaboo import _post
        _post(self.q, 0, (10,))
        self.assertEqual(self.log, ["key.lost: an accessibility read that can wait", ("posted", 10)])


class TextTargetFoundByItsRole(unittest.TestCase):
    """Codex P2 @5fb2651: "it shows a caret" could never be reached -- the element is found by a text role at its
    place (axfield.field_at) before any caret is asked about -- and the tests stubbed that finding. Through the real
    _field_now: an AXGroup with a selected text range is no text target; an AXTextArea is."""

    def _driver(self):
        from deskmind_hands.geometry import Point, Rect
        d = PeekabooDriver.__new__(PeekabooDriver)
        d.app = "com.example.term"
        d._rects, d._element_role = {"elem_3": Rect(0, 30, 580, 350)}, {"elem_3": "textArea"}
        d._labels_by_id, d._role_labels = {"elem_3": "Shell"}, set()
        d._origin, d._snapshot = Point(100, 100), "s1"
        d._background_input = lambda bundle: False
        d._key_target = lambda: (4242, 200)
        return d

    def _tree(self, role):
        from deskmind_hands.drivers import axchrome, axfield
        el = {"AXRole": role, "AXSelectedTextRange": (0, 0), "AXChildren": [], "frame": (100, 130, 580, 350),
              "AXTitle": "Shell"}
        win = {"AXRole": "AXWindow", "AXChildren": [el], "frame": (100, 100, 600, 400)}
        return [mock.patch.object(axfield, "window", lambda pid, wid: win),
                mock.patch.object(axfield, "_get", lambda e, attr: e.get(attr)),
                mock.patch.object(axchrome, "_frame", lambda e: e.get("frame"))], el

    def _type(self, d):
        from deskmind_hands.actions import Action, ActionKind, Binding
        from deskmind_hands.drivers.base import input_scope
        d._element_settable = {"elem_3": False}
        d._element_value = {"elem_3": ""}
        d._mcp = SimpleNamespace(tool=lambda name, args: self.fail("no accessibility write here"))
        d._type_in_background = lambda eid, text, clear: None
        with input_scope(None):
            return d._execute_inner(Action(kind=ActionKind.TYPE_TEXT, text="ls", binding=Binding(element_id="elem_3")))

    def test_a_group_with_a_selected_text_range_is_no_text_target(self):
        patches, _ = self._tree("AXGroup")
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        d = self._driver()
        self.assertIsNone(d._field_now("elem_3"))
        res = self._type(d)
        self.assertIs(res.performed, False)
        self.assertIn("not a text element", res.detail)

    def test_a_text_area_is_found_as_the_text_target(self):
        patches, el = self._tree("AXTextArea")
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.assertIs(self._driver()._field_now("elem_3"), el)


class NewlinesGoInAsText(unittest.TestCase):
    """hands#66's newline rule on the command-line channel: a newline in the text is never a bare Return key. Peekaboo's
    `type` presses Return for each one (AXorcist Element.typeText), _type_multiline pressed it between lines, and the
    type_fg fallback typed it the same way. Text with a newline goes in as text, the way chosen before the first send:
    the element's value written (then verified or unverified, never a paste after), else a paste where the step may
    use the foreground, else nothing; a one-line field refuses it. Fail on 10d5b71."""

    def setUp(self):
        from deskmind_hands.drivers import axfield
        self.shown = {"value": "old"}
        for name, fn in (("value", lambda f: self.shown["value"]),
                         ("settled_value", lambda f, before, wait=0.6: self.shown["value"])):
            p = mock.patch.object(axfield, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def driver(self, role="textArea", settable=True, foreground=False, takes=True, paste="ok", ax_role="AXTextArea"):
        self.selected_all = False
        d = PeekabooDriver.__new__(PeekabooDriver)
        d._element_ax_role = {"e1": ax_role}
        d._pid_of = lambda bundle, all_wins=None: 4242
        d.app, d._mcp, d._snapshot = "com.example.notes", None, "s1"
        d._pinned_window, d._element_window, d._window_id = None, {"e1": ""}, "200"
        d._element_role, d._element_settable = {"e1": role}, {"e1": settable}
        d._element_value, d._labels_by_id = {"e1": "old"}, {"e1": "Body"}
        d.allow_foreground, d._blind_write = foreground, None
        d._foreground_actions = d._escalations = 0
        d._field_now = lambda eid: "FIELD"
        d._foreground_refusal = lambda: None
        d._browser_field_refusal = lambda vid: None
        # the field as the next observation reads it (the foreground route's read-back)
        d.observe = lambda: SimpleNamespace(elements=[SimpleNamespace(settable=True, value=self.shown["value"])])
        d._settle = lambda ok, detail: ExecResult(ok, detail)
        d.AX_INSERT_WAIT_S = 0.0
        self.sent = []

        def run(key, _extra=None, **fmt):
            self.sent.append((key, fmt.get("text"), fmt.get("chord")))
            if key == "set_value" and takes == "fails":
                return False, {}, "ERROR: the value could not be set"
            if key == "set_value" and takes:
                self.shown["value"] = fmt["text"]
            if key == "paste_fg":
                if paste == "unknown":
                    return False, {}, "UNKNOWN: the outcome is unknown"
                # a paste replaces the selection, else goes in at the caret (the end)
                self.shown["value"] = (fmt["text"] if self.selected_all else self.shown["value"] + fmt["text"])
            return True, {}, ""
        d._run = run
        return d

    def type_(self, d, text, clear=False, foreground=False):
        from deskmind_hands.actions import Action, ActionKind, Binding
        from deskmind_hands.drivers.base import input_scope
        with input_scope(None):
            return d._execute_inner(Action(kind=ActionKind.TYPE_TEXT, text=text, clear_first=clear,
                                           foreground=foreground, binding=Binding(element_id="e1")))

    def no_return(self):
        self.assertFalse([s for s in self.sent if s[0].startswith("press") and s[2] == "return"], self.sent)
        self.assertFalse([s for s in self.sent if s[0] in ("type", "type_clear", "type_fg") and "\n" in (s[1] or "")],
                         self.sent)

    def test_appended_text_with_a_newline_is_inserted_at_the_end(self):
        self.replace_fakes()
        self.insert_fakes()
        res = self.type_(self.driver(), "a\nb")
        self.assertEqual((self.sent, self.inserted), ([], ["a\nb"]))      # no value written over it
        self.assertEqual(self.shown["value"], "olda\nb")
        self.assertTrue(res.ok and not res.indeterminate, res.detail)

    def test_codex_an_append_keeps_what_loaded_after_the_observation(self):
        """Review of #68 @98654dd: cached + text was written as the value, deleting what loaded after the
        observation, and read as verified."""
        self.replace_fakes()
        self.insert_fakes()
        d = self.driver()
        self.shown["value"] = "old + loaded since"                       # the cache still says "old"
        res = self.type_(d, "a\nb")
        self.assertEqual(self.shown["value"], "old + loaded sincea\nb")
        self.assertFalse([x for x in self.sent if x[0] == "set_value"], self.sent)
        self.assertTrue(res.ok and not res.indeterminate, res.detail)

    # -- review of #68 @870f1df
    def test_an_append_to_a_web_page_field_is_never_an_accessibility_insertion(self):
        """In a web page (a chat box in Chrome or Electron) the insertion reads back and the page's own state stays
        old: a later submit sends the old content. Pasted in the foreground instead, or refused without it."""
        self.replace_fakes()
        self.insert_fakes()
        self.web = True
        d = self.replace_driver()
        res = self.type_(d, "a\nb", foreground=True)
        self.assertEqual(self.inserted, [])
        self.assertEqual([x[0] for x in self.sent], ["paste_fg"])
        self.setUp()
        self.replace_fakes()
        self.insert_fakes()
        self.web = True
        res = self.type_(self.driver(settable=False), "a\nb")             # no foreground: refused
        self.assertEqual((self.inserted, self.sent), ([], []))
        self.assertIs(res.performed, False, res.detail)

    def test_the_paste_goes_at_the_real_end_when_the_field_grew_while_the_app_came_forward(self):
        self.replace_fakes(focused=False)
        self.insert_fakes(front=4242)                                      # the app in front: the paste route
        d = self.replace_driver()
        raise_app = d._raise_app

        def raise_and_grow(bundle):
            self.shown["value"] += " + more"                               # loaded while the app came forward
            return raise_app(bundle)
        d._raise_app = raise_and_grow
        res = self.type_(d, "a\nb", foreground=True)
        self.assertEqual(self.select_wants, [(len("old + more"), 0)])      # the caret at the real end
        self.assertEqual(self.shown["value"], "old + morea\nb")
        self.assertTrue(res.ok and not res.indeterminate, res.detail)

    def test_codex_a_text_area_peekaboo_calls_textfield_is_typed(self):
        """Review of #68 @98654dd: Peekaboo's role is textField for AXTextArea too; TextEdit's body was refused."""
        self.replace_fakes()
        self.insert_fakes()
        res = self.type_(self.driver(role="textField", ax_role="AXTextArea"), "a\nb")
        self.assertEqual(self.inserted, ["a\nb"])
        self.assertTrue(res.ok, res.detail)
        for ax_role, refused in (("AXTextField", True), ("", False)):
            with self.subTest(ax_role=ax_role):
                self.setUp()
                self.replace_fakes()
                self.insert_fakes()
                res = self.type_(self.driver(role="textField", ax_role=ax_role), "a\nb")
                self.assertEqual(res.performed is False and "one-line" in res.detail, refused, res.detail)

    def test_replacing_with_lines_is_written_whole_and_not_shown_is_unverified_with_no_paste_after(self):
        res = self.type_(self.driver(takes=False, foreground=True), "a\nb", clear=True, foreground=True)
        self.assertEqual(self.sent, [("set_value", "a\nb", None)])             # no paste after the write
        self.no_return()
        self.assertTrue(res.indeterminate, res.detail)

    def test_a_one_line_field_refuses_a_newline(self):
        res = self.type_(self.driver(role="textField"), "a\nb")
        self.assertEqual(self.sent, [])
        self.assertIs(res.performed, False, res.detail)
        self.assertIn("press Return as its own step", res.detail)

    def test_no_value_to_write_pastes_in_the_foreground_and_never_types_it(self):
        self.replace_fakes()
        self.insert_fakes(front=4242)                             # the app in front: no insertion in the background
        d = self.driver(settable=False, foreground=True, paste="unknown")
        d._raise_app = lambda bundle: True
        d._front_bundle = lambda: d.app
        res = self.type_(d, "a\nb", foreground=True)
        self.assertEqual([s[0] for s in self.sent], ["paste_fg"])               # no type_fg after it
        self.no_return()
        self.assertIsNotNone(res)

    def test_no_value_and_no_foreground_sends_nothing(self):
        res = self.type_(self.driver(settable=False), "a\nb")
        self.assertEqual(self.sent, [])
        self.assertIs(res.performed, False, res.detail)

    def test_text_without_a_newline_is_typed_as_before(self):
        self.type_(self.driver(), "ab")
        self.assertEqual([s[0] for s in self.sent][:1], ["type"])

    # -- review of #68 @1194dde
    def test_a_lone_carriage_return_is_a_newline_before_any_route(self):
        """B1: a lone "\r" reached Peekaboo's `type`."""
        self.type_(self.driver(), "a\rb", clear=True)
        self.assertEqual(self.sent, [("set_value", "a\nb", None)])
        self.replace_fakes()
        self.insert_fakes(front=4242)                             # the app in front: the paste route
        d = self.driver(settable=False, foreground=True)
        d._raise_app, d._front_bundle = (lambda bundle: True), (lambda: d.app)
        self.type_(d, "a\rb", foreground=True)
        self.assertFalse([x for x in self.sent if "\r" in (x[1] or "")], self.sent)
        self.assertEqual(self.sent, [("paste_fg", "a\nb", None)])

    def test_text_with_a_newline_into_the_focused_field_is_never_submitted(self):
        """B2: TYPE_FOCUSED (no element) pasted, then pressed Return as the rename's commit -- a chat message sent."""
        from deskmind_hands.actions import Action, ActionKind, Binding
        from deskmind_hands.drivers.base import input_scope
        d = self.driver(settable=False, foreground=True)
        with input_scope(None):
            d._execute_inner(Action(kind=ActionKind.TYPE_TEXT, text="one\ntwo", foreground=True, binding=Binding()))
        self.assertIn("paste_fg", [x[0] for x in self.sent])
        self.assertFalse([x for x in self.sent if x[2] == "return"], self.sent)

    def replace_fakes(self, readable=True, selects=True, focused=True):
        from deskmind_hands.drivers import axfield, peekaboo as P
        self.writes, self.focused, self.front, self.reads = [], focused, 4242, []
        self.on_read = {}                                         # the n-th focus read: what happens during it

        from deskmind_hands.drivers import base

        def select_range(el, want, wait=0.3):
            self.writes.append("select all")
            self.select_wants = getattr(self, "select_wants", []) + [want]
            base.ax_counted(lambda: 0)                            # an accessibility write: counted as sent
            self.selected_all = selects and want[1] > 0            # a range selected; (n, 0) is a caret
            return selects

        def is_focused(el):                                       # a read that can wait
            self.reads.append(1)
            self.on_read.get(len(self.reads), lambda: None)()
            return self.focused

        def focus(el, wait=0.5):
            self.writes.append("focus")
            base.ax_counted(lambda: 0)
            self.focused = True
            return True
        for name, fn in (("wanted", lambda el, all_: (0, len(self.shown["value"])) if all_
                          else (len(self.shown["value"]), 0)),
                         ("_selection", lambda el: (0, 0) if readable else None),
                         ("is_focused", is_focused), ("focus", focus), ("select_range", select_range)):
            p = mock.patch.object(axfield, name, fn)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(P, "_front_app_pid", lambda: self.front)
        p.start()
        self.addCleanup(p.stop)

    def insert_fakes(self, front=1111):
        """The accessibility insertion at the end (axfield.insert), its gate asked first as the real one does."""
        from deskmind_hands.drivers import axfield, base
        self.inserted = []
        self.front = front                                        # the user's app in front: the target is not

        def insert(el, text, replace_all, check=None, want=None):
            why = check() if check is not None else None
            if why:
                return axfield.NOTHING, why
            base.ax_counted(lambda: 0)
            self.inserted.append(text)
            self.shown["value"] += text                           # at the end: what is there stays
            return axfield.TEXT, None
        self.web = False
        for name, fn in (("insert", insert), ("selection_readable", lambda el: True),
                         ("in_web_area", lambda el: self.web)):
            p = mock.patch.object(axfield, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def replace_driver(self, arrives_after=0):
        """The app's process is 4242; `arrives_after` reads of the front after the raise, it is in front."""
        d = self.driver(settable=False, foreground=True)
        reads = {"left": None}

        def raise_app(bundle):
            self.writes.append("raise")
            reads["left"] = arrives_after
            return True

        def front_bundle():
            if reads["left"] is not None:
                if reads["left"] <= 0:
                    self.front = 4242
                reads["left"] -= 1
            return d.app if self.front == 4242 else "com.example.other"
        d._raise_app, d._front_bundle = raise_app, front_bundle
        d._pid_of = lambda bundle, all_wins=None: 4242
        return d

    def test_replacing_with_lines_by_a_paste_selects_all_first_and_verifies_by_equality(self):
        """B3: a replace on the paste route did not clear -- "old" became "olda\nb" and read as verified."""
        self.replace_fakes()
        res = self.type_(self.replace_driver(), "a\nb", clear=True, foreground=True)
        self.assertEqual(self.writes, ["raise", "select all"])
        self.assertEqual([x[0] for x in self.sent], ["paste_fg"])
        self.assertEqual(self.shown["value"], "a\nb")
        self.assertTrue(res.ok and not res.indeterminate, res.detail)

    def test_codex_the_replace_reads_back_its_own_field_not_any_field(self):
        """Review of #68 @98654dd: the replace's read-back scanned every settable field -- another holding the text
        made a failed paste "verified"."""
        self.replace_fakes()
        d = self.replace_driver()
        real_run = d._run

        def run(key, _extra=None, **fmt):
            if key == "paste_fg":
                return False, {}, "the paste did not go"                 # the target unchanged
            return real_run(key, _extra, **fmt)
        d._run = run
        d.observe = lambda: SimpleNamespace(elements=[SimpleNamespace(settable=True, value="a\nb")])  # another field
        res = self.type_(d, "a\nb", clear=True, foreground=True)
        self.assertTrue(res.indeterminate, res.detail)
        self.assertNotIn("verified by read", res.detail)

    def test_a_replace_that_only_contains_the_text_is_not_verified(self):
        self.replace_fakes(selects=True)
        d = self.replace_driver()
        real_run = d._run

        def run(key, _extra=None, **fmt):                              # the paste lands after the old text anyway
            if key == "paste_fg":
                self.selected_all = False
            return real_run(key, _extra, **fmt)
        d._run = run
        res = self.type_(d, "a\nb", clear=True, foreground=True)
        self.assertEqual(self.shown["value"], "olda\nb")
        self.assertTrue(res.indeterminate, res.detail)                  # contains it, does not equal it

    def test_a_replace_whose_selection_cannot_be_read_is_refused_before_a_send(self):
        self.replace_fakes(readable=False)
        res = self.type_(self.replace_driver(), "a\nb", clear=True, foreground=True)
        self.assertEqual((self.sent, self.writes), ([], []))
        self.assertIs(res.performed, False, res.detail)

    def test_the_replace_gate_before_each_write(self):
        """#68 B3 behind the foreground Gate: the app brought forward, then the gate before the focus write, the
        select-all and the paste -- the focus read first, STOP and the front app last. A STOP or another app coming
        forward during the read before a write: that write and none after it is made."""
        from deskmind_hands.grounding import STOP
        order = ("focus write", "select all", "paste")
        for n, write in enumerate(order, start=1):              # the n-th focus read precedes that write
            for what in ("front", "stop"):
                with self.subTest(write=write, condition=what):
                    STOP.clear()
                    self.addCleanup(STOP.clear)
                    self.replace_fakes(focused=False)
                    d = self.replace_driver()

                    def arise(what=what):
                        if what == "stop":
                            STOP.set()
                        else:
                            self.front = 9999                      # the user brought another app forward
                    self.on_read[n] = arise
                    res = self.type_(d, "a\nb", clear=True, foreground=True)
                    made = [w for w in self.writes if w != "raise"] + [x[0] for x in self.sent]
                    expected = {"focus write": [], "select all": ["focus"], "paste": ["focus", "select all"]}[write]
                    self.assertEqual(made, expected)
                    if write == "focus write":
                        self.assertIs(res.performed, False, res.detail)
                    else:
                        self.assertTrue(res.indeterminate, res.detail)

    # -- review of #68 @1fb0766
    def test_the_replace_waits_for_the_app_to_come_forward(self):
        """Bringing the app forward is asynchronous: the gate read the old front and refused almost every time."""
        from deskmind_hands.drivers import peekaboo as P
        self.replace_fakes()
        self.front = 1111                                         # the user's app in front
        with mock.patch.object(P.time, "sleep", lambda s: None):
            res = self.type_(self.replace_driver(arrives_after=3), "a\nb", clear=True, foreground=True)
        self.assertEqual([x[0] for x in self.sent], ["paste_fg"])
        self.assertTrue(res.ok and not res.indeterminate, res.detail)

    def test_the_replace_whose_app_never_comes_forward_sends_nothing(self):
        from deskmind_hands.drivers import peekaboo as P
        self.replace_fakes()
        self.front = 1111
        with mock.patch.object(P.time, "sleep", lambda s: None):
            res = self.type_(self.replace_driver(arrives_after=999), "a\nb", clear=True, foreground=True)
        self.assertEqual([w for w in self.writes if w != "raise"] + [x[0] for x in self.sent], [])
        self.assertIs(res.performed, False, res.detail)
        self.assertIn("did not come to the front", res.detail)

    def test_a_browser_replace_is_refused_before_anything_is_raised(self):
        """The browser refusal came inside the paste, after the raise, the focus write and the select-all."""
        self.replace_fakes()
        d = self.replace_driver()
        d.app = "com.google.Chrome"
        del d._foreground_refusal                                 # the real one: a browser is never pasted into
        res = self.type_(d, "a\nb", clear=True, foreground=True)
        self.assertEqual((self.writes, self.sent), ([], []))
        self.assertIs(res.performed, False, res.detail)

    def test_a_failed_value_write_says_so(self):
        res = self.type_(self.driver(takes="fails"), "a\nb", clear=True)
        self.assertFalse(res.ok)
        self.assertTrue(res.indeterminate, res.detail)
        self.assertIn("failed", res.detail)

    def test_no_key_route_sends_a_newline_as_return(self):
        d = PeekabooDriver.__new__(PeekabooDriver)
        posted = []
        d._post_key = lambda Q, code, flags=0: posted.append(code)
        for nl in ("\n", "\r"):
            with self.assertRaises(ValueError):
                d._post_chunk(None, nl)
        self.assertEqual(posted, [])
