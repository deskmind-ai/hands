"""A scripted save reports what happened (TextEdit, drivers/peekaboo.py `_textedit_save`).

cmd+S on a document that was never saved has nowhere to go: TextEdit shows its save sheet and saves nothing. That was
reported as "could not confirm ... saved", an error. The invariant: the result says what happened, told by structure
(the window's sheet, the document's `modified`), never by the title --

  saved         the document is no longer modified afterwards       ok,    outcome "saved"
  sheet_opened  still modified, and a sheet is open on the window    ok,    outcome "sheet_opened", no files
  failed        still modified, and no sheet -- or it cannot be read error

`axchrome.sheet_present` answers the sheet question: True / False from the window's children, None when it cannot be
read (no window at the frame, a failed read) -- never False on a guess.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from deskmind_hands.drivers import axchrome  # noqa: E402
from deskmind_hands.drivers.base import ExecResult, input_scope  # noqa: E402
from deskmind_hands.drivers.peekaboo import PeekabooDriver  # noqa: E402


def driver(sheet):
    d = PeekabooDriver.__new__(PeekabooDriver)
    d.bin, d.timeout_s, d._mcp, d.app = "peekaboo", 5, None, "com.apple.TextEdit"
    d._scripted_actions = 0
    d._save_sheet_open = lambda: sheet
    return d


def osa(modified_after: str):
    """`osascript` as TextEdit answers it: modified before the save, `modified_after` after; the path; no text."""
    reads = {"n": 0}

    class Out:
        returncode, stderr = 0, ""

        def __init__(self, cmd):
            script = cmd[-1]
            if "get modified" in script:
                reads["n"] += 1
                self.stdout = "true" if reads["n"] == 1 else modified_after
            elif "get path" in script:
                self.stdout = "/tmp/x/hello.txt"
            else:
                self.stdout = ""
    return lambda cmd, **k: Out(cmd)


class SaveOutcome(unittest.TestCase):
    def run_save(self, modified_after: str, sheet):
        from deskmind_hands.drivers import peekaboo
        d = driver(sheet)
        with mock.patch("subprocess.run", osa(modified_after)), mock.patch.object(peekaboo.time, "sleep", lambda s: None), \
                mock.patch("deskmind_hands.axdocument.document_text", lambda *a, **k: "hi"), input_scope():
            return d._textedit_save("未命名2")

    def test_saved(self):
        for sheet in (False, None, True):          # clean afterwards: saved, whatever a sheet read says
            with self.subTest(sheet=sheet):
                r = self.run_save("false", sheet)
                self.assertTrue(r.ok)
                self.assertEqual(r.outcome, "saved")

    def test_sheet_opened(self):
        r = self.run_save("true", True)
        self.assertTrue(r.ok)
        self.assertEqual(r.outcome, "sheet_opened")
        self.assertEqual(r.files, ())               # nothing written: nothing to credit
        self.assertNotIn("could not confirm", r.detail)

    def test_failed(self):
        for sheet in (False, None):                 # no sheet, or not known: not saved and no sheet to show for it
            with self.subTest(sheet=sheet):
                r = self.run_save("true", sheet)
                self.assertFalse(r.ok)
                self.assertEqual(r.outcome, "")
                self.assertIn("could not confirm", r.detail)

    def test_outcome_defaults_empty(self):
        self.assertEqual(ExecResult(True).outcome, "")


class SheetPresent(unittest.TestCase):
    def check(self, win, children, raises=False):
        def get(el, attr):
            if raises:
                raise axchrome.AXReadError("read failed")
            if attr == "AXChildren":
                return children
            return el.get("role")
        with mock.patch.object(axchrome, "_the_window", lambda pid, frame, wid=None: win):
            return axchrome.sheet_present(1, (0, 0, 10, 10), wid=None, get=get)

    def test_table(self):
        w = {"role": "AXWindow"}
        self.assertIs(self.check(w, [{"role": "AXGroup"}, {"role": "AXSheet"}]), True)
        self.assertIs(self.check(w, [{"role": "AXGroup"}, {"role": "AXButton"}]), False)
        self.assertIs(self.check(w, []), False)
        self.assertIsNone(self.check(None, []))                      # no window at the frame (or two): not known
        self.assertIsNone(self.check(w, [], raises=True))            # a failed read: not known, never "no sheet"


if __name__ == "__main__":
    unittest.main()
