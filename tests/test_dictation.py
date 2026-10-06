"""Lines after a colon are dictated text, except a list of numbered steps that name file operations (deskmind#26):
a numbered list of things to do was offered line by line as values, and a planner renamed a file to one of its
instructions. Dictation that names no writing (a message to send, a reply) must stay dictated."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.adapters.systemone import value_candidates   # noqa: E402

TODO = ("请依次完成下面几件事，其他文件和文件夹不要动：\n1. 把 记录-81.txt 改名为 plan-2.txt。\n"
        "2. 把 backup 文件夹里的 summary-95.log 移到工作目录顶层。\n3. 新建文件夹 草稿，把 计划-48.md 放进去。")
G04 = ("report.txt 已在 TextEdit 中打开，目前是空的。请逐字写入下面两行，\n全角标点、半角数字和换行都要一致，然后保存：\n"
       "一季度营收 1,240 万元，环比增长 8.5%。\n未结订单 37 笔，其中逾期 3 笔。\n")


class Dictation(unittest.TestCase):
    def test_a_list_of_things_to_do_is_not_text_to_type(self):
        values = value_candidates(TODO)
        self.assertFalse([v for v in values if "把" in v or v.startswith(("1.", "2.", "3."))], values)
        for name in ("plan-2.txt", "summary-95.log", "计划-48.md", "草稿"):
            self.assertIn(name, values)                                  # the names are still offered

    def test_dictated_lines_still_are(self):
        self.assertEqual(value_candidates(G04)[0], "一季度营收 1,240 万元，环比增长 8.5%。\n未结订单 37 笔，其中逾期 3 笔。\n")
        self.assertEqual(value_candidates("在 todo.txt 里写下：\n1. 买牛奶\n2. 交报告")[0], "1. 买牛奶\n2. 交报告\n")
        self.assertEqual(value_candidates("Open notes.txt and write these lines:\nalpha\nbeta")[0], "alpha\nbeta\n")

    def test_messages_without_a_writing_word_stay_dictated(self):
        self.assertEqual(value_candidates("把下面这段话发给 Lisa：\n周五的会改到下午三点。")[0], "周五的会改到下午三点。\n")
        self.assertEqual(value_candidates("回复他：\n收到，明天给你。")[0], "收到，明天给你。\n")
        self.assertEqual(value_candidates("Send this message to Sam:\nRunning late, start without me.")[0],
                         "Running late, start without me.\n")


if __name__ == "__main__":
    unittest.main()
