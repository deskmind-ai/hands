"""gym --lessons: a share of runs, by seed, shown pinned lessons -- this family's, or others' that do not apply."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from deskmind_hands import lessons
from tools.gym import run as gym_run

POOL = {"music": {"relevant": ["To play a song from search results, double-click its row."],
                  "general": ["Change only what the goal names."]},
        "mail": {"relevant": ["Reply from the message itself, not from a new message."],
                 "general": ["Check every field before you send."]},
        "*": {"irrelevant": ["Close any dialog you did not open before acting."]}}


class GymLessons(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "pool.json").write_text(json.dumps(POOL), encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {})
        self.env.start()
        self.runs = mock.patch.object(gym_run, "RUNS", self.tmp)
        self.runs.start()

    def tearDown(self):
        self.runs.stop()
        self.env.stop()

    def args(self, share):
        return SimpleNamespace(lessons=share, lessons_pool=self.tmp / "pool.json", app="music")

    def test_off_by_default(self):
        self.assertEqual(gym_run._stage_lessons(self.args(0.0), 1, "r1"), ("off", []))
        self.assertEqual(os.environ["HANDS_LESSONS"], "0")

    def test_the_share_and_both_kinds(self):
        modes = {}
        for seed in range(200):
            mode, texts = gym_run._stage_lessons(self.args(0.3), seed, f"r{seed}")
            modes[mode] = modes.get(mode, 0) + 1
            if mode == "relevant":
                self.assertTrue(set(texts) <= set(POOL["music"]["relevant"] + POOL["music"]["general"]))
            elif mode == "irrelevant":
                self.assertTrue(set(texts) <= set(POOL["mail"]["relevant"] + POOL["*"]["irrelevant"]))
                shown = [l.text for l, _ in lessons.recall("Play Lanterns by Lena Vale in 声海", set(),
                                                             lessons.load(Path(os.environ["HANDS_LESSONS_DIR"])))]
                self.assertEqual(set(shown), set(texts))      # pinned: shown although nothing in the goal matches
        self.assertTrue(40 <= modes["relevant"] + modes["irrelevant"] <= 80, modes)
        self.assertTrue(modes["relevant"] > 10 and modes["irrelevant"] > 10, modes)

    def test_the_same_seed_shows_the_same_lessons(self):
        a = [gym_run._stage_lessons(self.args(0.3), s, f"a{s}") for s in range(30)]
        b = [gym_run._stage_lessons(self.args(0.3), s, f"b{s}") for s in range(30)]
        self.assertEqual(a, b)



class UserWaitInRows(unittest.TestCase):
    def test_read_from_the_runs_record(self):
        import time as _time
        with tempfile.TemporaryDirectory() as d, mock.patch.object(gym_run, "RUNS", Path(d)):
            started = _time.time()
            run = Path(d) / "do-20261001-200000"
            run.mkdir()
            (run / "run.json").write_text(json.dumps({"metrics": {"user_wait_s": 42.37}}), encoding="utf-8")
            self.assertEqual(gym_run._user_wait(started), 42.4)
            self.assertIsNone(gym_run._user_wait(_time.time() + 60))


if __name__ == "__main__":
    unittest.main()
