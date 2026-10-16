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
    return d


class Offered(unittest.TestCase):
    def setUp(self):
        self.p = mock.patch.object(axchrome, "fields_outside_page", lambda pid: [BAR])
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_offered_in_a_browser_window(self):
        d = driver("com.apple.Safari")
        out = d._project_browser(SAFARI)
        self.assertEqual([(e.id, e.label, e.settable, e.synthetic) for e in out], [(WEB_SEARCH, "在地址栏搜索", True, True)])
        self.assertEqual(d._address_bar, "e48")
        self.assertEqual(d.browser_field(WEB_SEARCH), "智能搜索栏")

    def test_not_in_textedit_or_finder(self):
        for app, els in (("com.apple.TextEdit", TEXTEDIT), ("com.apple.finder", FINDER)):
            d = driver(app)
            self.assertEqual(d._project_browser(els), [], app)
            self.assertIsNone(d.browser_field(WEB_SEARCH), app)

    def test_not_for_a_web_area_without_a_web_address(self):
        # a mail message is a web area too, with a search field beside it
        d = driver("com.apple.mail")
        d.page_info = lambda: {"url": "x-message://1", "title": "收件箱", "text": ""}
        self.assertEqual(d._project_browser(SAFARI), [])

    def test_not_when_no_address_bar_is_found(self):
        with mock.patch.object(axchrome, "fields_outside_page", lambda pid: []):
            self.assertEqual(driver("com.apple.Safari")._project_browser(SAFARI), [])


class Carried(unittest.TestCase):
    def act(self, d):
        return d._execute_projected(Action(kind=ActionKind.TYPE_TEXT, text="新加坡 天气",
                                           binding=Binding(observation_id="o", app="Safari", element_id=WEB_SEARCH)))

    def test_allowed_it_types_into_the_address_bar_and_submits(self):
        d = driver("com.apple.Safari")
        d._address_bar, d._projected_actions = "e48", 0
        typed = []
        d._execute_inner = lambda a: (typed.append((a.binding.element_id, a.text, d._browser_field_allowed))
                                      or ExecResult(True, "entered and pressed Return"))
        d.allow_browser_field(WEB_SEARCH)
        self.assertTrue(self.act(d).ok)
        # into the address bar itself, allowed for that one step: its route focuses it, replaces, and presses Return
        self.assertEqual(typed, [("e48", "新加坡 天气", "e48")])

    def test_not_allowed_it_is_refused(self):
        d = driver("com.apple.Safari")
        d._address_bar, d._projected_actions = "e48", 0
        d._execute_inner = mock.Mock()
        res = self.act(d)
        self.assertFalse(res.ok)
        d._execute_inner.assert_not_called()


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
