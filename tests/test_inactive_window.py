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

from deskmind_hands.drivers.base import Element   # noqa: E402
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
    d._element_cache = lambda data, origin: [Element(id="e1", role="webArea", ax_role="AXWebArea", label="控制台")]
    d.calls = []

    def fake_run(key, _extra=None, **kw):
        d.calls.append((key, kw.get("window")))
        if key == "see":
            return False, {}, NOT_FOUND
        if key == "see_window":
            return (True, PAGE, "") if kw.get("window") in readable else (False, {}, NOT_FOUND)
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

    def observe(self):
        obs = super().observe()
        return dataclasses.replace(obs, elements=[], windows=[], focused_app="com.apple.Safari (no window open)",
                                   accepts_keys=False, no_window=True)


def run_unseen(goal, adapter, actions, done_check=True):
    with mock.patch("test_judge.Browser", NoWindowBrowser):
        return run(goal, actions, adapter, page=None, done_check=done_check)


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
        adapter = Judged([DONE], done=judge.NO_WINDOW_NEEDED)
        res, _, _ = run_unseen("把草稿另存为 notes.txt，然后关掉文稿", adapter, [DONE])
        self.assertEqual(res.state.value, "completed")
        self.assertEqual(len(adapter.asked), 1)
        self.assertFalse([s for s in res.steps if s.get("kind") == "done_blocked"])

    def test_the_two_options_only(self):
        # Its own second option: "结果本来就不在网页上" is always true of a document app (review).
        adapter = Judged([DONE], done=judge.NO_WINDOW_NEEDED)
        run_unseen("打开 Safari，搜索新加坡的天气", adapter, [DONE])
        self.assertNotIn(f"- {judge.SHOWN}:", adapter.asked[0])
        self.assertNotIn(f"- {judge.NOT_ON_A_PAGE}:", adapter.asked[0])
        self.assertIn(f"- {judge.NOT_SHOWN}:", adapter.asked[0])
        self.assertIn(f"- {judge.NO_WINDOW_NEEDED}:", adapter.asked[0])
        self.assertIn("助手做过的事", adapter.asked[0])

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

    def test_no_judgement_holds_nothing_back(self):
        # The judge down: no verdict, and the run completes as it did (only 没出来 holds a run back).
        res, _, _ = run_unseen("打开 Safari，搜索新加坡的天气", Judged([DONE], down=True), [DONE])
        self.assertEqual(res.state.value, "completed")

    def test_blocks_spent_by_another_check_then_no_window_needed_completes(self):
        # done_check's own objections took both blocks; the final DONE on no window is judged once, and passes.
        adapter = Judged([DONE] * 3, done=judge.NO_WINDOW_NEEDED)
        adapter.done_objection = lambda ctx: "the file is not saved"
        res, _, _ = run_unseen("把草稿另存为 notes.txt，然后关掉文稿", adapter, [DONE] * 3)
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
