"""Vision mode's text blocks and generic controls (hands/vision.py). Pure python: OCR boxes are given, no screen."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.adapters.systemone import generic_wanted
from deskmind_hands.vision import text_blocks

W, H = 1000, 800


def box(text, x, y, w, h):
    return {"text": text, "x": x / W, "y": y / H, "w": w / W, "h": h / H, "conf": 1}


def labels(rows):
    return [" · ".join(" ".join(r["text"] for r in line) for line in b) for b in text_blocks(rows, W, H)]


class TextBlocksTest(unittest.TestCase):
    def test_a_result_card_is_one_item(self):
        rows = [box("Song A (Live)", 300, 100, 110, 17), box("VIP", 302, 125, 20, 12), box("MV", 326, 125, 18, 12),
                box("Artist One", 350, 125, 60, 12), box("Song A", 600, 100, 60, 17), box("Artist Two", 600, 122, 60, 15)]
        self.assertEqual(labels(rows), ["Song A (Live) · VIP MV Artist One", "Song A · Artist Two"])

    def test_heading_tabs_and_menu_stay_single(self):
        rows = [box("Results", 240, 90, 100, 24), box("All", 240, 140, 36, 20), box("Songs", 296, 140, 36, 20),
                box("Albums", 352, 140, 40, 20), box("Home", 40, 90, 40, 18), box("Browse", 40, 130, 50, 18)]
        self.assertEqual(sorted(labels(rows)), sorted(["Results", "All", "Songs", "Albums", "Home", "Browse"]))

    def test_a_larger_line_below_is_not_a_detail(self):
        rows = [box("small caption", 100, 100, 90, 12), box("Big Title", 100, 115, 120, 26)]
        self.assertEqual(len(text_blocks(rows, W, H)), 2)

    def test_menu_entries_with_icons_stay_single(self):
        # OCR boxes that take in an entry's icon: taller, and starting further left than the next entry's text.
        rows = [box("• Recent", 38, 410, 164, 42), box("Shows", 78, 478, 70, 36), box("① Downloaded", 35, 598, 143, 48),
                box("① Latest", 32, 660, 170, 48)]
        self.assertEqual(len(text_blocks(rows, W, H)), 4)

    def test_side_by_side_columns_do_not_merge(self):
        rows = [box("Menu item", 40, 400, 60, 17), box("Card title", 245, 400, 120, 17),
                box("card detail", 245, 420, 100, 15)]
        self.assertEqual(sorted(labels(rows)), sorted(["Menu item", "Card title · card detail"]))


class GenericControlsTest(unittest.TestCase):
    def test_offered_by_goal(self):
        play = "Open the music app, search 林夏 纸船 and play it"
        self.assertFalse(generic_wanted("播放按钮", play))       # a song is played by opening it
        self.assertTrue(generic_wanted("播放按钮", "继续播放刚才的歌"))
        self.assertFalse(generic_wanted("发送按钮", play))
        self.assertFalse(generic_wanted("分享按钮", play))
        self.assertTrue(generic_wanted("返回按钮", play))           # no condition: always
        self.assertTrue(generic_wanted("发送按钮", "在聊天 app 里给项目组发一句你好"))
        self.assertTrue(generic_wanted("添加按钮（＋）", "在备忘录里新建一条笔记"))


class ScreenChangeTest(unittest.TestCase):
    """What the last action changed on screen, in a window seen through the screenshot (systemone)."""

    def test_new_words_are_reported_with_where_they_are(self):
        from types import SimpleNamespace
        from deskmind_hands.adapters.systemone import SystemOneAdapter
        from deskmind_hands.drivers.base import Element
        from deskmind_hands.geometry import Rect
        a = SystemOneAdapter.__new__(SystemOneAdapter)
        geo = SimpleNamespace(logical=SimpleNamespace(w=1000, h=800))

        def ctx(labels):
            els = [Element(id=f"ocr:{i}", role="listitem", label=l, rect=Rect(10, y, 100, 20))
                   for i, (l, y) in enumerate(labels)]
            return SimpleNamespace(observation=SimpleNamespace(elements=els, geometry=geo),
                                   task=SimpleNamespace(goal="play something"))
        a._with_screen_change(ctx([("Results", 100), ("▶ Play all", 150)]), [{"action": "search"}])
        out = a._with_screen_change(ctx([("Results", 100), ("▶ Play all", 150), ("Song A · Artist", 760)]),
                                    [{"action": "▶ Play all", "kind": "click"}])
        self.assertEqual(out[-1]["screen_now_shows"], ["Song A · Artist (bottom)"])
        self.assertEqual(out[-1]["screen_no_longer_shows"], [])

    def test_nothing_added_when_nothing_changed_or_no_screenshot_words(self):
        from types import SimpleNamespace
        from deskmind_hands.adapters.systemone import SystemOneAdapter
        a = SystemOneAdapter.__new__(SystemOneAdapter)
        c = SimpleNamespace(observation=SimpleNamespace(elements=[], geometry=None),
                            task=SimpleNamespace(goal="g"))
        self.assertEqual(a._with_screen_change(c, [{"action": "x"}]), [{"action": "x"}])   # AX mode: untouched



class NoKeysWhereNoneArrive(unittest.TestCase):
    """10-01: just launched, the music app had no window; KEY was offered, G18b pressed cmd+f twice, and both were
    refused. The driver says when a chord can reach the window (Observation.accepts_keys); KEY is offered only then."""

    def operations(self, accepts_keys: bool) -> set:
        from unittest import mock
        from deskmind_hands.adapters.base import TurnContext
        from deskmind_hands.adapters.systemone import SystemOneAdapter
        from deskmind_hands.drivers.base import Element, Observation
        from deskmind_hands.geometry import ImageTransform, Rect, ScreenGeometry, Size
        from deskmind_hands.live import live_task
        s = Size(900, 700)
        o = Observation(id="o", geometry=ScreenGeometry(s, s), transform=ImageTransform.identity(s),
                        focused_app="TextEdit", window_title="notes.txt", accepts_keys=accepts_keys,
                        elements=[Element(id="e1", role="textarea", label="notes", rect=Rect(10, 10, 400, 300),
                                          app="TextEdit")])
        task = live_task("Add a line to notes.txt and save it.", None, app="com.apple.TextEdit", max_actions=5,
                         wall_clock_s=60)
        a = SystemOneAdapter()
        seen = {}

        def ask(state, questions):
            seen.update(questions)
            return {"operation": {"choice": "BLOCKED", "confidence": 1.0}}
        with mock.patch.object(a, "_ask", side_effect=ask):
            try:
                a.propose(TurnContext(task=task, observation=o, history=[], channels=frozenset({"ax"})))
            except Exception:   # noqa: BLE001 -- only the questions asked matter here
                pass
        return set(seen["operation"]["criteria"])

    def test_key_is_offered_where_a_chord_arrives(self):
        self.assertIn("KEY", self.operations(True))

    def test_no_key_without_a_window_or_through_pixels(self):
        self.assertNotIn("KEY", self.operations(False))


if __name__ == "__main__":
    unittest.main()
