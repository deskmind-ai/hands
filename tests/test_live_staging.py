"""A document app that is not running is not started bare for a live run with a folder of its documents: started with
no document, TextEdit shows its Open panel (17 s of a recording, 10-01); the run's first step opens the document."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deskmind_hands import cli


class OpensFolderDocuments(unittest.TestCase):
    def test_textedit_and_a_folder_of_text_files(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "records.txt").write_text("x")
            self.assertTrue(cli._opens_folder_documents("com.apple.TextEdit", Path(d)))
            self.assertFalse(cli._opens_folder_documents("com.apple.Safari", Path(d)))
            self.assertFalse(cli._opens_folder_documents("com.apple.TextEdit", None))

    def test_a_folder_with_none_of_its_documents(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "photo.png").write_bytes(b"x")
            self.assertFalse(cli._opens_folder_documents("com.apple.TextEdit", Path(d)))
            self.assertTrue(cli._opens_folder_documents("com.apple.Preview", Path(d)))

    def test_unknown_running_state_is_running(self):
        with mock.patch.dict("sys.modules", {"AppKit": None}):
            self.assertTrue(cli._bundle_running("com.apple.TextEdit"))



class NamedFilesNowhere(unittest.TestCase):
    """10-01: a D4 take with the folder left unattached: TextEdit came up with its Open panel and the planner clicked
    in it until stopped as stuck. Named files with no folder and no window showing them are refused up front."""
    GOAL = "Find Lisa Wong's order in records.txt and add it to ledger.csv, then save ledger.csv."

    def test_names_in_an_instruction(self):
        self.assertEqual(cli.named_files(self.GOAL), ["records.txt", "ledger.csv"])
        self.assertEqual(cli.named_files("把 报销单.xlsx 里的金额改成 3.5"), ["报销单.xlsx"])
        self.assertEqual(cli.named_files("Open https://example.com, read v0.1 notes; open TextEdit.app"), [])

    def test_refused_only_when_none_is_open(self):
        with mock.patch.object(cli, "_window_titles", return_value=["Safari", "notes.txt"]):
            self.assertEqual(cli._named_files_nowhere(self.GOAL, {"com.apple.TextEdit"}), ["records.txt", "ledger.csv"])
        with mock.patch.object(cli, "_window_titles", return_value=["records.txt", "Safari"]):
            self.assertEqual(cli._named_files_nowhere(self.GOAL, {"com.apple.TextEdit"}), [])

    def test_unreadable_titles_refuse_nothing(self):
        for titles in ([], ["", "", ""]):
            with mock.patch.object(cli, "_window_titles", return_value=titles):
                self.assertEqual(cli._named_files_nowhere(self.GOAL, {"com.apple.TextEdit"}), [])

    def test_a_file_no_app_of_the_run_opens_is_not_refused(self):
        with mock.patch.object(cli, "_window_titles", return_value=["Inbox"]):
            self.assertEqual(cli._named_files_nowhere("Forward the mail with report.pdf attached",
                                                      {"ai.deskmind.gymhost"}), [])

    def test_no_names_no_refusal(self):
        with mock.patch.object(cli, "_window_titles", return_value=["Safari"]):
            self.assertEqual(cli._named_files_nowhere("Play a song in the music app", {"com.apple.TextEdit"}), [])


if __name__ == "__main__":
    unittest.main()
