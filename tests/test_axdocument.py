"""The window's document, read generically: the parsing, same-titled windows left unknown, and the once-only ask
of an app that does not answer. unittest, as CI collects it."""
from __future__ import annotations

import unittest
from unittest import mock

from deskmind_hands import axdocument as ad

APP = "com.example.doc"


class Paths(unittest.TestCase):
    def test_document_values_become_posix_paths(self):
        self.assertEqual(ad._posix("file:///Users/me/a%20b.txt"), "/Users/me/a b.txt")
        self.assertEqual(ad._posix("/Users/me/a.txt"), "/Users/me/a.txt")
        self.assertEqual(ad._posix("Macintosh HD:Users:me:a.txt"), "/Users/me/a.txt")
        self.assertIsNone(ad._posix(""))
        self.assertIsNone(ad._posix("https://example.com/"))


class Picking(unittest.TestCase):
    ROWS = ("notes.txt\tfile:///ws/one/notes.txt\t10\t20\ttrue\n"
            "notes.txt\tfile:///ws/two/notes.txt\t400\t20\tfalse\n"
            "未命名\t\t0\t0\ttrue\n"
            "ghost\t\t0\t0\t\n")

    def test_the_window_is_picked_by_title_then_position(self):
        self.assertEqual(ad._pick(self.ROWS, "notes.txt", [400, 20, 500, 300]), ("/ws/two/notes.txt", False))
        self.assertEqual(ad._pick(self.ROWS, "notes.txt", [10, 20, 500, 300]), ("/ws/one/notes.txt", True))
        self.assertEqual(ad._pick(self.ROWS, "未命名", None), (None, True))     # never saved: no path, unsaved changes
        self.assertIsNone(ad._pick(self.ROWS, "ghost", None))                   # neither known: not a document window
        self.assertIsNone(ad._pick(self.ROWS, "other", None))
        # accessibility rows carry no modified column: the path alone
        self.assertEqual(ad._pick("a.txt\tfile:///ws/a.txt\t1\t2\n", "a.txt", None), ("/ws/a.txt", None))

    def test_two_windows_of_one_title_that_cannot_be_told_apart_are_unknown(self):
        """Never the first one: the user's notes.txt was taken for the task's (PR #48 review)."""
        self.assertIsNone(ad._pick(self.ROWS, "notes.txt", None))                 # no position to tell them apart
        self.assertIsNone(ad._pick(self.ROWS, "notes.txt", [900, 900, 10, 10]))   # a position neither is at
        # an app's scripted bounds are not screen coordinates: nothing is near, so unknown rather than the first
        rows = "notes.txt\tfile:///ws/task/notes.txt\t152\t73\tfalse\nnotes.txt\tfile:///ws/user/notes.txt\t60\t40\tfalse\n"
        self.assertIsNone(ad._pick(rows, "notes.txt", [212, 114, 591, 381]))

    def test_the_scripting_never_overrides_a_document_accessibility_identified(self):
        """AX told the windows apart by position; the scripting could not (one row per title, bounds unusable):
        the scripting's modified flag is not known to be this window's."""
        def osa(script):
            if "System Events" in script:
                return "notes.txt\tfile:///ws/task/notes.txt\t212\t114\nnotes.txt\tfile:///ws/user/notes.txt\t600\t114\n"
            return "notes.txt\tfile:///ws/user/notes.txt\t60\t40\ttrue\n"
        with mock.patch.object(ad, "_osa", osa):
            ad._NO_SCRIPTING.discard(APP)
            self.assertEqual(ad.window_document(1, APP, "notes.txt", [212, 114, 591, 381]), ("/ws/task/notes.txt", None))


class Asking(unittest.TestCase):
    def setUp(self):
        ad._NO_SCRIPTING.discard(APP)

    def tearDown(self):
        ad._NO_SCRIPTING.discard(APP)

    def test_an_app_whose_scripting_hangs_is_asked_once(self):
        calls = []

        def osa(script):
            calls.append(script)
            return "" if "System Events" in script else None   # AX: nothing; scripting: timed out
        with mock.patch.object(ad, "_osa", osa):
            self.assertIsNone(ad.window_document(1, "com.example.hung", "x", None))
            self.assertIsNone(ad.window_document(1, "com.example.hung", "x", None))
        self.assertEqual(sum("application id" in s for s in calls), 1)     # scripting asked the first time only
        self.assertEqual(sum("System Events" in s for s in calls), 2)      # accessibility is cheap and asked each time
        ad._NO_SCRIPTING.discard("com.example.hung")

    def test_an_app_that_answers_with_nothing_is_asked_again(self):
        calls = []

        def osa(script):
            calls.append(script)
            return ""
        with mock.patch.object(ad, "_osa", osa):
            for _ in range(2):
                self.assertIsNone(ad.window_document(1, APP, "未命名", None))
        self.assertEqual(sum("application id" in s for s in calls), 2)

    def test_document_text_is_the_apps_own_text_character_for_character(self):
        seen = []

        def osa(script):
            seen.append(script)
            return "ok\nalpha\rbeta\ngamma\n\n"     # the document's own breaks (CR and LF, a trailing one), then osascript's line end
        with mock.patch.object(ad, "_osa", osa):
            self.assertEqual(ad.document_text(APP, "notes.txt", [10, 20, 300, 200]), "alpha\rbeta\ngamma\n")
        self.assertIn('is "notes.txt"', seen[-1])
        self.assertNotIn("bounds", seen[-1])                                  # by title only
        # two windows of that title: unknown, never the first one's text
        with mock.patch.object(ad, "_osa", lambda script: "n=2\n"):
            self.assertIsNone(ad.document_text(APP, "notes.txt", None))
        # a window of that title whose text the app cannot give (an error, no value): unknown, never "" (hands#60 review)
        with mock.patch.object(ad, "_osa", lambda script: "unread\n"):
            self.assertIsNone(ad.document_text(APP, "notes.txt", None))
        self.assertIn("set bad to true", seen[-1])
        self.assertLess(seen[-1].index("set v to (text of document of w)"), seen[-1].index("on error\nset bad to true"))
        # no such window: None; an app that hangs is asked once
        with mock.patch.object(ad, "_osa", lambda script: "n=0\n"):
            self.assertIsNone(ad.document_text(APP, "other", None))
        with mock.patch.object(ad, "_osa", lambda script: None):
            self.assertIsNone(ad.document_text(APP, "notes.txt", None))
            self.assertIsNone(ad.document_text(APP, "notes.txt", None))
        self.assertIn(APP, ad._NO_SCRIPTING)


if __name__ == "__main__":
    unittest.main()
