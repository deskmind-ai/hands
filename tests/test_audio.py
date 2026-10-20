"""The apps sending sound now, from CoreAudio's process objects, each attributed to the application that owns it."""
from __future__ import annotations

import unittest
from unittest import mock

from deskmind_hands import audio


class Playing(unittest.TestCase):
    def test_a_helper_process_is_attributed_to_its_application_and_each_app_listed_once(self):
        owners = {501: ("com.netease.163music", "网易云音乐"), 502: ("com.netease.163music", "网易云音乐"), 600: None}
        with mock.patch.object(audio, "output_pids", return_value=[501, 502, 600]), \
             mock.patch.object(audio, "_app_of", side_effect=lambda pid: owners[pid]):
            self.assertEqual(audio.playing_apps(), [{"bundle": "com.netease.163music", "app_name": "网易云音乐"}])

    def test_nothing_readable_is_nothing_playing(self):
        with mock.patch.object(audio, "output_pids", return_value=[]):
            self.assertEqual(audio.playing_apps(), [])


if __name__ == "__main__":
    unittest.main()
