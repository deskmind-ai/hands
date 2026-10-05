"""A REPLACE_TEXT whose new value already holds the whole field is a rewrite, not a splice. On G04 (app 0.4.0 and
after, deskmind#23) report.txt was already right and saved when the planner chose REPLACE_TEXT "1,240" -> the whole
dictated text; spliced in, the document went inside itself, and every later step rewrote a longer copy until the
budget ran out. As a rewrite the step writes the text the field already holds, which the driver refuses as nothing
changed, and the value is not offered again."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.adapters.base import TurnContext   # noqa: E402
from deskmind_hands.adapters.systemone import SystemOneAdapter, replace_edit   # noqa: E402
from deskmind_hands.drivers.base import Element, Observation   # noqa: E402
from deskmind_hands.geometry import ImageTransform, Rect, ScreenGeometry, Size   # noqa: E402
from deskmind_hands.live import live_task   # noqa: E402

GOAL = ("report.txt 已在 TextEdit 中打开，目前是空的。请逐字写入下面两行，\n全角标点、半角数字和换行都要一致，然后保存：\n"
        "一季度营收 1,240 万元，环比增长 8.5%。\n未结订单 37 笔，其中逾期 3 笔。\n")
DOC = "一季度营收 1,240 万元，环比增长 8.5%。\n未结订单 37 笔，其中逾期 3 笔。\n"


class ReplaceEdit(unittest.TestCase):
    def test_a_value_holding_the_whole_field_rewrites_it(self):
        self.assertEqual(replace_edit(GOAL, DOC, "1,240", DOC), DOC)   # not "一季度营收 一季度营收 1,240 …"
        bigger = DOC + "备注：已核对。\n"
        self.assertEqual(replace_edit(GOAL, DOC, "1,240", bigger), bigger)

    def test_ordinary_edits_are_unchanged(self):
        self.assertEqual(replace_edit("Budget from 1200 to 1500", "x\nBudget: 1200\n", "Budget: 1200", "1500"),
                         "x\nBudget: 1500\n")                          # only the named value, the label stays
        self.assertEqual(replace_edit("change the status to final", "a\nstatus: draft\n", "status: draft", "final"),
                         "a\nstatus: final\n")
        self.assertEqual(replace_edit(GOAL, DOC, "1,240", "1,250"), DOC.replace("1,240", "1,250"))

    def test_not_an_edit(self):
        self.assertIsNone(replace_edit(GOAL, DOC, "9,999", "1,240"))  # not in the field
        self.assertIsNone(replace_edit(GOAL, DOC, "1,240", "1,240"))  # replaced by itself
        self.assertIsNone(replace_edit(GOAL, DOC, "", DOC))
        self.assertEqual(replace_edit(GOAL, "", "", DOC), None)


class G04Step(unittest.TestCase):
    """The adapter, with the planner's G04 answer at step 3 (REPLACE_TEXT, "1,240" -> the dictated text)."""

    def test_the_proposed_write_is_the_text_not_a_nested_copy(self):
        s = Size(1200, 900)
        o = Observation(id="obs-3", geometry=ScreenGeometry(s, s), transform=ImageTransform.identity(s),
                        elements=[Element(id="e2", role="textarea", ax_role="AXTextArea", label="", value=DOC,
                                          settable=True, rect=Rect(10, 40, 800, 600), app="文本编辑")],
                        focused_app="文本编辑", window_title="report.txt")
        task = live_task(GOAL, Path(tempfile.mkdtemp()), app="com.apple.TextEdit", max_actions=20, wall_clock_s=60)
        a = SystemOneAdapter(url="http://127.0.0.1:9", timeout=2)

        def answer(state, questions):
            def pick(key, want):
                crit = questions[key]["criteria"]
                k = next(k for k, v in crit.items() if (v if isinstance(v, str) else next(iter(v.values()))) == want
                         or k == want)
                return {"type": "choice", "choice": k, "confidence": 0.95, "probabilities": {k: 0.95}}
            out = {"operation": pick("operation", "REPLACE_TEXT"), "replace_text_target": pick("replace_text_target", "1"),
                   "replace_from": pick("replace_from", "1,240"), "type_text_value": pick("type_text_value", DOC)}
            return out
        a._ask = answer
        p = a.propose(TurnContext(task=task, observation=o, history=[], channels=frozenset({"ax"})))
        self.assertEqual(p.action.text, DOC)
        self.assertTrue(p.action.clear_first)


if __name__ == "__main__":
    unittest.main()
