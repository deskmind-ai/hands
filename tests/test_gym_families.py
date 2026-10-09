"""Gym task families (deskmind#59, T8): the same task on different items, every variant graded by the app's oracle.
A family's variants are the same seeds every time, come from a seed range no collection has used, are held out by
default, and differ in what the task is about."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.gym import families, mail   # noqa: E402

FAMILY = "mail/delete/邮筒/zh"


class Families(unittest.TestCase):
    def test_variants_are_the_family_and_the_same_every_time(self):
        seeds = families.variants(FAMILY, 6)
        self.assertEqual(seeds, families.variants(FAMILY, 6))
        self.assertTrue(all(s >= families.SEED_BASE for s in seeds), "never a collection seed")
        tasks = [mail.make_task(s, 2) for s in seeds]
        self.assertEqual({families.family("mail", t) for t in tasks}, {FAMILY})
        self.assertEqual({t["split"] for t in tasks}, {"heldout"})
        self.assertGreaterEqual(len({(t["target"]["from"], t["target"]["subject"]) for t in tasks}), 5,
                                "variants are about different items")

    def test_start_skips_the_first_variants(self):
        self.assertEqual(families.variants(FAMILY, 3, start=2), families.variants(FAMILY, 5)[2:])

    def test_every_variant_is_graded_by_the_oracle(self):
        t = mail.make_task(families.variants(FAMILY, 1)[0], 2)
        self.assertFalse(mail.passed(t, {"deleted": [], "flagged": t["page"]["flagged"], "replies": []}))
        self.assertTrue(mail.passed(t, {"deleted": [t["target"]["id"]], "flagged": t["page"]["flagged"],
                                        "replies": []}))

    def test_a_family_is_named_by_its_app_kind_skin_and_language(self):
        self.assertEqual(families.family("music", {"app": "听岛", "lang": "en"}), "music/music/听岛/en")
        with self.assertRaises(ValueError):
            families.variants("nosuch/kind/x/en", 1)
        with self.assertRaises(ValueError):
            families.variants("mail/delete/NoSuchSkin/en", 1)


if __name__ == "__main__":
    unittest.main()
