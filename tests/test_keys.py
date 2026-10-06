"""cmd+A and cmd+V by the letter on the user's keyboard layout, not key codes 0 and 9 (deskmind#21): on French AZERTY
key code 0 types Q, so the hard-coded cmd+A could reach the app as cmd+Q and quit it; on Dvorak key code 9 is K."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.drivers import keys   # noqa: E402
from deskmind_hands.drivers.peekaboo import PeekabooDriver   # noqa: E402


class PickKeycodes(unittest.TestCase):
    def test_french_and_dvorak(self):
        azerty = {0: "q", 12: "a", 9: "v", 6: "w"}
        self.assertEqual(keys.pick_keycodes(azerty, "av"), {"a": 12, "v": 9})
        dvorak = {0: "a", 9: "k", 47: "v"}
        self.assertEqual(keys.pick_keycodes(dvorak, "av"), {"a": 0, "v": 47})

    def test_no_latin_letter_or_no_table_falls_back_to_ansi(self):
        self.assertEqual(keys.pick_keycodes({0: "ф", 9: "м"}, "av"), {"a": 0, "v": 9})
        self.assertEqual(keys.pick_keycodes({}, "av"), keys.ANSI)

    def test_uppercase_and_several_keys(self):
        self.assertEqual(keys.pick_keycodes({30: "A", 12: "a"}, "a"), {"a": 12})   # the lowest code that types it

    def test_the_driver_presses_the_layouts_codes(self):
        saved = (PeekabooDriver._key_blobs, PeekabooDriver._letter_codes)
        try:
            PeekabooDriver._key_blobs, PeekabooDriver._letter_codes = {"loaded": b""}, {"a": 12, "v": 9}
            self.assertEqual((PeekabooDriver._keycode(None, "a"), PeekabooDriver._keycode(None, "v")), (12, 9))
            PeekabooDriver._letter_codes = {}
            self.assertEqual(PeekabooDriver._keycode(None, "a"), 0)                  # not read: ANSI
        finally:
            PeekabooDriver._key_blobs, PeekabooDriver._letter_codes = saved


def _child(layout: str) -> dict[str, int]:
    r = subprocess.run([sys.executable, "-c", keys.CHILD, layout], capture_output=True, text=True, timeout=30)
    return keys.pick_keycodes({int(k): v for k, v in json.loads(r.stdout)["table"].items()}, "av")


@unittest.skipUnless(sys.platform == "darwin" and subprocess.run([sys.executable, "-c", "import Quartz"],
                                                                  capture_output=True).returncode == 0,
                     "needs macOS and pyobjc (the app's runtime has both)")
class RealLayouts(unittest.TestCase):
    """The child reads Apple's own layout data. A named layout is read without switching the user's keyboard."""

    def test_layouts(self):
        self.assertEqual(_child("com.apple.keylayout.French"), {"a": 12, "v": 9})
        self.assertEqual(_child("com.apple.keylayout.Dvorak"), {"a": 0, "v": 47})
        self.assertEqual(_child("com.apple.keylayout.DVORAK-QWERTYCMD"), {"a": 0, "v": 9})   # QWERTY with cmd
        self.assertEqual(_child("com.apple.keylayout.ABC"), {"a": 0, "v": 9})


if __name__ == "__main__":
    unittest.main()
