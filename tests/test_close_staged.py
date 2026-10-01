"""A staged task's TextEdit documents are closed when its driver closes (bench G13's source.txt was left open, 10-01),
and a live run's are not."""
import unittest
from pathlib import Path
from unittest import mock

from deskmind_hands.drivers import peekaboo
from deskmind_hands.drivers.peekaboo import PeekabooDriver


def driver(stage):
    d = PeekabooDriver.__new__(PeekabooDriver)
    d.stage_commands, d.ws = stage, Path("/tmp/runs/G13-roundtrip-r3/ws")
    d._staged_window, d._clipboard_saved, d._mcp = None, None, None
    d.scripts = []
    d._osa = lambda script: d.scripts.append(script) or (True, "")
    return d


class CloseStaged(unittest.TestCase):
    def test_a_staged_tasks_documents_are_closed(self):
        d = driver(['open -a TextEdit "$WS/source.txt"'])
        with mock.patch.object(peekaboo, "_app_running", return_value=True):
            d.close()
        self.assertTrue(d.scripts)
        self.assertTrue(all('whose path starts with "' in s and '/G13-roundtrip-r3/ws/"' in s and "saving no" in s
                            for s in d.scripts))

    def test_a_live_run_closes_nothing_of_the_users(self):
        d = driver([])
        with mock.patch.object(peekaboo, "_app_running", return_value=True):
            d.close()
        self.assertEqual(d.scripts, [])


if __name__ == "__main__":
    unittest.main()
