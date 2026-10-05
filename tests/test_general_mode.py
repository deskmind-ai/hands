"""General mode (`hands do`, the app's path) and the gym: pure logic, no desktop, no model.

What 09-28/29 fixed or added, each pinned by the failure it came from: other apps' windows offered to a `do` run
(a Safari-to-TextEdit request), the run's own documents told apart from same-titled ones, a three-app cycle stopped, a newly opened
window followed, the ambiguity question's distinct lines, ANSWER candidates split into fields, vision controls kept
out of the page text, the decision panel's top operations -- and the gym oracles and traps.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.adapters.systemone import SystemOneAdapter, ambiguity_question, answer_candidates, top_operations
from deskmind_hands.cli import task_apps
from deskmind_hands.drivers.base import Element
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
