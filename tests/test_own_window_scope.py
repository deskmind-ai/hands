"""The run's own browser window, from the 0.4.3-rc.3 live test (10-17, hands 11714c0). Nothing here touches the Mac.

Run 5, 「打开 Safari，搜索新加坡的天气」 with the user's Safari windows minimised and Finder in front: a search, a click,
a click Peekaboo could not confirm (sent, its result not read back), and DONE -- completed with no judgement of the page at all. A page the driver could not read
(or one the judge gave no answer on) passed for "no web page", and a DONE there was taken as it was.

Run 4, 「把网页上的零件表抄进 parts.txt」 with the parts page in the user's window: the run's window stayed on its marker
page, and in 68 s the run clicked 「显示边栏」 and double-clicked the sidebar's 「默认，含1个标签页的标签页组」, 「边栏」 and
「单元格」 -- the user's tab groups -- twelve clicks, until it ended as a loop. It never asked: the look did not say
its window was empty, and ASK was not offered. (Which actions are in scope is for a model to judge, not a per-app list
of controls: the computer-use design, step 5.)

And `preexisting_windows` said 9 to 15 while Safari had 2 to 6 windows.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from deskmind_hands.actions import ActionKind   # noqa: E402
from deskmind_hands.adapters.base import TurnContext   # noqa: E402
from deskmind_hands.adapters.scripted import ReplayAdapter   # noqa: E402
from deskmind_hands.adapters.systemone import OWN_WINDOW_QUESTION, SystemOneAdapter   # noqa: E402
from deskmind_hands.drivers import axchrome   # noqa: E402
from deskmind_hands.drivers import peekaboo as pk   # noqa: E402
from deskmind_hands.drivers.base import Element, ExecResult, Observation   # noqa: E402
from deskmind_hands.drivers.mock import MockDriver   # noqa: E402
from deskmind_hands.geometry import ImageTransform, Rect, ScreenGeometry, Size   # noqa: E402
from deskmind_hands.live import live_task   # noqa: E402
from deskmind_hands.runtime import judge   # noqa: E402
import test_own_browser_window as obw   # noqa: E402
from test_own_browser_window import PID, SAFARI, FakeMac   # noqa: E402

#: Peekaboo's answer to the second 「百度一下」 click in run 5 (its MCP layer, Peekaboo 4.3.1 fabaadc56): a click it
#: could not confirm, not one it refused -- the driver reports it as dispatched and unverified (hands #51).
UNCONFIRMED = ("INDETERMINATE: The click mutation returned without valid canonical result semantics. Hint: Observe the "
           "intended target before retrying and update the runtime host.")
BAIDU = {"url": "https://www.baidu.com/s?wd=%E6%96%B0%E5%8A%A0%E5%9D%A1%E5%A4%A9%E6%B0%94", "title": "新加坡天气_百度搜索",
         "text": "新加坡 天气 29°C 雷阵雨", "browser": True}
MARKER = {"url": pk.marker_page("dm-c087a9a2d0a2"), "title": "dm-c087a9a2d0a2", "text": pk.MARKER_TEXT,
          "browser": True, "marker": True}
SEARCH = {"kind": "type_text", "text": "新加坡的天气", "binding": {"element_id": "syn:websearch"}}
CLICK = {"kind": "click", "binding": {"element_id": "baidu"}}
DONE = {"kind": "done"}


# -- the loop: a DONE in a browser always gets a verdict on the page, or ends unverified --------------------------

class Safari(MockDriver):
    """The mock desktop as a browser: page_info answers what PeekabooDriver.page_info answers for it."""

    app = SAFARI

    def __init__(self, page, refuse_second_click=True):
        super().__init__(render=False)
        self.page, self.clicks, self.refuse = page, 0, refuse_second_click

    def page_info(self):
        return self.page

    def observe(self):
        obs = super().observe()
        self.looks = getattr(self, "looks", 0) + 1
        obs.focused_app = f"Safari浏览器 ({self.looks})"     # each look a new page: no loop of identical looks
        return obs

    def execute(self, action):
        if action.kind is ActionKind.TYPE_TEXT:
            return ExecResult(True, f"searched {action.text!r} from the address bar (in the background)")
        if action.kind is ActionKind.CLICK:
            self.clicks += 1
            if self.clicks == 2 and self.refuse:
                return ExecResult(False, UNCONFIRMED)
            return ExecResult(True, indeterminate=True, detail="dispatched but the outcome could not be confirmed")
        return super().execute(action)


class Judging(ReplayAdapter):
    def __init__(self, actions, done=None, down=False):
        super().__init__(actions)
        self.done, self.down, self.asked = done, down, []

    def judge_choice(self, state, instructions, criteria):
        self.asked.append(state)
        if self.down:
            raise RuntimeError("judge timed out")
        return {"choice": self.done, "probabilities": {self.done: 0.9}}


class User:
    def __init__(self, reply="ok"):
        self.asked, self.reply = [], reply

    def respond(self, question, approval, options=()):
        self.asked.append(question)
        return self.reply, True, 0.0


def run(goal, actions, driver, adapter, user=None):
    from deskmind_bench.task import load_task
    from deskmind_hands.env.workspace import Workspace
    from deskmind_hands.runtime.loop import RunConfig, run_task
    repo = Path(__file__).resolve().parent.parent
    task = dataclasses.replace(load_task(repo / "tasks" / "smoke" / "S01-rename.yaml"), goal=goal)
    ws = Workspace.create(Path(tempfile.mkdtemp()), repo / "fixtures")
    with mock.patch.dict(os.environ, {"HANDS_DONE_CHECK": "1"}):
        return run_task(task, driver, adapter, ws, config=RunConfig(user=user or User(), approve_risky=True))


class DoneInABrowser(unittest.TestCase):
    GOAL = "打开 Safari，搜索新加坡的天气"

    def test_run5_an_unread_page_is_never_a_silent_completion(self):
        # Run 5's steps; the page of the run's window could not be read when it said DONE.
        unread = {"url": "", "title": "", "text": "", "browser": True, "unread": pk.PeekabooDriver.PAGE_UNREAD}
        acts = [SEARCH, CLICK, CLICK, DONE, DONE]
        res = run(self.GOAL, acts, Safari(unread), Judging(acts, done=judge.SHOWN))
        self.assertNotEqual(res.state.value, "completed")
        judged = [s for s in res.steps if s.get("kind") == "judged"]
        self.assertTrue(judged and judged[0]["about"] == "result_on_page" and judged[0].get("not_judged"), res.steps)
        self.assertTrue([s for s in res.steps if s.get("kind") == "done_blocked"])     # held back once, with why
        self.assertIn("not verified", res.failure.detail)

    def test_a_silent_judge_on_a_browser_page_is_no_pass(self):
        acts = [SEARCH, DONE, DONE]
        res = run(self.GOAL, acts, Safari(BAIDU), Judging(acts, down=True))
        self.assertNotEqual(res.state.value, "completed")
        self.assertIn("not verified", res.failure.detail)
        self.assertTrue([s for s in res.steps if s.get("kind") == "judged" and s.get("not_judged")])

    def test_a_results_page_judged_shown_completes_with_the_judgement(self):
        acts = [SEARCH, CLICK, CLICK, DONE]
        res = run(self.GOAL, acts, Safari(BAIDU), Judging(acts, done=judge.SHOWN))
        self.assertEqual(res.state.value, "completed")
        self.assertEqual([s["verdict"] for s in res.steps if s.get("kind") == "judged"], [judge.SHOWN])

    def test_the_marker_page_is_judged_and_said_as_the_runs_empty_window(self):
        acts = [DONE] * 4
        adapter = Judging(acts, done=judge.NOT_SHOWN)
        res = run(self.GOAL, acts, Safari(MARKER), adapter)
        self.assertNotEqual(res.state.value, "completed")
        self.assertEqual(res.failure.cls.name, "FALSE_COMPLETION")
        self.assertIn(judge.MARKER_URL, adapter.asked[0])
        self.assertNotIn("data:", adapter.asked[0])
        blocked = [s for s in res.steps if s.get("kind") == "done_blocked"]
        self.assertIn("no page opened in it yet", blocked[0]["text"])

    def test_the_typing_judge_is_told_the_marker_page_plainly(self):
        prompts = []
        judge.typing(lambda p, sy, o: prompts.append(p) or {"choice": judge.DO}, self.GOAL, "智能搜索栏", "新加坡的天气",
                     MARKER)
        self.assertIn(judge.MARKER_TITLE, prompts[0])
        self.assertNotIn("data:", prompts[0])
        self.assertNotIn("dm-c087", prompts[0])

    def test_an_adapter_that_cannot_judge_is_as_before(self):
        # No judge_choice at all: a missing capability, recorded in the run's checks, not the planner's failure.
        unread = {"url": "", "title": "", "text": "", "browser": True, "unread": "x"}
        res = run(self.GOAL, [DONE], Safari(unread), ReplayAdapter([DONE]))
        self.assertEqual(res.state.value, "completed")


class OnTheMac(unittest.TestCase):
    """The stand-in Mac of test_own_browser_window (its tests are not collected again here)."""
    mac = obw.OwnWindow.mac


class PageInfo(OnTheMac):
    """PeekabooDriver.page_info in run 5's setting: the user's only Safari window minimised, Finder in front."""

    def run5(self):
        mac = FakeMac([{"id": 19085, "title": "收藏夹", "on_screen": False, "frame": (0, 0, 1200, 800)}])
        d = self.mac(mac)
        d.observe()
        return mac, d

    def test_a_page_that_cannot_be_read_is_said_so(self):
        _, d = self.run5()
        calls = []
        with mock.patch.object(axchrome, "page_info", lambda pid, frame, wid=None: calls.append(wid)):
            page = d.page_info()
        self.assertTrue(page["browser"] and page["unread"], page)
        self.assertEqual(len(calls), 3)                                 # read again before it is called unread
        self.assertEqual(set(calls), {d._pinned_window})                # the run's window, never the user's

    def test_the_marker_page_and_a_real_page(self):
        mac, d = self.run5()
        own = mac.find(d._pinned_window)
        with mock.patch.object(axchrome, "page_info", lambda pid, frame, wid=None:
                               {"url": own["url"], "title": own["title"], "text": pk.MARKER_TEXT}):
            self.assertTrue(d.page_info().get("marker"))
        with mock.patch.object(axchrome, "page_info", lambda pid, frame, wid=None: dict(BAIDU, browser=None)):
            page = d.page_info()
        self.assertTrue(page["browser"])
        self.assertNotIn("marker", page)

    def test_another_app_is_as_before(self):
        _, d = self.run5()
        d.app = "com.apple.TextEdit"
        with mock.patch.object(axchrome, "page_info", lambda pid, frame, wid=None: None):
            self.assertIsNone(d.page_info())


# -- the run's own window, still on its marker page ----------------------------------------------------------------

class MarkerPage(OnTheMac):
    def test_the_marker_page_is_said_plainly(self):
        # Run 4's setting: the page it needed is in the user's window; the run's window is on its marker page.
        mac = FakeMac([{"id": 19116, "title": "零件表", "on_screen": True, "frame": (0, 0, 1200, 800)}])
        d = self.mac(mac)
        obs = d.observe()
        self.assertTrue(obs.own_window_empty)
        note = next((n for n in obs.notes if "DeskMind opened for this run" in n), "")
        self.assertIn("no web page is open in it yet", note)
        self.assertIn("other 1 window(s) are the user's", note)        # the user's 零件表 window
        self.assertIn("ask the user", note)
        # A page loaded in it: no longer said.
        own = mac.find(d._pinned_window)
        own["title"] = "零件表 - 测试站点"
        obs = d.observe()
        self.assertFalse(obs.own_window_empty)
        self.assertFalse([n for n in obs.notes if "DeskMind opened for this run" in n])

    def test_preexisting_windows_counts_the_users_real_windows(self):
        class Busy(FakeMac):
            def quartz(self):
                q = super().quartz()
                inner = q.CGWindowListCopyWindowInfo

                def windows(option, wid):
                    rows = inner(option, wid)
                    if option == 0:      # Safari's own surfaces, off the ordinary window layer
                        rows += [{"kCGWindowNumber": 900 + i, "kCGWindowOwnerPID": PID, "kCGWindowLayer": 25,
                                  "kCGWindowIsOnscreen": False, "kCGWindowBounds": {}} for i in range(5)]
                    return rows
                q.CGWindowListCopyWindowInfo = windows
                return q
        mac = Busy([{"id": 19116, "title": "零件表", "on_screen": True, "frame": (0, 0, 1200, 800)},
                    {"id": 19085, "title": "收藏夹", "on_screen": False, "frame": (0, 0, 1200, 800)},
                    {"id": 19001, "title": "", "on_screen": False, "frame": (1400, 0, 64, 64)}])     # a ghost
        d = self.mac(mac)
        self.assertEqual(d.state()["preexisting_windows"], 2)


# -- with nothing to work on, the planner may ask -----------------------------------------------------------------

def marker_look(empty=True):
    s = Size(1200, 800)
    note = pk.PeekabooDriver.OWN_WINDOW_EMPTY_NOTE.format(others="The browser's other 1 window(s) are the user's.")
    return Observation(id="obs-1", geometry=ScreenGeometry(s, s), transform=ImageTransform.identity(s),
                       elements=[Element(id="e6", role="statictext", label=pk.MARKER_TEXT, rect=Rect(400, 300, 200, 20),
                                         app="Safari浏览器"),
                                 Element(id="syn:websearch", role="textbox", label="在地址栏搜索（智能搜索栏）",
                                         settable=True, synthetic=True, app="Safari浏览器")],
                       focused_app="Safari浏览器", window_title="dm-c087a9a2d0a2",
                       notes=[note] if empty else [], own_window_empty=empty)


class Picks:
    """A planner that picks `op` whenever it is offered; records what was offered."""

    def __init__(self, op):
        self.op, self.offered, self.states = op, [], []

    def __call__(self, state, questions):
        ops = list(questions["operation"]["criteria"])
        self.offered.append(ops)
        self.states.append(state)
        op = self.op if self.op in ops else "BLOCKED"
        out = {"operation": {"type": "choice", "choice": op, "probabilities": {o: (1.0 if o == op else 0.0)
                                                                                 for o in ops}}}
        for qid, q in questions.items():
            if qid != "operation":
                keys = list(q["criteria"])
                out[qid] = {"type": "choice", "choice": keys[0], "probabilities": {k: 1 / len(keys) for k in keys}}
        return out


class Ask(unittest.TestCase):
    GOAL = "把网页上的零件表抄进 parts.txt"

    def propose(self, obs, planner):
        task = live_task(self.GOAL, Path(tempfile.mkdtemp()), app=SAFARI, max_actions=20, wall_clock_s=60)
        a = SystemOneAdapter(url="http://127.0.0.1:9", timeout=2)      # no text helper, as in the app
        a._ask = planner
        return a.propose(TurnContext(task=task, observation=obs, history=[], channels=frozenset({"ax"}))), a

    def test_ask_is_offered_on_the_empty_own_window_and_asks(self):
        planner = Picks("ASK")
        p, a = self.propose(marker_look(), planner)
        self.assertIn("ASK", planner.offered[0])
        self.assertIs(p.action.kind, ActionKind.ASK_USER)
        self.assertEqual(p.action.text, OWN_WINDOW_QUESTION["zh"])
        self.assertIn("搜索什么", p.action.text)          # fits a search goal as well as a page the user has open
        self.assertIn("DeskMind opened for this run", planner.states[0]["note"])      # the planner sees it

    def test_not_offered_otherwise(self):
        planner = Picks("ASK")
        self.propose(marker_look(empty=False), planner)
        self.assertNotIn("ASK", planner.offered[0])

    def test_the_question_reaches_the_user_in_a_run(self):
        a = SystemOneAdapter(url="http://127.0.0.1:9", timeout=2)
        a._ask = Picks("ASK")
        user = User()
        run_marker(self.GOAL, OnMarker(), a, user)
        self.assertEqual(user.asked[:1], [OWN_WINDOW_QUESTION["zh"]])

    def test_with_the_ax_channel_alone_the_empty_window_still_asks(self):
        # Codex review of 82681e5: _filter rebuilt the look for one channel and dropped own_window_empty and the notes.
        from deskmind_hands.runtime.loop import _filter
        for channels in ({"ax"}, {"screenshot"}):
            seen = _filter(marker_look(), frozenset(channels))
            self.assertTrue(seen.own_window_empty, channels)
            self.assertEqual(seen.notes, marker_look().notes)
            self.assertEqual(seen.window_title, "dm-c087a9a2d0a2")
        self.assertEqual(_filter(marker_look(), frozenset({"screenshot"})).elements, [])     # the channel withheld
        a = SystemOneAdapter(url="http://127.0.0.1:9", timeout=2)
        a._ask = planner = Picks("ASK")
        user = User()
        run_marker(self.GOAL, OnMarker(), a, user, channels=frozenset({"ax"}))
        self.assertIn("ASK", planner.offered[0])
        self.assertEqual(user.asked[:1], [OWN_WINDOW_QUESTION["zh"]])
        self.assertIn("DeskMind opened for this run", planner.states[0]["note"])

    def answered(self, reply, done_verdict, turns):
        """The empty-window ASK answered with `reply`, then the planner's `turns`. The judges answer as the app's Brain
        did on these cases (probe, 10-17): typing parts.txt into the address bar 不做, the empty window `done_verdict`."""
        driver = OnMarker()
        a = SystemOneAdapter(url="http://127.0.0.1:9", timeout=2)
        a._ask = planner = Script([("ASK", None, None)] + turns)
        a.judge_choice = lambda state, sy, options: {"choice": judge.DONT if "准备打的内容" in state else done_verdict}
        user = User(reply=reply)
        res = run_marker(self.GOAL, driver, a, user)
        return res, driver, planner, user

    def test_an_answer_is_not_acted_on_and_the_run_never_completes(self):
        # Option 1 (review of 61efec4): what the user answers is not turned into a value to type -- hands derives
        # values with code rules and the model only chooses. The planner typed parts.txt into the address bar after
        # an address was given (probe); the typing judge refuses that, and the empty window is not done.
        turns = [("TYPE_TEXT", "在地址栏搜索", "parts.txt")] + [("DONE", None, None)] * 4
        res, driver, planner, user = self.answered("https://example.com/parts", judge.NOT_SHOWN, turns)
        self.assertEqual(user.asked[:1], [OWN_WINDOW_QUESTION["zh"]])
        values = [v["value"] for v in planner.requests[1]["type_text_value"]["criteria"].values()]
        self.assertNotIn("https://example.com/parts", values)          # not acted on yet
        self.assertEqual(driver.typed, [])                              # parts.txt never reached the address bar
        self.assertTrue([s for s in res.steps if s.get("kind") == "judged" and s.get("verdict") == judge.DONT])
        self.assertNotEqual(res.state.value, "completed")
        self.assertEqual(res.failure.cls.name, "FALSE_COMPLETION")

    def test_an_answer_of_dont_know_ends_unverified_not_completed(self):
        # The Brain's done judge said 结果本来就不在网页上 of the empty window here (probe): with no step gone through,
        # that is not taken.
        turns = [("TYPE_TEXT", "在地址栏搜索", "parts.txt")] + [("DONE", None, None)] * 4
        res, driver, _, _ = self.answered("不知道，你看着办", judge.NOT_ON_A_PAGE, turns)
        self.assertEqual(driver.typed, [])
        self.assertNotEqual(res.state.value, "completed")
        self.assertIn("not verified", res.failure.detail)
        judged = [s for s in res.steps if s.get("kind") == "judged" and s.get("about") == "result_on_page"]
        self.assertTrue(judged and judged[0]["verdict"] == judge.NOT_ON_A_PAGE and judged[0].get("not_taken"))

    def test_a_stop_after_the_answer_keeps_the_reason(self):
        res, _, _, _ = self.answered("不知道", judge.NOT_SHOWN, [("BLOCKED", None, None)])
        self.assertEqual(res.state.value, "gave_up")
        stop = [s for s in res.steps if s.get("kind") == "give_up"]
        self.assertTrue(stop and any("DeskMind opened for this run" in n for n in stop[0]["notes"]))

    def test_no_other_copy_of_a_look_drops_it(self):
        # The sweep after _filter: the hybrid adapter's look without pixels, and a look replayed from a trace.
        from deskmind_hands.adapters.base import TurnContext as Ctx
        from deskmind_hands.adapters.hybrid import HybridAdapter
        from deskmind_hands.replay import observation
        task = live_task(self.GOAL, Path(tempfile.mkdtemp()), app=SAFARI, max_actions=20, wall_clock_s=60)
        blind = HybridAdapter._without_screenshot(Ctx(task=task, observation=marker_look(), history=[],
                                                      channels=frozenset({"ax"}))).observation
        self.assertTrue(blind.own_window_empty and blind.notes and blind.screenshot_png is None)
        self.assertTrue(observation({"id": "obs-0001", "own_window_empty": True}).own_window_empty)


class OnMarker(MockDriver):
    """The run's own Safari window on its marker page until something is searched from its address bar."""

    app = SAFARI

    def __init__(self):
        super().__init__(render=False)
        self.typed, self.allowed = [], []

    def observe(self):
        if not self.typed:
            # Each look a little different (the clock in the title bar, say): the loop's identical-looks stop is not
            # what these tests are about.
            self.looks = getattr(self, "looks", 0) + 1
            return dataclasses.replace(marker_look(), focused_app=f"Safari浏览器 {self.looks}")
        return dataclasses.replace(marker_look(empty=False), window_title="零件表", id=f"obs-{len(self.typed) + 1}")

    def browser_field(self, eid):
        return "智能搜索栏" if eid == "syn:websearch" else None

    def allow_browser_field(self, eid):
        self.allowed.append(eid)

    def page_info(self):
        if not self.typed:
            return MARKER
        return {"url": self.typed[-1], "title": "零件表", "text": "P-102 螺丝 200", "browser": True}

    def execute(self, action):
        if action.kind is ActionKind.TYPE_TEXT and action.binding.element_id == "syn:websearch":
            self.typed.append(action.text)
            return ExecResult(True, f"searched {action.text!r} from the address bar (in the background)")
        return ExecResult(True, "ok")


