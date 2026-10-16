"""axchrome by structure, on a fake accessibility tree: an address bar known by its identifier is the browser's own
field even in a window with no page (a start page), and page_info reads the window the run observes, not the app's
front one (the live run judged the user's own Safari window)."""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.drivers import axchrome   # noqa: E402


def node(role, frame=None, children=(), **attrs):
    return {"AXRole": role, "frame": frame, "AXChildren": list(children), **attrs}


START = node("AXWindow", (0, 0, 800, 600), [node("AXToolbar", children=[
    node("AXTextField", (275, 18, 237, 31), AXIdentifier="WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD"),
    node("AXTextField", (600, 18, 120, 31))])], AXTitle="起始页")
PAGE = node("AXWindow", (900, 0, 800, 600), [
    node("AXToolbar", children=[node("AXTextField", (1175, 18, 237, 31), AXIdentifier="WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD")]),
    node("AXWebArea", (900, 52, 800, 548), [node("AXStaticText", AXValue="新加坡 28°C 多云")],
         AXURL="https://www.bing.com/search?q=x")], AXTitle="新加坡 天气 - 搜索")
APP = {"AXWindows": [START, PAGE]}


def fake():
    return [mock.patch.object(axchrome, "_get", lambda el, attr: el.get(attr)),
            mock.patch.object(axchrome, "_frame", lambda el: el.get("frame")),
            mock.patch.dict(sys.modules, {"ApplicationServices": types.SimpleNamespace(
                AXUIElementCreateApplication=lambda pid: APP)})]


class Fields(unittest.TestCase):
    def setUp(self):
        self.ps = fake()
        for p in self.ps:
            p.start()

    def tearDown(self):
        for p in self.ps:
            p.stop()

    def test_a_known_address_bar_counts_without_a_page(self):
        own = axchrome.own_fields(1)
        self.assertIn(((275, 18, 237, 31), True), own)
        self.assertNotIn(((600, 18, 120, 31), False), own)        # an unknown toolbar field of a pageless window
        self.assertIn((275, 18, 237, 31), axchrome.fields_outside_page(1))

    def test_page_info_reads_the_observed_window(self):
        self.assertIsNone(axchrome.page_info(1, (0, 0, 800, 600)))          # the start page has no page
        info = axchrome.page_info(1, (900, 0, 800, 600))
        self.assertEqual(info["url"], "https://www.bing.com/search?q=x")
        self.assertIn("28°C", info["text"])
        self.assertIsNone(axchrome.page_info(1, None))
        self.assertIsNone(axchrome.page_info(1, (5, 5, 10, 10)))            # no window there: nothing, not another


if __name__ == "__main__":
    unittest.main()
