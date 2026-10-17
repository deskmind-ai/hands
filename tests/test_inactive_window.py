"""An app that is not in front is still observed, and a DONE on nothing seen is judged, never taken (deskmind#73, live
10-17). Safari was running but not the active app, one test window open and the user's own window minimised:
Peekaboo's app-target read found no window, the run saw "(no window open)", the planner said DONE at once, and
「打开 Safari，搜索新加坡的天气」 ended "completed" in 4 s with nothing searched. The page check was skipped: no page."""
from __future__ import annotations

import dataclasses
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from deskmind_hands.actions import ActionKind   # noqa: E402
from deskmind_hands.drivers.base import Element, ExecResult   # noqa: E402
from deskmind_hands.drivers.peekaboo import PeekabooDriver   # noqa: E402
from deskmind_hands.geometry import Point   # noqa: E402
from deskmind_hands.runtime import judge   # noqa: E402

from test_judge import DONE, Browser, Judged, run   # noqa: E402

NOT_FOUND = "WINDOW_NOT_FOUND: No window found for application com.apple.Safari"
#: Safari's window list in the live run: the user's own window minimised, the test window on screen and in front.
OWNER = {"window_id": 19085, "window_title": "收藏夹", "window_index": 0, "is_on_screen": False,
         "bounds": {"x": 0, "y": 0, "width": 1200, "height": 800}}
TEST = {"window_id": 19116, "window_title": "控制台 - 测试站点", "window_index": 1, "is_on_screen": True,
        "bounds": {"x": 100, "y": 60, "width": 1000, "height": 700}}
PAGE = {"coordinate_context": {"window": {"window_id": 19116}, "logical_bounds": [[100, 60], [1100, 760]]},
        "application_name": "Safari浏览器", "window_title": "控制台 - 测试站点",
        "ui_elements": [{"id": "e1", "role": "webArea", "ax_role": "AXWebArea", "label": "控制台"}]}


def safari(windows, readable=("19116",)):
    """The driver as the helper runs it, Peekaboo stubbed: `see` for the app finds no window (Safari not in front),
    `see` for a window by id reads it when it is one of `readable`."""
    d = PeekabooDriver.__new__(PeekabooDriver)
    d.app, d._home_app, d._stage_apps, d._mcp, d._window_app = "com.apple.Safari", "com.apple.Safari", [], None, {}
    d._obs_n, d._incomplete_reads, d._escalations = 0, 0, 0
    d._pinned_window, d._window_id, d._pin_window = None, None, True
    d.scratch, d.offer_folder_files, d._origin = Path("/nonexistent"), False, Point(0, 0)
    d._sparse_tree = lambda data: False
    d._pid_of = lambda bundle, all_wins=None: None
    d.calls, d.read = [], dict(PAGE)        # `read`: what a read by id returns (the real conversion runs on it)

    def fake_run(key, _extra=None, **kw):
        d.calls.append((key, kw.get("window")))
        if key == "see":
            return False, {}, NOT_FOUND
        if key == "see_window":
            if kw.get("window") not in readable:
                return False, {}, NOT_FOUND
            cc = {**d.read["coordinate_context"], "window": {"window_id": int(kw["window"])}}
            return True, {**d.read, "coordinate_context": cc}, ""
        if key == "window_list":
            return True, {"windows": [dict(w) for w in windows]}, ""
        raise AssertionError(key)

    d._run = fake_run
    return d


def observe(d):
    quartz = types.SimpleNamespace(kCGWindowListOptionIncludingWindow=8, CGWindowListCopyWindowInfo=lambda o, w: [])
    with mock.patch.dict(sys.modules, {"Quartz": quartz}):
        return d.observe()