def run_marker(goal, driver, adapter, user, channels=None):
    from deskmind_bench.task import load_task
    from deskmind_hands.env.workspace import Workspace
    from deskmind_hands.runtime.loop import RunConfig, run_task
    repo = Path(__file__).resolve().parent.parent
    task = dataclasses.replace(load_task(repo / "tasks" / "smoke" / "S01-rename.yaml"), goal=goal)
    ws = Workspace.create(Path(tempfile.mkdtemp()), repo / "fixtures")
    cfg = RunConfig(user=user, approve_risky=True, **({"channels": channels} if channels else {}))
    with mock.patch.dict(os.environ, {"HANDS_DONE_CHECK": "1"}):
        return run_task(task, driver, adapter, ws, config=cfg)


class Script:
    """A planner answering from a script, one (operation, target's label, value) a turn; heads it was not given a
    pick for take their first option."""

    def __init__(self, turns):
        self.turns, self.requests = list(turns), []

    def __call__(self, state, questions):
        self.requests.append(questions)
        ops = list(questions["operation"]["criteria"])
        op, label, value = self.turns.pop(0) if self.turns else ("BLOCKED", None, None)
        assert op in ops, (op, ops)
        one = lambda pick, keys: {"type": "choice", "choice": pick,
                                  "probabilities": {k: (1.0 if k == pick else 0.0) for k in keys}}
        out = {"operation": one(op, ops)}
        for qid, q in questions.items():
            if qid == "operation":
                continue
            keys = list(q["criteria"])
            want = value if qid.endswith("_value") else label if qid.endswith("_target") else None
            pick = next((k for k, v in q["criteria"].items() if want and want in json.dumps(v, ensure_ascii=False)
                         and (not qid.endswith("_value") or v.get("value") == want)), keys[0])
            out[qid] = one(pick, keys)
        return out

if __name__ == "__main__":
    unittest.main()
