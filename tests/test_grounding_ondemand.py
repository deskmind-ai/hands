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
        g._EYES_ASKED.update(at=0.0, up=False, seen=0.0)
        g.STOP.clear()

    def tearDown(self):
        g._EYES_ASKED.update(at=0.0, up=False, seen=0.0)
        g.STOP.clear()

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

    def test_the_helper_is_asked_to_start_it_and_it_is_waited_for(self):
        path, got = self._helper({"ok": True, "starting": True})
        up = iter([False, False, True])
        with mock.patch.dict(os.environ, {"HANDS_HELPER_SOCKET": path}), mock.patch.object(g, "_grounder_listening", side_effect=lambda: next(up)):
            self.assertTrue(g.ensure_grounder(wait_s=5))
        self.assertEqual(got, [{"op": "eyes", "wait": 0}])

    def test_a_stop_ends_the_wait_at_once(self):
        path, _ = self._helper({"ok": True, "starting": True})
        g.STOP.set()
        with mock.patch.dict(os.environ, {"HANDS_HELPER_SOCKET": path}), mock.patch.object(g, "_grounder_listening", return_value=False):
            self.assertFalse(g.ensure_grounder(wait_s=30))

    def test_a_model_that_died_is_started_again(self):
        with mock.patch.dict(os.environ, {"HANDS_HELPER_SOCKET": "/nonexistent.sock"}):
            g._EYES_ASKED.update(up=True, seen=0.0)        # seen up long ago
            with mock.patch.object(g, "_grounder_listening", return_value=False):
                g.ensure_grounder(wait_s=0.1)
            self.assertFalse(g._EYES_ASKED["up"])

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