class Observed(unittest.TestCase):
    def test_an_inactive_safari_with_one_open_window_is_observed(self):
        d = safari([OWNER, TEST])
        obs = observe(d)
        self.assertFalse(obs.no_window)
        self.assertEqual([e.id for e in obs.elements], ["e1"])
        self.assertEqual(obs.window_title, "控制台 - 测试站点")
        self.assertEqual(d._pinned_window, "19116")          # observed from now on, as a first look pins
        self.assertNotIn(("see_window", "19085"), d.calls)   # the user's minimised window is never read
        self.assertEqual([w.id for w in obs.windows], ["19116"])

    def test_the_front_one_of_two_on_screen(self):
        back = {**TEST, "window_id": 19117, "window_title": "后面的", "window_index": 2}
        d = safari([back, OWNER, TEST], readable=("19116", "19117"))
        observe(d)
        self.assertEqual(d._pinned_window, "19116")

    def test_only_a_minimised_window_is_no_window(self):
        d = safari([OWNER])
        obs = observe(d)
        self.assertTrue(obs.no_window)
        self.assertEqual(obs.elements, [])
        self.assertIsNone(d._pinned_window)
        self.assertEqual([k for k, _ in d.calls if k == "see_window"], [])

    def test_a_window_gone_before_it_is_read_is_no_window(self):
        d = safari([TEST], readable=())
        obs = observe(d)
        self.assertTrue(obs.no_window)
        self.assertIsNone(d._pinned_window)

    def test_the_no_window_look_lists_the_windows_it_may_switch_to(self):
        # Another app of the task stays reachable from an app with no window (as a FOCUS_WINDOW target).
        d = safari([OWNER])
        d._mcp, d._stage_apps = object(), ["TextEdit"]
        d._workspace_file_names = lambda: {"parts.txt"}
        d._preexisting = set()
        base = d._run

        def run_(key, _extra=None, **kw):
            if key == "window_list" and kw.get("app") == "com.apple.TextEdit":
                return True, {"windows": [{"window_id": 7, "window_title": "parts.txt", "is_on_screen": True}]}, ""
            return base(key, _extra, **kw)

        d._run = run_
        obs = observe(d)
        self.assertTrue(obs.no_window)
        self.assertEqual([w.id for w in obs.windows], ["7"])

    def test_a_missing_window_index_is_the_lists_order(self):
        d = safari([{**OWNER, "window_index": None}, {**TEST, "window_index": None}])
        observe(d)
        self.assertEqual(d._pinned_window, "19116")

    def test_the_no_window_look_marks_no_window_current(self):
        d = safari([OWNER])
        d._window_id = "19085"                  # left from an earlier look
        obs = observe(d)
        self.assertIsNone(d._window_id)
        self.assertEqual([w.id for w in obs.windows], [])

    def test_a_document_app_picks_only_this_tasks_document(self):
        # TextEdit not in front: the user's own document (open before the run) is never chosen, the task's is.
        d = safari([{"window_id": 5, "window_title": "日记.txt", "window_index": 0, "is_on_screen": True},
                    {"window_id": 6, "window_title": "parts.txt", "window_index": 1, "is_on_screen": True}],
                   readable=("5", "6"))
        d.app = d._home_app = "com.apple.textedit"
        d._workspace_file_names, d._preexisting = (lambda: {"parts.txt", "日记.txt"}), {"5"}
        observe(d)
        self.assertEqual(d._pinned_window, "6")
        self.assertNotIn(("see_window", "5"), d.calls)

    def test_an_untitled_finder_window_is_not_chosen(self):
        d = safari([{"window_id": 8, "window_title": "", "window_index": 0, "is_on_screen": True}], readable=("8",))
        d.app = d._home_app = "com.apple.finder"
        d._workspace_folder_names = lambda: {"ws"}
        self.assertIsNone(d._window_to_observe())

    def test_a_read_with_nothing_in_it_is_no_window(self):
        # A read that succeeds and holds nothing is judged like no window (review of #42).
        d = safari([OWNER, TEST])
        d.read = {**PAGE, "ui_elements": []}
        self.assertTrue(observe(d).no_window)

    def textedit(self, ui_elements):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        (tmp / "parts.txt").write_text("P-101\n")
        (tmp / "notes.txt").write_text("")
        doc = {"window_id": 6, "window_title": "parts.txt", "window_index": 0, "is_on_screen": True,
               "bounds": {"x": 0, "y": 0, "width": 600, "height": 500}}
        d = safari([doc], readable=("6",))
        d.app = d._home_app = "com.apple.TextEdit"
        d._mcp, d.ws, d.offer_folder_files, d._preexisting = object(), tmp, True, set()
        d._workspace_file_names = lambda: {"parts.txt", "notes.txt"}
        d.read = {**PAGE, "application_name": "文本编辑", "window_title": "parts.txt", "ui_elements": ui_elements}
        return observe(d)

    def test_a_read_of_nothing_with_the_harness_own_controls_is_no_window(self):
        # TextEdit read of nothing, through the real conversion: 保存, 另存为 and the folder's documents are added,
        # and they are not a window's content (review of #42: real 0, synthetic 5, the done judge never asked).
        obs = self.textedit([])
        self.assertTrue([e for e in obs.elements if e.synthetic])
        self.assertIn("syn:save", [e.id for e in obs.elements])
        self.assertTrue(obs.no_window)

    def test_a_document_read_with_its_text_is_a_window(self):
        obs = self.textedit([{"id": "t1", "role": "AXTextArea", "label": "", "value": "P-101\n",
                              "is_value_settable": True}])
        self.assertIn("t1", [e.id for e in obs.elements])
        self.assertFalse(obs.no_window)

    def test_a_ghost_window_is_no_window(self):
        # Safari's 64x64 untitled window with no accessibility window behind it, read because it was pinned.
        ghost = {"window_id": 77, "window_title": "", "is_on_screen": True, "observation_capability": "pixels_only",
                 "bounds": {"x": 0, "y": 900, "width": 64, "height": 64}}
        d = safari([OWNER, TEST, ghost], readable=("77",))
        d._pinned_window = "77"
        self.assertTrue(observe(d).no_window)

    def test_a_small_real_window_is_a_window(self):
        # A small window with a title and a tree (a 140x90 panel) is a real one: not caught (review of #42).
        small = {"window_id": 78, "window_title": "下载", "is_on_screen": True,
                 "bounds": {"x": 0, "y": 0, "width": 140, "height": 90}}
        d = safari([small], readable=("78",))
        self.assertFalse(observe(d).no_window)
        self.assertEqual(d._pinned_window, "78")

    def vision_look(self, rows, windows=(TEST,), app="com.apple.Safari", readable=("19116",), d=None, box=None):
        # Safari read from its pixels (an empty tree sends it here on its own): OCR and the grounder stood in for.
        import os
        from deskmind_hands import vision
        d = d or safari(list(windows), readable=readable)
        d.app = d._home_app = app
        d.read = {**PAGE, "ui_elements": []}
        with mock.patch.dict(os.environ, {"HANDS_VISION_APPS": app.lower()}), \
                mock.patch.object(vision, "ocr", lambda shot: [dict(r) for r in rows]), \
                mock.patch.object(vision, "_icons", lambda *a, **k: {}), \
                mock.patch.object(vision, "ground_one", lambda *a, **k: box):
            return observe(d)

    def test_a_picture_with_nothing_found_in_it_is_no_window(self):
        # OCR and the grounder found nothing; the scroll buttons and generic controls are added anyway, and counted
        # they passed for 15 elements of a window (review of #42).
        obs = self.vision_look([])
        self.assertTrue(obs.elements)
        self.assertFalse([e for e in obs.elements if e.id.startswith(("ocr:", "icon:")) and "scroll" not in e.id])
        self.assertTrue(obs.no_window)

    def test_read_from_its_pixels_the_users_minimised_window_is_never_pinned(self):
        # By size alone the user's minimised Safari window (the larger one) was pinned and read (review of #42).
        d = safari([OWNER, TEST], readable=("19085", "19116"))
        self.vision_look([], d=d)
        self.assertEqual(d._pinned_window, "19116")
        self.assertNotIn(("see_window", "19085"), d.calls)

    def test_read_from_its_pixels_with_no_window_allowed_is_no_window(self):
        d = safari([OWNER], readable=("19085",))
        obs = self.vision_look([], d=d)
        self.assertTrue(obs.no_window)
        self.assertIsNone(d._pinned_window)
        self.assertEqual([k for k, _ in d.calls if k in ("see", "see_window")], [])

    def test_read_from_its_pixels_another_app_keeps_its_largest_window(self):
        # The music app's 500x500 side window in front: its main window, the largest, is still the one read.
        side = {"window_id": 31, "window_title": "迷你播放器", "window_index": 0, "is_on_screen": True,
                "bounds": {"x": 0, "y": 0, "width": 500, "height": 500}}
        main = {"window_id": 32, "window_title": "音乐", "window_index": 1, "is_on_screen": True,
                "bounds": {"x": 0, "y": 0, "width": 1200, "height": 800}}
        put_away = {"window_id": 33, "window_title": "大窗口", "window_index": 2, "is_on_screen": False,
                    "bounds": {"x": 0, "y": 0, "width": 1600, "height": 1000}}
        d = safari([side, main, put_away], readable=("31", "32", "33"))
        self.vision_look([], d=d, app="com.example.music")
        self.assertEqual(d._pinned_window, "32")

    def test_words_only_in_its_search_box_is_a_window(self):
        # The only words on screen are the query in the search box: they become the box's value, not an ocr:
        # element, and counted by element the window read as nothing (review of #42).
        query = {"text": "新加坡 天气", "x": 0.3, "y": 0.05, "w": 0.2, "h": 0.03, "conf": 0.9}
        obs = self.vision_look([query], box=(400.0, 45.0))      # the grounder puts the box on those words
        box = next(e for e in obs.elements if e.id == "gen:搜索框")
        self.assertEqual(box.value, "新加坡 天气")
        self.assertFalse([e for e in obs.elements if e.id.startswith("ocr:")])
        self.assertFalse(obs.no_window)

    def test_a_picture_with_words_in_it_is_a_window(self):
        obs = self.vision_look([{"text": "新加坡 天气 28°C", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.03, "conf": 0.9}])
        self.assertTrue([e for e in obs.elements if e.id.startswith("ocr:")])
        self.assertFalse(obs.no_window)

    def test_the_no_window_look_never_offers_a_put_away_window(self):
        # A minimised TextEdit document (any app's, not only a browser's) is no place to send the planner (review).
        d = safari([OWNER])
        d._mcp, d._stage_apps = object(), ["TextEdit"]
        d._workspace_file_names = lambda: {"parts.txt", "notes.txt"}
        d._preexisting = set()
        base = d._run

        def run_(key, _extra=None, **kw):
            if key == "window_list" and kw.get("app") == "com.apple.TextEdit":
                return True, {"windows": [{"window_id": 7, "window_title": "parts.txt", "is_on_screen": True},
                                          {"window_id": 8, "window_title": "notes.txt", "is_on_screen": False}]}, ""
            return base(key, _extra, **kw)

        d._run = run_
        obs = observe(d)
        self.assertTrue(obs.no_window)
        self.assertEqual([w.id for w in obs.windows], ["7"])

    def test_the_trace_keeps_it(self):
        import json
        import tempfile
        from deskmind_hands.record.recorder import Recorder
        from deskmind_hands.replay import observation
        obs = observe(safari([OWNER]))
        with tempfile.TemporaryDirectory() as tmp:
            rec = Recorder(Path(tmp), keep_screenshots=False)
            rec.observation(obs)
            rec.close()
            line = json.loads((Path(tmp) / "trace.jsonl").read_text().splitlines()[0])
        self.assertTrue(line["no_window"])
        self.assertTrue(observation(line).no_window)

    def test_the_run_summary_says_why(self):
        d = safari([OWNER, TEST])
        d._foreground_actions = d._scripted_actions = d._projected_actions = 0
        d.transport = "mcp"
        observe(d)
        self.assertEqual(d.state()["observed_by_id"], 1)
        self.assertIn("WINDOW_NOT_FOUND", d.state()["app_read_miss"])


