"""「在地址栏搜索」: a browser window's address bar projected as one plain option (peekaboo._project_browser), offered
whenever a web page is in front -- no word of the goal decides it. On a page with its own search box the planner never
chose the address bar (deskmind#73). Typing through it is judged like typing into the address bar itself."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.actions import Action, ActionKind, Binding   # noqa: E402
from deskmind_hands.drivers import axchrome   # noqa: E402
from deskmind_hands.drivers.base import Element, ExecResult   # noqa: E402
from deskmind_hands.drivers.peekaboo import WEB_SEARCH, PeekabooDriver   # noqa: E402
from deskmind_hands.geometry import Point, Rect   # noqa: E402
from deskmind_hands.runtime import judge   # noqa: E402

BAR = (275.0, 18.0, 237.0, 31.0)            # Safari's 智能搜索栏, on screen
WIN = (0, 0, 800, 600)                        # the window the run observes


def el(eid, role, ax, label, rect=None):
    return Element(id=eid, role=role, ax_role=ax, label=label, rect=Rect(*rect) if rect else None)


SAFARI = [el("e6", "webArea", "AXWebArea", "控制台", (0, 52, 728, 1032)),
          el("e19", "textField", "AXTextField", "搜索", (378, 70, 214, 32)),     # the page's own box
          el("e48", "textField", "AXTextField", "智能搜索栏", BAR)]
TEXTEDIT = [el("t1", "textArea", "AXTextArea", "", (0, 30, 600, 500))]
FINDER = [el("f1", "textField", "AXTextField", "报销单.csv", (10, 40, 200, 20))]


def driver(app):
    d = PeekabooDriver.__new__(PeekabooDriver)
    d.app, d._origin, d._app_name, d._window_id = app, Point(0, 0), "Safari浏览器", "1"
    d._pid_of = lambda bundle, all_wins=None: 123
    d._labels_by_id = {"e48": "智能搜索栏"}
    d.page_info = lambda: {"url": "http://127.0.0.1:8765/", "title": "控制台", "text": ""}
    d._window_frame = lambda: (0, 0, 800, 600)
    return d


class Offered(unittest.TestCase):
    def setUp(self):
        self.p = mock.patch.object(axchrome, "own_fields", lambda pid, frame=None: [(BAR, False)])   # outside the page
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_offered_in_a_browser_window(self):
        d = driver("com.apple.Safari")
        out = d._project_browser(SAFARI)
        self.assertEqual([(e.id, e.label, e.settable, e.synthetic) for e in out], [(WEB_SEARCH, "在地址栏搜索（智能搜索栏）", True, True)])
        self.assertEqual(d._address_bar, "e48")
        self.assertEqual(d.browser_field(WEB_SEARCH), "智能搜索栏")
        d._rects = {}                                   # the raw bar, out of the options, is still the browser's own
        self.assertEqual(d.browser_field("e48"), "智能搜索栏")
        self.assertIsNotNone(d._browser_field_refusal("e48"))

    def test_a_known_address_bar_without_a_page(self):
        # A start page or a blank tab has no web area; its address bar, known by its identifier, is still one (the live
        # run typed into the user's start page unjudged).
        with mock.patch.object(axchrome, "own_fields", lambda pid, frame=None: [(BAR, True)]):
            d = driver("com.apple.Safari")
            d.page_info = lambda: None
            out = d._project_browser([e for e in SAFARI if e.ax_role != "AXWebArea"])
        self.assertEqual([e.id for e in out], [WEB_SEARCH])

    def test_the_known_address_bar_before_a_find_bar(self):
        # A find bar is a browser-own field too; the address bar known by its identifier is the one offered.
        find = el("e70", "textField", "AXTextField", "在页面上查找", (100, 60, 300, 24))
        with mock.patch.object(axchrome, "own_fields", lambda pid, frame=None: [((100, 60, 300, 24), False), (BAR, True)]):
            d = driver("com.apple.Safari")
            d._project_browser([SAFARI[0], find, SAFARI[2]])
        self.assertEqual(d._address_bar, "e48")

    def test_only_the_observed_windows_fields(self):
        seen = []
        with mock.patch.object(axchrome, "own_fields", lambda pid, frame=None: seen.append(frame) or [(BAR, True)]):
            driver("com.apple.Safari")._project_browser(SAFARI)
        self.assertEqual(seen, [(0, 0, 800, 600)])

    def test_not_in_textedit_or_finder(self):
        for app, els in (("com.apple.TextEdit", TEXTEDIT), ("com.apple.finder", FINDER)):
            d = driver(app)
            d.page_info = lambda: None
            self.assertEqual(d._project_browser(els), [], app)
            self.assertIsNone(d.browser_field(WEB_SEARCH), app)

    def test_not_for_a_web_area_without_a_web_address(self):
        # a mail message is a web area too, with a search field beside it
        d = driver("com.apple.mail")
        d.page_info = lambda: {"url": "x-message://1", "title": "收件箱", "text": ""}
        self.assertEqual(d._project_browser(SAFARI), [])

    def test_not_when_no_address_bar_is_found(self):
        with mock.patch.object(axchrome, "own_fields", lambda pid, frame=None: []):
            self.assertEqual(driver("com.apple.Safari")._project_browser(SAFARI), [])


class Carried(unittest.TestCase):
    """Only in the background, into the observed window, once the focus is confirmed on the address bar (review)."""

    def act(self, d):
        return d._execute_projected(Action(kind=ActionKind.TYPE_TEXT, text="新加坡 天气",
                                           binding=Binding(observation_id="o", app="Safari", element_id=WEB_SEARCH)))

    def searchable(self, background=True, window=True, focused="WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD", frame=None):
        frame = WIN if frame is None else frame
        from deskmind_hands.drivers import skylight
        d = driver("com.apple.Safari")
        d._address_bar, d._address_bar_rect, d._projected_actions = "e48", Rect(*BAR), 0
        d._background_input = lambda bundle: background
        d._moved_since_seen = lambda: None
        d._wait_until_still = lambda bundle, what="": None
        d._background_quartz = lambda bundle, observed_only=False: (("Q", "token") if window and observed_only else None)
        d._flash = mock.Mock(side_effect=AssertionError("never the foreground route"))
        d._settle = lambda ok, detail: ExecResult(ok, detail)
        d.events = []
        d._post_click = lambda q, x, y: d.events.append(("click", x, y))

        def paste_and_submit(q, text, before_return=None):
            d.events.append(("paste", text))
            if before_return is not None and not before_return():
                return False
            d.events.append(("return",))
            return True

        d._paste_and_submit = paste_and_submit
        d._pid_of = lambda bundle: 7
        d._window_frame = lambda: frame
        seq = iter(focused if isinstance(focused, list) else [focused] * 3)
        self.patches = [mock.patch.object(axchrome, "focused_identifier",
                                          lambda pid, fr: next(seq) if fr == WIN else "another window"),
                        mock.patch.object(skylight, "give_focus_back", lambda token: d.events.append(("focus back",)))]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        return d

    def test_allowed_it_clicks_checks_focus_pastes_and_submits(self):
        d = self.searchable()
        d.allow_browser_field(WEB_SEARCH)
        res = self.act(d)
        self.assertTrue(res.ok, res.detail)
        self.assertEqual([e[0] for e in d.events], ["click", "paste", "return", "focus back"])
        self.assertEqual(d.events[1], ("paste", "新加坡 天气"))

    def test_not_allowed_it_is_refused(self):
        d = self.searchable()
        self.assertFalse(self.act(d).ok)
        self.assertEqual(d.events, [])

    def test_no_background_route_no_search(self):
        d = self.searchable(background=False)
        d.allow_browser_field(WEB_SEARCH)
        res = self.act(d)
        self.assertFalse(res.ok)
        self.assertIn("background", res.detail)
        self.assertEqual(d.events, [])

    def test_the_observed_window_or_nothing(self):
        d = self.searchable(window=False)
        d.allow_browser_field(WEB_SEARCH)
        res = self.act(d)
        self.assertFalse(res.ok)
        self.assertIn("observes", res.detail)
        self.assertEqual(d.events, [])

    def test_focus_elsewhere_nothing_is_pasted(self):
        d = self.searchable(focused="WEB_FIND_FIELD")
        d.allow_browser_field(WEB_SEARCH)
        res = self.act(d)
        self.assertFalse(res.ok)
        self.assertIn("focus", res.detail)
        self.assertEqual([e[0] for e in d.events], ["click", "focus back"])

    def test_an_address_bar_of_another_window_is_not_this_one(self):
        # Every window's address bar has the same identifier: focus left in the user's own window must not pass.
        d = self.searchable(frame=(5, 5, 10, 10))
        d.allow_browser_field(WEB_SEARCH)
        res = self.act(d)
        self.assertFalse(res.ok)
        self.assertIn("another window", res.detail)
        self.assertEqual([e[0] for e in d.events], ["click", "focus back"])

    def test_no_window_frame_no_paste(self):
        d = self.searchable()
        d._window_frame = lambda: None
        d.allow_browser_field(WEB_SEARCH)
        self.assertFalse(self.act(d).ok)
        self.assertEqual([e[0] for e in d.events], ["click", "focus back"])

    def test_focus_lost_after_the_paste_no_return(self):
        d = self.searchable(focused=["WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD", "WEB_FIND_FIELD"])
        d.allow_browser_field(WEB_SEARCH)
        res = self.act(d)
        self.assertFalse(res.ok)
        self.assertIn("Return was not pressed", res.detail)
        self.assertEqual([e[0] for e in d.events], ["click", "paste", "focus back"])

    def test_no_address_bar_on_screen(self):
        d = self.searchable()
        d._address_bar_rect = None
        d.allow_browser_field(WEB_SEARCH)
        self.assertFalse(self.act(d).ok)


class Values(unittest.TestCase):
    def test_a_field_named_in_the_goal_does_not_cut_the_query(self):
        # 「在智能搜索栏里搜索新加坡的天气」 offered only 「栏里搜索新加坡的天气」 and its pieces (live 10-16).
        from deskmind_hands.adapters.systemone import value_candidates
        got = value_candidates("打开 Safari，在智能搜索栏里搜索新加坡的天气")
        self.assertEqual(got[0], "新加坡的天气")
        self.assertIn("新加坡 天气", got)
        self.assertEqual(value_candidates("搜索 林夏的 纸船，并播放")[:2], ["林夏 纸船", "林夏的 纸船"])
        # A verb inside the query is part of it (review of b23238d): the whole query stays first.
        for goal, first in (("Search for Finding Nemo and play it", "Finding Nemo"),
                            ("search for pathfinder reviews", "pathfinder reviews"),
                            ("搜索查找替换的用法", "查找替换的用法"),
                            ("查找 搜索引擎 的历史", "搜索引擎 的历史")):
            self.assertEqual(value_candidates(goal)[0], first, goal)


class Windows(unittest.TestCase):
    def test_a_minimised_browser_window_is_not_offered(self):
        # The parts run switched to the user's own minimised Safari window and read its page (live 10-16).
        d = driver("com.apple.Safari")
        d._mcp, d._pinned_window, d._window_id, d._obs_n = None, "2", "2", 0
        d._run = lambda op, **kw: (True, {"windows": [
            {"window_id": 1, "window_title": "新加坡 天气_百度搜索", "is_on_screen": False},
            {"window_id": 2, "window_title": "零件清单", "is_on_screen": True},
            {"window_id": 3, "window_title": "另一个", "is_on_screen": True}]}, "")
        self.assertEqual([w.id for w in d._windows()], ["2", "3"])
        d._pinned_window = d._window_id = "1"           # the observed window itself stays, on screen or not
        self.assertIn("1", [w.id for w in d._windows()])

    def test_nor_from_another_app(self):
        # Observing TextEdit, the staged Safari's windows are listed too: a minimised one is not (review of b23238d).
        d = driver("com.apple.TextEdit")
        d._mcp, d._pinned_window, d._window_id, d._obs_n = object(), "9", "9", 0
        d._stage_apps, d._home_app, d._window_app = ["Safari"], "com.apple.TextEdit", {}
        d._run = lambda op, app=None, **kw: (True, {"windows": [] if app == "com.apple.TextEdit" else [
            {"window_id": 1, "window_title": "新加坡 天气_百度搜索", "is_on_screen": False},
            {"window_id": 2, "window_title": "零件清单", "is_on_screen": True}]}, "")
        self.assertEqual([w.id for w in d._windows()], ["2"])


class Judged(unittest.TestCase):
    """In the loop, choosing it is judged first (runtime/judge.py): 照做 types, 不做 refuses."""

    def run_with(self, verdict):
        from test_judge import Browser, Judged as JudgedAdapter, run
        pick = {"kind": "type_text", "text": "新加坡 天气", "binding": {"element_id": WEB_SEARCH}}

        class WithSearch(Browser):
            def browser_field(self, eid):
                return "智能搜索栏" if eid == WEB_SEARCH else None

            def execute(self, action):
                if action.binding.element_id == WEB_SEARCH:
                    self.typed.append(action.text)
                    return ExecResult(True, "searched")
                return super().execute(action)

        with mock.patch("test_judge.Browser", WithSearch):
            return run("打开 Safari，搜索新加坡的天气", [pick, {"kind": "done"}],
                       JudgedAdapter([pick, {"kind": "done"}], typing=verdict))

    def test_judged_then_carried_out(self):
        res, driver_, adapter = self.run_with(judge.DO)
        self.assertEqual(driver_.typed, ["新加坡 天气"])
        self.assertEqual(driver_.allowed, [WEB_SEARCH])
        self.assertIn("新加坡 天气", adapter.asked[0])

    def test_refused_when_judged_not(self):
        res, driver_, _ = self.run_with(judge.DONT)
        self.assertEqual(driver_.typed, [])
        self.assertTrue(any(s.get("kind") == "judged" and s.get("verdict") == judge.DONT for s in res.steps))


if __name__ == "__main__":
    unittest.main()


class ObservedWindowOnly(unittest.TestCase):
    """_background_quartz(observed_only=True): the observed window or none -- never the app's largest window."""

    def quartz(self, observed):
        import types
        from deskmind_hands.drivers import skylight
        wins = [{"kCGWindowOwnerPID": 123, "kCGWindowLayer": 0, "kCGWindowNumber": 7,
                 "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1600, "Height": 1000}},     # the user's big window
                {"kCGWindowOwnerPID": 123, "kCGWindowLayer": 0, "kCGWindowNumber": 9,
                 "kCGWindowBounds": {"X": 900, "Y": 0, "Width": 800, "Height": 600}}]
        fake_q = types.SimpleNamespace(CGWindowListCopyWindowInfo=lambda opt, wid: wins, kCGWindowListOptionAll=0)
        d = driver("com.apple.Safari")
        d._pinned_window, d._window_id = None, observed
        with mock.patch.dict(sys.modules, {"Quartz": fake_q, "AppKit": types.SimpleNamespace(NSRunningApplication=None)}), \
                mock.patch.object(skylight, "focus_without_raise", lambda pid, wid: ("token", wid)), \
                mock.patch.object(skylight, "BackgroundQuartz", lambda pid, wid, origin: ("Q", wid)):
            return d._background_quartz("com.apple.Safari", observed_only=True), \
                d._background_quartz("com.apple.Safari")

    def test_the_observed_window(self):
        strict, _ = self.quartz("9")
        self.assertEqual(strict[0], ("Q", 9))

    def test_not_the_largest_when_the_observed_one_is_gone(self):
        strict, loose = self.quartz("42")
        self.assertIsNone(strict)
        self.assertEqual(loose[0], ("Q", 7))          # what the other routes still do
