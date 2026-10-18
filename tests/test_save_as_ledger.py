"""Save As renames the document the ledger knows (0.5 desktop test): text typed into a new document ("未命名-2") and saved
as draft.txt was still kept under "未命名-2", and the DONE check held the run back -- "'未命名-2' was written but not
saved", or "nothing has been written to 'draft.txt'" -- over a file that was written and saved."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands import done_check                       # noqa: E402
from deskmind_hands.adapters.systemone import SystemOneAdapter   # noqa: E402

GOAL = "新建一个纯文本文稿，写入“This is a draft.”，保存为 draft.txt"


def ctx(detail: str, title: str, ok: bool = True):
    return SimpleNamespace(history=[SimpleNamespace(result_ok=ok, result_detail=detail)],
                           observation=SimpleNamespace(window_title=title))


def run(saved_detail: str, after: str):
    a = SystemOneAdapter(url="http://127.0.0.1:9")
    a._pending_write = ("未命名-2", "This is a draft.")
    a._keep_ledger(ctx("set 16 chars in the background, verified", "未命名-2"))
    a._keep_ledger(ctx("save-as name set to 'draft.txt'", "未命名-2"))
    a._keep_ledger(ctx("save-as place set to '文稿'", "未命名-2"))
    a._keep_ledger(ctx(saved_detail, after))
    return a._ledger


class SaveAs(unittest.TestCase):
    def test_the_driver_names_the_new_file(self):
        ledger = run("saved 'draft.txt' (scripted, in the background)", "draft.txt")
        self.assertIsNone(done_check.objection(GOAL, ledger, ["draft.txt"], []))
        self.assertIn("draft.txt", ledger.written)

    def test_the_driver_names_the_untitled_document(self):
        ledger = run("saved '未命名-2' (scripted, in the background)", "draft.txt")
        self.assertIsNone(done_check.objection(GOAL, ledger, ["draft.txt"], []))
        self.assertIn("draft.txt", ledger.written)
        self.assertNotIn("未命名-2", ledger.unsaved)

    def test_a_plain_save_is_unchanged(self):
        a = SystemOneAdapter(url="http://127.0.0.1:9")
        a._pending_write = ("notes.txt", "x")
        a._keep_ledger(ctx("set 1 chars", "notes.txt"))
        a._keep_ledger(ctx("saved 'notes.txt'", "notes.txt"))
        self.assertEqual((a._ledger.saved, a._ledger.unsaved), ({"notes.txt"}, set()))

    def test_a_save_as_name_that_was_not_saved_renames_nothing(self):
        a = SystemOneAdapter(url="http://127.0.0.1:9")
        a._pending_write = ("未命名-2", "x")
        a._keep_ledger(ctx("set 1 chars", "未命名-2"))
        a._keep_ledger(ctx("save-as name set to 'draft.txt'", "未命名-2"))
        a._keep_ledger(ctx("cancelled", "未命名-2"))
        self.assertEqual(set(a._ledger.written), {"未命名-2"})
        self.assertIn("未命名-2", a._ledger.unsaved)


class CancelledSheet(unittest.TestCase):
    def test_a_cancelled_sheet_does_not_rename_a_later_plain_save(self):
        """Review of #31: the typed name stayed pending after the sheet was cancelled, and a later plain save of the
        document took it -- the ledger would then say draft.txt was written and saved when it never was."""
        a = SystemOneAdapter(url="http://127.0.0.1:9")
        a._pending_write = ("notes.txt", "x")
        a._keep_ledger(ctx("set 1 chars", "notes.txt"))
        a._keep_ledger(ctx("save-as name set to 'draft.txt'", "notes.txt"))
        a._keep_ledger(ctx("pressed escape", "notes.txt"))            # the sheet cancelled
        a._keep_ledger(ctx("clicked 'Edit'", "notes.txt"))
        a._keep_ledger(ctx("saved 'notes.txt'", "notes.txt"))         # a plain cmd+s, later
        self.assertEqual(set(a._ledger.written), {"notes.txt"})
        self.assertEqual(a._ledger.saved, {"notes.txt"})
        self.assertNotIn("draft.txt", a._ledger.saved)

    def test_a_failed_sheet_step_clears_it_too(self):
        a = SystemOneAdapter(url="http://127.0.0.1:9")
        a._pending_write = ("notes.txt", "x")
        a._keep_ledger(ctx("set 1 chars", "notes.txt"))
        a._keep_ledger(ctx("save-as name set to 'draft.txt'", "notes.txt"))
        a._keep_ledger(ctx("the Save button is not there", "notes.txt", ok=False))
        a._keep_ledger(ctx("saved 'notes.txt'", "notes.txt"))
        self.assertEqual(set(a._ledger.written), {"notes.txt"})

    def test_the_new_adapter_has_nothing_pending(self):
        self.assertIsNone(SystemOneAdapter(url="http://127.0.0.1:9")._save_as)


if __name__ == "__main__":
    unittest.main()