class NoWindowBrowser(Browser):
    """The mock desktop as the run saw it: Safari with no window to observe, so no page either."""

    app = "com.apple.Safari"

    presses = 0

    def observe(self):
        obs = super().observe()
        # Nothing to read but the picture, which a step that went through changes (the closed document is gone).
        return dataclasses.replace(obs, elements=[], windows=[], focused_app="com.apple.Safari (no window open)",
                                   accepts_keys=False, no_window=True, screenshot_png=b"after %d" % self.presses)

    def execute(self, action):
        if action.kind is ActionKind.KEY:       # the save-and-close, as a step that went through
            self.presses += 1
            return ExecResult(True, f"pressed {action.keys}: saved and closed 'notes.txt'")
        return super().execute(action)


SAVE_CLOSE = {"kind": "key", "keys": "cmd+s"}


def run_unseen(goal, adapter, actions, done_check=True):
    with mock.patch("test_judge.Browser", NoWindowBrowser):
        return run(goal, actions, adapter, page=None, done_check=done_check)


class Sequenced(Judged):
    """Answers the done judgements in turn; None is no answer (the judge down)."""

    def __init__(self, actions, answers):
        super().__init__(actions)
        self.answers = list(answers)

    def judge_choice(self, state, instructions, criteria):
        self.asked.append(state)
        choice = self.answers.pop(0)
        if choice is None:
            raise RuntimeError("no answer")
        return {"choice": choice, "probabilities": {choice: 0.9}}


