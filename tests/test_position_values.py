"""A scroll bar's position is not text on the screen (deskmind#63). In TextEdit the two scroll bars' AXValues
(0.1851851791143417, 0.615384578704834) were in page.text, became two of the three values offered on a replace-all
task, and one was typed over the document's first line on every step. Controls whose value is a position or a level
keep their names; their values are neither page text nor values to type."""
from __future__ import annotations

import dataclasses
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from test_render import context                  # noqa: E402
from deskmind_hands.drivers.base import Element   # noqa: E402
from deskmind_hands.pipeline import ask, render   # noqa: E402

SCROLL = [Element(id="sb1", role="scrollBar", ax_role="AXScrollBar", label="0.1851851791143417",
                  value="0.1851851791143417"),
          Element(id="sb2", role="scrollBar", ax_role="AXScrollBar", label="0.615384578704834",
                  value="0.615384578704834"),
          Element(id="vi", role="other", ax_role="AXValueIndicator", label="0.2136894824707846",
                  value="0.2136894824707846"),
          Element(id="sl", role="slider", ax_role="AXSlider", label="音量", value="0.5"),
          Element(id="doc", role="textArea", ax_role="AXTextArea", label="", settable=True,
                  value="Project Orion is ready for review.\nOrion ships in Q4.\n")]


def with_scroll_bars():
    ctx = context()
    obs = dataclasses.replace(ctx.observation, elements=list(ctx.observation.elements) + SCROLL)
    return dataclasses.replace(ctx, observation=obs)


class PositionValues(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.startswith("HANDS_")},
                              clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_a_scroll_bar_position_is_not_page_text(self):
        text = render.page_text(with_scroll_bars().observation).split("\n")
        for v in ("0.1851851791143417", "0.615384578704834", "0.2136894824707846", "0.5"):
            self.assertNotIn(v, text)
        self.assertIn("音量", text, "a slider keeps its name")
        self.assertIn("Project Orion is ready for review.\nOrion ships in Q4.", "\n".join(text), "content stays")

    def test_nor_a_value_to_type(self):
        ctx = with_scroll_bars()
        mem = render.Memory()
        render.remember(mem, ctx)
        state = render.render(ctx, mem, prior=[], lessons=None, progress=None, done_note=None)
        got = ask.build(ctx, state, mem=mem, seen={}, prior=[], text_helper=None)
        self.assertFalse([c for c in got.candidates if c.strip().startswith(("0.18518", "0.61538", "0.21368"))],
                         got.candidates)


if __name__ == "__main__":
    unittest.main()
