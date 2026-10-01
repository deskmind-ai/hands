"""Layer 4's verdict (tools/harness_ab.py): the gate, applied task by task."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from harness_ab import compare   # noqa: E402


def runs(**tasks):
    return {t: [{"task": t, "passed": p} for p in ps] for t, ps in tasks.items()}


class Gate(unittest.TestCase):
    def test_one_task_worse_is_worse_even_if_the_total_rises(self):
        r = compare(runs(a=[1, 1], b=[0, 0]), runs(a=[1, 0], b=[1, 1]))
        self.assertEqual(r["verdict"], "worse")
        self.assertEqual(r["worse"], ["a"])

    def test_better_and_no_change(self):
        self.assertEqual(compare(runs(a=[0, 1]), runs(a=[1, 1]))["verdict"], "better")
        self.assertEqual(compare(runs(a=[1, 1]), runs(a=[1, 1]))["verdict"], "no change")

    def test_an_incomplete_pair_gives_no_headline(self):
        r = compare(runs(a=[1, 1], b=[1]), runs(a=[1, 1]))
        self.assertEqual(r["verdict"], "no headline: incomplete pairs")
        self.assertEqual(r["incomplete"], ["b"])


if __name__ == "__main__":
    unittest.main()