class DoneOnNothingSeen(unittest.TestCase):
    def test_done_with_no_window_is_judged_and_held_back(self):
        adapter = Judged([DONE] * 4, done=judge.NOT_SHOWN)
        res, _, _ = run_unseen("打开 Safari，搜索新加坡的天气", adapter, [DONE] * 4)
        self.assertNotEqual(res.state.value, "completed")
        self.assertEqual(res.failure.cls.name, "FALSE_COMPLETION")
        self.assertTrue([s for s in res.steps if s.get("kind") == "done_blocked"])
        self.assertIn("看到的", adapter.asked[0])
        self.assertIn("com.apple.Safari", adapter.asked[0])
        judged = [s for s in res.steps if s.get("kind") == "judged"]
        self.assertEqual({s["about"] for s in judged}, {"result_no_window"})

    def test_a_result_that_needs_no_window_completes(self):
        # 「另存为 notes.txt，然后关掉文稿」 ends with no window by design: the judge says so and the run completes.
        adapter = Judged([SAVE_CLOSE, DONE], done=judge.NO_WINDOW_NEEDED)
        res, _, _ = run_unseen("把草稿另存为 notes.txt，然后关掉文稿", adapter, [SAVE_CLOSE, DONE])
        self.assertEqual(res.state.value, "completed")
        self.assertEqual(len(adapter.asked), 1)
        self.assertFalse([s for s in res.steps if s.get("kind") == "done_blocked"])

    def test_no_window_needed_before_anything_was_done_is_not_taken(self):
        # 「在 notes.txt 末尾加一行「明天开会」，保存后关闭」, DONE on the first look: the judge said the result needs no
        # window, and the run completed with nothing written (review of #42). Taken as no answer: held once, then
        # unverified.
        adapter = Judged([DONE] * 3, done=judge.NO_WINDOW_NEEDED)
        res, _, _ = run_unseen("在 notes.txt 末尾加一行「明天开会」，保存后关闭", adapter, [DONE] * 3)
        self.assertNotEqual(res.state.value, "completed")
        self.assertEqual(res.metrics.actions, 0)
        blocked = [s["text"] for s in res.steps if s.get("kind") == "done_blocked"]
        self.assertIn("no step of this run has gone through", blocked[0])
        judged = [s for s in res.steps if s.get("kind") == "judged"]
        self.assertTrue(judged and judged[0].get("not_taken"))

    def test_not_shown_says_check_never_do_it_again(self):
        # Told to "do it there", a run that had appended its line and closed the document would append it again.
        adapter = Judged([SAVE_CLOSE, DONE, DONE], done=judge.NOT_SHOWN)
        res, _, _ = run_unseen("在 notes.txt 末尾加一行「明天开会」，保存后关闭", adapter, [SAVE_CLOSE, DONE, DONE])
        blocked = [s["text"] for s in res.steps if s.get("kind") == "done_blocked"]
        self.assertIn("check whether it was done", blocked[0])
        self.assertNotIn("do it there", blocked[0])

    def test_a_page_on_an_empty_look_and_a_silent_judge_is_not_completed(self):
        # The look read nothing (no_window) but a page is attached; the judge is down: unverified, not completed.
        with mock.patch("test_judge.Browser", NoWindowBrowser):
            from test_judge import CONSOLE
            res, _, _ = run("打开 Safari，搜索新加坡的天气", [DONE] * 3, Judged([DONE] * 3, down=True), page=CONSOLE,
                            done_check=True)
        self.assertNotEqual(res.state.value, "completed")
        self.assertTrue([s for s in res.steps if s.get("kind") == "done_blocked"
                         and "could not be confirmed" in s["text"]])

    def test_the_two_options_only(self):
        # Its own second option: "结果本来就不在网页上" is always true of a document app (review).
        adapter = Judged([DONE], done=judge.NO_WINDOW_NEEDED)
        run_unseen("打开 Safari，搜索新加坡的天气", adapter, [DONE])
        self.assertNotIn(f"- {judge.SHOWN}:", adapter.asked[0])
        self.assertNotIn(f"- {judge.NOT_ON_A_PAGE}:", adapter.asked[0])
        self.assertIn(f"- {judge.NOT_SHOWN}:", adapter.asked[0])
        self.assertIn(f"- {judge.NO_WINDOW_NEEDED}:", adapter.asked[0])
        self.assertIn("助手做过的事", adapter.asked[0])

    def test_the_options_go_no_window_needed_first_and_ask_what_was_done(self):
        # The local Brain mostly took the option in first place: 没出来 first, 2 of 6 save-and-close runs passed (dev set,
        # review of #42). Asked whether the steps did what was asked, 6/6 and 7/7 with the run guard, either order.
        self.assertEqual(list(judge.DONE_UNSEEN), [judge.NO_WINDOW_NEEDED, judge.NOT_SHOWN])
        self.assertIn("做完了吗", judge.DONE_UNSEEN_QUESTION)

    def test_the_judge_is_told_what_the_run_did(self):
        from deskmind_hands.runtime.loop import _what_was_done
        steps = [{"describe": "type_text #t1", "ok": True, "detail": "typed 3 lines"},
                 {"describe": "click #x", "ok": False, "detail": "refused"},
                 {"kind": "judged"},
                 {"describe": "close_document", "ok": True, "detail": "saved and closed 'notes.txt'"}]
        self.assertEqual(_what_was_done(steps), ["type_text #t1：typed 3 lines",
                                                 "close_document：saved and closed 'notes.txt'"])
        prompt = []
        judge.done_unseen(lambda p, s, o: prompt.append(p) or {"choice": judge.NO_WINDOW_NEEDED}, "g", "TextEdit",
                          done=_what_was_done(steps))
        self.assertIn("saved and closed 'notes.txt'", prompt[0])

    def test_no_judgement_is_held_back_once_then_unverified(self):
        # The judge down, or an answer off its options: never completed. Held back once with the reason, and
        # insisted on, the run ends unverified (review of #42).
        for adapter in (Judged([DONE] * 4, down=True), Judged([DONE] * 4, done="随便")):
            res, _, _ = run_unseen("打开 Safari，搜索新加坡的天气", adapter, [DONE] * 4)
            self.assertEqual(res.state.value, "gave_up")
            self.assertTrue(res.failure.detail.startswith("not verified"))
            blocked = [s for s in res.steps if s.get("kind") == "done_blocked"]
            self.assertTrue(blocked)
            self.assertIn("could not be confirmed", blocked[0]["text"])
            self.assertTrue([s for s in res.steps if s.get("kind") == "done_unverified"])

    def test_after_other_checks_no_judgement_is_still_held_back_once(self):
        # done_check's objections took both blocks; the final DONE has no judgement: held back once, then unverified.
        adapter = Judged([DONE] * 5, down=True)
        adapter.done_objection = lambda ctx: "the file is not saved"
        res, _, _ = run_unseen("打开 Safari，搜索新加坡的天气", adapter, [DONE] * 5)
        self.assertNotEqual(res.state.value, "completed")     # here the unchanged screen ends it first (no progress)
        unverified = [s for s in res.steps if s.get("kind") == "done_blocked" and "confirmed" in s["text"]]
        self.assertEqual(len(unverified), 1)

    def test_no_answer_after_not_shown_keeps_not_shown(self):
        adapter = Sequenced([DONE] * 4, [judge.NOT_SHOWN, None, None, None])
        res, _, _ = run_unseen("打开 Safari，搜索新加坡的天气", adapter, [DONE] * 4)
        self.assertEqual(res.failure.cls.name, "FALSE_COMPLETION")
        kept = [s for s in res.steps if s.get("kind") == "judged" and s.get("kept")]
        self.assertTrue(kept)
        self.assertEqual({s["verdict"] for s in kept}, {judge.NOT_SHOWN})

    def test_not_shown_is_dropped_once_the_run_acted(self):
        # 没出来, then a step that went through (the save-and-close), then the judge times out: the old 没出来 is
        # about a screen that is gone; unverified, not a false completion (review of #42).
        key = {"kind": "key", "keys": "cmd+s"}
        adapter = Sequenced([DONE, key, DONE, DONE], [judge.NOT_SHOWN, None, None])
        res, _, _ = run_unseen("把草稿另存为 notes.txt，然后关掉文稿", adapter, [DONE, key, DONE, DONE])
        self.assertEqual(res.metrics.actions, 1)
        self.assertNotEqual(res.state.value, "completed")
        self.assertNotEqual(res.failure.cls.name, "FALSE_COMPLETION")
        self.assertFalse([s for s in res.steps if s.get("kept")])
        blocked = [s["text"] for s in res.steps if s.get("kind") == "done_blocked"]
        self.assertIn("could not be confirmed", blocked[1])       # the second DONE: unverified, not 没出来 again

    def test_not_shown_is_dropped_once_the_user_changed_the_request(self):
        # 没出来, then the user withdraws the page (「不用打开网页了，保持窗口关闭即可」), then the judge times out: the
        # old 没出来 judged a request that no longer stands; unverified, not 没出来 again (review of #42).
        ask = {"kind": "ask_user", "text": "要打开网页吗？"}

        class Withdraws:
            def respond(self, question, approval, options=()):
                return "不用打开网页了，保持窗口关闭即可", True, 0.0

        adapter = Sequenced([DONE, ask, DONE, DONE], [judge.NOT_SHOWN, None, None])
        with mock.patch("test_judge.Browser", NoWindowBrowser):
            res, _, _ = run("打开 Safari，搜索新加坡的天气", [DONE, ask, DONE, DONE], adapter, page=None,
                            user=Withdraws(), done_check=True)
        self.assertEqual(res.metrics.actions, 0)
        self.assertNotEqual(res.state.value, "completed")
        self.assertFalse([s for s in res.steps if s.get("kept")])
        blocked = [s["text"] for s in res.steps if s.get("kind") == "done_blocked"]
        self.assertIn("could not be confirmed", blocked[1])

    def test_a_screen_locked_before_the_done_is_taken_is_not_completed(self):
        # Observed unlocked, locked by the time the planner says DONE: the run ends as the environment's (#40).
        from test_locked_session import LOCKED, UNLOCKED, Planner, run_live
        locked_now = []
        planner = Planner(on_turn=lambda n: locked_now.append(True))
        res, _ = run_live(planner, lambda: LOCKED if locked_now else UNLOCKED)
        self.assertEqual(planner.turns, 1)                # it saw the screen, and said DONE
        self.assertNotEqual(res.state.value, "completed")
        self.assertEqual(res.failure.cls.value, "environment", res.failure)
        self.assertIn("screen is locked", res.failure.detail)

    def test_a_silent_judge_is_not_asked_again(self):
        adapter = Sequenced([DONE] * 3, [None, judge.NO_WINDOW_NEEDED, judge.NO_WINDOW_NEEDED])
        res, _, _ = run_unseen("打开 Safari，搜索新加坡的天气", adapter, [DONE] * 3)
        self.assertEqual(len(adapter.asked), 1)
        self.assertEqual(res.state.value, "gave_up")

    def test_an_adapter_that_cannot_judge_keeps_the_old_behaviour(self):
        # No judge_choice (anthropic_cu, openai_cu, a hybrid over one): a save-and-close is not failed for a missing
        # capability; the run says the check was off (review of #42).
        from deskmind_hands.adapters.scripted import ReplayAdapter
        res, _, _ = run_unseen("把草稿另存为 notes.txt，然后关掉文稿", ReplayAdapter([DONE]), [DONE])
        self.assertEqual(res.state.value, "completed")
        self.assertTrue(res.checks["no_window_judge"].startswith("off"))
        self.assertIn("checks", res.to_json())
        res, _, _ = run_unseen("把草稿另存为 notes.txt，然后关掉文稿", Judged([DONE], done=judge.NO_WINDOW_NEEDED), [DONE])
        self.assertEqual(res.checks["no_window_judge"], "on")

    def test_a_hybrid_passes_its_planners_judgement_through(self):
        from deskmind_hands.adapters.hybrid import HybridAdapter

        class Planner:
            def judge_choice(self, state, instructions, criteria):
                return {"choice": judge.NO_WINDOW_NEEDED}

        self.assertTrue(callable(getattr(HybridAdapter(Planner(), object()), "judge_choice", None)))
        self.assertIsNone(getattr(HybridAdapter(object(), object()), "judge_choice", None))

    def test_a_later_answer_replaces_an_earlier_not_shown(self):
        adapter = Sequenced([DONE, SAVE_CLOSE, DONE], [judge.NOT_SHOWN, judge.NO_WINDOW_NEEDED])
        res, _, _ = run_unseen("把草稿另存为 notes.txt，然后关掉文稿", adapter, [DONE, SAVE_CLOSE, DONE])
        self.assertEqual(res.state.value, "completed")

    def test_blocks_spent_by_another_check_then_no_window_needed_completes(self):
        # done_check's own objections took both blocks; the final DONE on no window is judged once, and passes.
        adapter = Judged([DONE, DONE, SAVE_CLOSE, DONE], done=judge.NO_WINDOW_NEEDED)
        adapter.done_objection = lambda ctx: "the file is not saved"
        res, _, _ = run_unseen("把草稿另存为 notes.txt，然后关掉文稿", adapter, [DONE, DONE, SAVE_CLOSE, DONE])
        self.assertEqual(res.state.value, "completed")
        self.assertEqual(len(adapter.asked), 1)

    def test_a_task_with_its_window_open_and_no_page_is_not_judged(self):
        # A non-web task that ends with its window on screen completes as before, without a judgement.
        adapter = Judged([DONE], done=judge.NOT_SHOWN)
        res, _, _ = run("把草稿另存为 notes.txt", [DONE], adapter, page=None, done_check=True)
        self.assertEqual(res.state.value, "completed")
        self.assertEqual(adapter.asked, [])

    def test_off_without_the_done_check(self):
        adapter = Judged([DONE], done=judge.NOT_SHOWN)
        res, _, _ = run_unseen("打开 Safari，搜索新加坡的天气", adapter, [DONE], done_check=False)
        self.assertEqual(adapter.asked, [])


if __name__ == "__main__":
    unittest.main()
