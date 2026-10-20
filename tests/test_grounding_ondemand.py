"""The vision model started on demand: the first grounder request asks the app's helper to start it."""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from unittest import mock

from deskmind_hands import grounding as g


class OnDemand(unittest.TestCase):
    def setUp(self):
        g._EYES_ASKED.update(at=0.0, up=False)

    def tearDown(self):
        g._EYES_ASKED.update(at=0.0, up=False)

    def _helper(self, reply):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "h.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        got = []

        def serve2():
            c, _ = srv.accept()
            data = b""
            while b"\n" not in data:
                data += c.recv(4096)
            got.append(json.loads(data.split(b"\n")[0]))
            c.sendall((json.dumps(reply) + "\n").encode())
            c.close()
            srv.close()
        threading.Thread(target=serve2, daemon=True).start()
        return path, got

    def test_the_helper_is_asked_once_and_its_answer_kept(self):
        path, got = self._helper({"ok": True, "ready": True})
        with mock.patch.dict(os.environ, {"HANDS_HELPER_SOCKET": path}), mock.patch.object(g, "_grounder_listening", return_value=False):
            self.assertTrue(g.ensure_grounder(wait_s=1))
            self.assertTrue(g.ensure_grounder(wait_s=1))          # kept: not asked again
        self.assertEqual(got, [{"op": "eyes", "wait": 1}])

    def test_nothing_is_asked_when_the_grounder_is_already_up_or_there_is_no_app(self):
        with mock.patch.dict(os.environ, {"HANDS_HELPER_SOCKET": "/nonexistent.sock"}), mock.patch.object(g, "_grounder_listening", return_value=True):
            self.assertTrue(g.ensure_grounder())
        g._EYES_ASKED.update(at=0.0, up=False)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HANDS_HELPER_SOCKET", None)
            self.assertTrue(g.ensure_grounder())                   # not started by the app: nothing to do

    def test_a_failed_ask_is_retried_at_most_once_a_minute(self):
        with mock.patch.dict(os.environ, {"HANDS_HELPER_SOCKET": "/nonexistent.sock"}), mock.patch.object(g, "_grounder_listening", return_value=False):
            self.assertFalse(g.ensure_grounder(wait_s=0.1))
            t = g._EYES_ASKED["at"]
            self.assertFalse(g.ensure_grounder(wait_s=0.1))
            self.assertEqual(g._EYES_ASKED["at"], t)              # not asked again within the minute


if __name__ == "__main__":
    unittest.main()
