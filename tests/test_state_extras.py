"""The planner-state extras behind switches (off by default, for G19 on): the environment section and new-element
marks. Off, the state is exactly what G18b was trained on."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.adapters.base import TurnContext   # noqa: E402
from deskmind_hands.adapters.systemone import SystemOneAdapter   # noqa: E402
from deskmind_hands.drivers.base import Element, Observation   # noqa: E402
from deskmind_hands.geometry import ImageTransform, Rect, ScreenGeometry, Size   # noqa: E402
from deskmind_hands.live import live_task   # noqa: E402


def obs(labels, title="parts.csv", n=1):
    s = Size(900, 700)
    return Observation(id=f"obs-{n}", geometry=ScreenGeometry(s, s), transform=ImageTransform.identity(s),
                       elements=[Element(id=f"e{i}", role="button", label=l, rect=Rect(0, 30 * i, 80, 20),
                                         app="TextEdit") for i, l in enumerate(labels)],
                       focused_app="TextEdit", window_title=title)


def state(adapter, task, o):
    return adapter._state(TurnContext(task=task, observation=o, history=[], channels=frozenset({"ax"})))


class Extras(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        for n in ("parts.csv", "notes.txt", ".DS_Store"):
            (self.dir / n).write_text("x")
        self.task = live_task("Open parts.csv", self.dir, app="com.apple.TextEdit", max_actions=5, wall_clock_s=60)

    def test_off_by_default(self):
        a = SystemOneAdapter()
        with mock.patch.dict(os.environ, {"HANDS_ENV_SECTIONS": "0", "HANDS_MARK_NEW": "0"}):
            state(a, self.task, obs(["Save"]))
            st = state(a, self.task, obs(["Save", "Close"], n=2))
        self.assertNotIn("environment", st)
        self.assertFalse(any(e.get("new") for e in st["elements"]))

    def test_environment_names_the_folder_and_its_files(self):
        with mock.patch.dict(os.environ, {"HANDS_ENV_SECTIONS": "1"}):
            env = state(SystemOneAdapter(), self.task, obs(["Save"]))["environment"]
        self.assertEqual(env["attached_folder"], {"name": self.dir.name, "files": ["notes.txt", "parts.csv"]})
        self.assertRegex(env["today"], r"^\d{4}-\d{2}-\d{2}$")

    def test_new_elements_are_marked_in_the_same_window_only(self):
        a = SystemOneAdapter()
        with mock.patch.dict(os.environ, {"HANDS_MARK_NEW": "1"}):
            first = state(a, self.task, obs(["Save"]))
            second = state(a, self.task, obs(["Save", "Close"], n=2))
            other = state(a, self.task, obs(["Save", "Close", "Open"], title="notes.txt", n=3))
        self.assertFalse(any(e.get("new") for e in first["elements"]))           # nothing to compare with
        self.assertEqual([e["label"] for e in second["elements"] if e.get("new")], ["Close"])
        self.assertFalse(any(e.get("new") for e in other["elements"]))           # another window: not marked


if __name__ == "__main__":
    unittest.main()
