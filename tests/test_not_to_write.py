"""Which documents a goal must not write in (adapters/systemone.py, not_to_write)."""
import unittest

from deskmind_hands.adapters.systemone import not_named, not_to_write

D1EN = ("In Safari, a parts inventory table is open. Open parts.csv from the attached folder in TextEdit, append the "
        "table's four rows to it as `Part No.,Name,Qty`, sorted by Qty from high to low (the header is already there; "
        "don't repeat it), then save and close it.")


class NotToWrite(unittest.TestCase):
    def test_a_document_the_goal_does_not_name(self):
        # 10-01: parts.csv closed, the driver moved on to another task's source.txt, and the rows went in there.
        self.assertTrue(not_to_write(D1EN, "source.txt"))
        self.assertFalse(not_to_write(D1EN, "parts.csv"))

    def test_windows_without_a_file_name_are_not_held_back(self):
        self.assertFalse(not_to_write(D1EN, "Parts inventory"))
        self.assertFalse(not_to_write("Write a note and save it as todo.txt", "Untitled"))
        self.assertFalse(not_to_write("Write a note and save it as todo.txt", "未命名"))

    def test_the_source_and_a_destination_mismatch_as_before(self):
        goal = "Find the line whose status is critical in log.txt and write its project number into answer.txt."
        self.assertTrue(not_to_write(goal, "log.txt"))
        self.assertFalse(not_to_write(goal, "answer.txt"))

    def test_saving_is_held_back_only_in_named_documents(self):
        goal = "Write the summary into a new document and save it as report.txt"
        self.assertFalse(not_to_write(goal, "Untitled", saving=True))
        self.assertTrue(not_to_write(D1EN, "source.txt", saving=True))
        self.assertFalse(not_to_write(D1EN, "打开", saving=True))

    def test_a_document_the_goal_does_not_name_is_not_closed(self):
        # 10-01: parts.csv done and closed, G18b closed the user's notes.txt too.
        self.assertTrue(not_named(D1EN, "notes.txt"))
        self.assertFalse(not_named(D1EN, "parts.csv"))
        self.assertFalse(not_named(D1EN, "Parts inventory"))
        self.assertFalse(not_named("Close every TextEdit window", "notes.txt"))

    def test_a_goal_that_names_no_file_holds_nothing_back(self):
        self.assertFalse(not_to_write("Type hello into the open document", "notes.txt"))


if __name__ == "__main__":
    unittest.main()
