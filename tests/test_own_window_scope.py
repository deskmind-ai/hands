"""The run's own browser window, from the 0.4.3-rc.3 live test (10-17, hands 11714c0). Nothing here touches the Mac.

Run 5, 「打开 Safari，搜索新加坡的天气」 with the user's Safari windows minimised and Finder in front: a search, a click,
a click Peekaboo refused, and DONE -- completed with no judgement of the page at all. A page the driver could not read
(or one the judge gave no answer on) passed for "no web page", and a DONE there was taken as it was.

Run 4, 「把网页上的零件表抄进 parts.txt」 with the parts page in the user's window: the run's window stayed on its marker
page, and in 68 s the run clicked 「显示边栏」 and double-clicked the sidebar's 「默认，含1个标签页的标签页组」, 「边栏」 and
「单元格」 -- the user's tab groups -- twelve clicks, until it ended as a loop. It never asked.

And `preexisting_windows` said 9 to 15 while Safari had 2 to 6 windows.
"""
from __future__ import annotations

import dataclasses
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from deskmind_hands.actions import Action, ActionKind, Binding   # noqa: E402
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

#: Peekaboo's answer to the second 「百度一下」 click in run 5 (its MCP layer, Peekaboo 4.3.1 fabaadc56).
REFUSED = ("INDETERMINATE: The click mutation returned without valid canonical result semantics. Hint: Observe the "
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
                return ExecResult(False, REFUSED)
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
    def __init__(self):
        self.asked = []

    def respond(self, question, approval, options=()):
        self.asked.append(question)
        return "ok", True, 0.0


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


# -- the browser's own controls are never offered -----------------------------------------------------------------

def node(role, frame=None, children=(), **attrs):
    return {"AXRole": role, "frame": frame, "AXChildren": list(children), **attrs}


def safari_window(x, y, w, h, sheet=False, scrolled=0, menu=None):
    """Safari's window as accessibility shows it, sidebar open: toolbar, sidebar (the user's tab groups), the page in
    its scroll area. `scrolled`: how far the page is scrolled down -- WebKit's web area frame is the whole document, so
    it then starts above the window. `menu`: a menu the toolbar's tab group picker opened, at that frame."""
    toolbar = node("AXToolbar", (x, y, w, 50), [
        node("AXButton", (x + 10, y + 10, 30, 24), AXDescription="显示边栏"),
        node("AXButton", (x + 50, y + 10, 30, 24), AXDescription="后退"),
        node("AXTextField", (x + 300, y + 10, 300, 24), AXIdentifier="WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD")])
    sidebar = node("AXGroup", (x, y + 50, 250, h - 50), [node("AXScrollArea", (x, y + 50, 250, h - 50), [
        node("AXOutline", (x, y + 50, 250, h - 50), [node("AXRow", (x + 5, y + 60, 240, 24), [
            node("AXCell", (x + 5, y + 60, 240, 24))])], AXDescription="边栏")])])
    doc = node("AXWebArea", (x + 250, y + 50 - scrolled, w - 250, h - 50 + 3 * scrolled),
               [node("AXStaticText", AXValue=pk.MARKER_TEXT)])
    page = node("AXTabGroup", (x + 250, y + 50, w - 250, h - 50), [node("AXGroup", (x + 250, y + 50, w - 250, h - 50), [
        node("AXScrollArea", (x + 250, y + 50, w - 250, h - 50), [doc])])])
    if menu:
        toolbar["AXChildren"].append(node("AXMenuButton", (x + 90, y + 10, 30, 24), [node("AXMenu", menu)]))
    kids = [node("AXSplitGroup", (x, y + 50, w, h - 50), [sidebar, node("AXSplitter"), page]), toolbar]
    if sheet:
        kids.append(node("AXSheet", (x + 100, y + 50, 400, 200), [node("AXButton", (x + 120, y + 200, 60, 24))]))
    return node("AXWindow", (x, y, w, h), kids, AXTitle="dm-c087a9a2d0a2")


def fake_ax(windows):
    app = {"AXWindows": windows}
    return [mock.patch.object(axchrome, "_get", lambda el, attr: el.get(attr)),
            mock.patch.object(axchrome, "_frame", lambda el: el.get("frame")),
            mock.patch.object(axchrome, "_window_id", lambda w: None),
            mock.patch.dict(sys.modules, {"ApplicationServices": types.SimpleNamespace(
                AXUIElementCreateApplication=lambda pid: app)})]


class Regions(unittest.TestCase):
    def setUp(self):
        for p in fake_ax([safari_window(0, 0, 1200, 800, sheet=True)]):
            p.start()
            self.addCleanup(p.stop)

    def test_what_is_the_browsers_own(self):
        r = axchrome.regions(PID, (0, 0, 1200, 800))
        self.assertEqual(r["page"], [(250, 50, 950, 750)])
        chrome = lambda *rect: axchrome.chrome(r, rect)
        self.assertTrue(chrome(10, 10, 30, 24))         # 「显示边栏」
        self.assertTrue(chrome(50, 10, 30, 24))         # back: the toolbar is the browser's; the address bar search goes on
        self.assertTrue(chrome(5, 400, 240, 24))        # a row of the sidebar: the user's tab groups
        self.assertTrue(chrome(0, 300, 250, 500))       # the sidebar, below the sheet, up to the page's edge
        self.assertFalse(chrome(300, 10, 300, 24))      # the address bar (offered as 「在地址栏搜索」)
        self.assertFalse(chrome(400, 300, 200, 20))     # the page
        self.assertFalse(chrome(400, 30, 200, 40))      # a page row scrolled half under the toolbar: still the page's
        self.assertFalse(chrome(120, 200, 60, 24))      # a sheet over the sidebar: its buttons are the next step
        self.assertFalse(chrome(1500, 400, 50, 20))     # outside the window (a menu the page opened)

    def test_a_structure_not_understood_is_left_alone(self):
        # Chrome with its tree off: no web area, no address bar known by its identifier -- nothing is told apart.
        r = {"window": (0, 0, 1200, 800), "page": [], "overlay": [], "address_bar": []}
        d = pk.PeekabooDriver.__new__(pk.PeekabooDriver)
        d.app, d._pid_of, d._window_frame, d._observed_id = "com.google.Chrome", (lambda b, w=None: PID), \
            (lambda: (0, 0, 1200, 800)), (lambda: None)
        els = [Element(id="e1", role="link", label="零件表", rect=Rect(400, 300, 100, 20))]
        with mock.patch.object(axchrome, "regions", lambda pid, frame, wid=None: r):
            self.assertEqual(d._browser_scope(els, pk.Point(0, 0)), els)
        start_page = {**r, "address_bar": [(300, 10, 300, 24)]}
        with mock.patch.object(axchrome, "regions", lambda pid, frame, wid=None: start_page):
            self.assertEqual(d._browser_scope(els, pk.Point(0, 0)), [])     # a start page: only the address bar

    def test_a_scrolled_page_does_not_bring_the_toolbar_back(self):
        # Review of de228eb: scrolled, the web area's frame starts above the window and covers the toolbar.
        for p in fake_ax([safari_window(0, 0, 1200, 800, scrolled=2000)]):
            p.start()
            self.addCleanup(p.stop)
        r = axchrome.regions(PID, (0, 0, 1200, 800))
        self.assertEqual(r["page"], [(250, 50, 950, 750)])            # the viewport, not the document
        self.assertTrue(axchrome.chrome(r, (1100, 10, 30, 24)))         # a toolbar button above the page's column
        self.assertFalse(axchrome.chrome(r, (400, 300, 200, 20)))       # the page

    def test_a_web_area_with_no_scroll_area_is_clipped_to_the_window(self):
        win = node("AXWindow", (0, 0, 1200, 800), [node("AXWebArea", (0, -3000, 1200, 9000))])
        for p in fake_ax([win]):
            p.start()
            self.addCleanup(p.stop)
        self.assertEqual(axchrome.regions(PID, (0, 0, 1200, 800))["page"], [(0, 0, 1200, 800)])

    def test_a_menu_the_toolbar_opened_over_the_page_is_chrome(self):
        for p in fake_ax([safari_window(0, 0, 1200, 800, menu=(90, 34, 300, 400))]):
            p.start()
            self.addCleanup(p.stop)
        r = axchrome.regions(PID, (0, 0, 1200, 800))
        self.assertTrue(axchrome.chrome(r, (95, 300, 280, 22)))        # a tab group listed in the picker's menu
        self.assertFalse(axchrome.chrome(r, (600, 300, 200, 20)))       # the page beside it

    def test_an_edge_that_only_touches_the_page_is_not_on_it(self):
        r = axchrome.regions(PID, (0, 0, 1200, 800))
        self.assertTrue(axchrome.chrome(r, (700, 30, 30, 22)))          # a toolbar button 2 px over the page's top

    def test_no_single_window_no_regions(self):
        self.assertIsNone(axchrome.regions(PID, (5, 5, 10, 10)))
        self.assertIsNone(axchrome.regions(PID, None))


class SafariWithSidebar(FakeMac):
    """Run 4's window: the run's own, on its marker page, the sidebar open with the user's tab groups."""

    def peekaboo(self, key, _extra=None, **kw):
        ok, data, detail = super().peekaboo(key, _extra, **kw)
        if ok and key in ("see", "see_window"):
            x, y = data["coordinate_context"]["logical_bounds"][0]
            el = lambda i, role, label, bx, by, bw, bh: {"id": i, "role": role, "ax_role": role, "label": label,
                                                         "bounds": {"x": x + bx, "y": y + by, "width": bw, "height": bh}}
            data = {**data, "ui_elements": [
                el("e1", "AXButton", "显示边栏", 10, 10, 30, 24),
                el("e2", "AXTextField", "智能搜索栏", 300, 10, 300, 24),
                el("e3", "AXOutline", "边栏", 0, 50, 250, 650),
                el("e4", "AXRow", "默认，含1个标签页的标签页组", 5, 60, 240, 24),
                el("e5", "AXCell", "单元格", 5, 60, 240, 24),
                el("e6", "AXStaticText", pk.MARKER_TEXT, 400, 300, 200, 20)]}
        return ok, data, detail


class Scope(OnTheMac):
    def run4(self):
        mac = SafariWithSidebar([{"id": 19116, "title": "零件表", "on_screen": True, "frame": (0, 0, 1200, 800)}])
        d = self.mac(mac)
        del d._element_cache                       # the real one: what the planner is offered
        d._attach_popup_options = lambda els, origin: None
        x, y, w, h = mac.find(d._pinned_window)["frame"]
        for p in fake_ax([safari_window(x, y, w, h)]):
            p.start()
            self.addCleanup(p.stop)
        return mac, d

    def test_run4_the_sidebar_and_tab_groups_are_not_offered(self):
        _, d = self.run4()
        obs = d.observe()
        labels = [e.label for e in obs.elements]
        for chrome in ("显示边栏", "边栏", "默认，含1个标签页的标签页组", "单元格"):
            self.assertNotIn(chrome, labels)
        self.assertIn(pk.MARKER_TEXT, labels)           # the page
        self.assertIn("智能搜索栏", labels)                # the address bar (projected as the search with Peekaboo's MCP)

    def test_a_step_on_one_of_them_is_refused(self):
        _, d = self.run4()
        d.observe()
        for kind in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK):
            res = d._execute_inner(Action(kind=kind, binding=Binding(observation_id="o", app="Safari", element_id="e4")))
            self.assertFalse(res.ok)
            self.assertIn("browser's own controls", res.detail)

    def test_the_marker_page_is_said_plainly(self):
        mac, d = self.run4()
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
        class Busy(SafariWithSidebar):
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
        class Marker(MockDriver):
            app = SAFARI

            def observe(self):
                return marker_look()

            def execute(self, action):
                return ExecResult(True, "ok")
        from deskmind_bench.task import load_task
        from deskmind_hands.env.workspace import Workspace
        from deskmind_hands.runtime.loop import RunConfig, run_task
        repo = Path(__file__).resolve().parent.parent
        task = dataclasses.replace(load_task(repo / "tasks" / "smoke" / "S01-rename.yaml"), goal=self.GOAL)
        ws = Workspace.create(Path(tempfile.mkdtemp()), repo / "fixtures")
        a = SystemOneAdapter(url="http://127.0.0.1:9", timeout=2)
        a._ask = Picks("ASK")
        user = User()
        run_task(task, Marker(render=False), a, ws, config=RunConfig(user=user, approve_risky=True))
        self.assertEqual(user.asked[:1], [OWN_WINDOW_QUESTION["zh"]])


if __name__ == "__main__":
    unittest.main()
