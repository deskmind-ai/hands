"""The driver's side of the files receipt: the digest is of the content written, the scratch is never reported, and
the command-key path keeps a scripted save's receipt (PR #48 review). unittest, as CI collects it."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from deskmind_hands.actions import Action, ActionKind, Binding
from deskmind_hands.drivers.base import ExecResult
from deskmind_hands.drivers.peekaboo import PeekabooDriver


def _driver(ws: Path):
    d = PeekabooDriver.__new__(PeekabooDriver)
    d.app, d.ws, d._snapshot = "com.apple.TextEdit", ws, "s1"
    return d


class Receipt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_receipt_digests_what_was_written_not_what_is_on_disk_later(self):
        d = _driver(self.ws)
        f = self.ws / "hello.txt"
        f.write_bytes(b"hello")
        self.assertEqual(d._receipt(str(f), b"hello"), ((str(f.resolve()), hashlib.sha256(b"hello").hexdigest()),))
        # without the content, the path alone: never the file read back from disk (PR #16 review, eleventh round)
        self.assertEqual(d._receipt(str(f), None), ((str(f.resolve()), None),))
        # a sync program rewrote it before the receipt: the digest of what the driver wrote no longer matches the file
        f.write_bytes(b"someone else's")
        self.assertNotEqual(d._receipt(str(f), b"hello")[0][1], hashlib.sha256(b"someone else's").hexdigest())
        # relative, or the driver's own scratch: no receipt
        self.assertEqual(d._receipt("hello.txt", b"x"), ())
        scratch = self.ws / ".hands" / "未命名-1"
        scratch.parent.mkdir()
        scratch.write_bytes(b"")
        self.assertEqual(d._receipt(str(scratch), b""), ())

    def test_the_command_key_path_keeps_the_scripted_saves_receipt(self):
        d = _driver(self.ws)
        d._mcp = object()
        d._chord_repeat = {}
        receipt = ((str(self.ws / "hello.txt"), hashlib.sha256(b"x").hexdigest()),)
        d._scripted_command = lambda chord: ExecResult(True, "saved 'hello.txt' (scripted, in the background)", files=receipt)
        r = d._execute_inner(Action(kind=ActionKind.KEY, keys=("cmd", "s"), binding=Binding()))
        self.assertTrue(r.ok)
        self.assertEqual(r.files, receipt)


if __name__ == "__main__":
    unittest.main()
