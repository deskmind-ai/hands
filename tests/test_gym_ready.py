"""gym readiness (deskmind#64): a run starts only once GymHost's window has a real frame and the task's list rows are
rows in its accessibility tree; otherwise it fails as environment, not as the model's failure."""
import unittest

from tools.gym import mail, mailmusic, settings
from tools.gym import run as gym_run

# The two hosts deskmind#64 compared: the same mail page on the main display and on a virtual one.
MAIN = [{"title": "Mailora", "w": 1100.0, "h": 792.0, "elements": 60, "rows": 8}]
VDISPLAY = [{"title": "Mailora", "w": 1.0, "h": 573.0, "elements": 60, "rows": 0}]


class Readiness(unittest.TestCase):
    def test_a_normal_host_is_ready(self):
        self.assertIsNone(gym_run.readiness(MAIN, rows=8))

    def test_the_one_point_wide_window_is_not(self):
        why = gym_run.readiness(VDISPLAY, rows=8)
        self.assertIn("1 x 573 pt", why)

    def test_rows_that_lost_their_role_are_not(self):
        lost = [dict(MAIN[0], rows=0)]   # a real frame, but the rows came through as role "other"
        self.assertIn("0 of the page's 8 list rows", gym_run.readiness(lost, rows=8))

    def test_no_window_or_no_tree(self):
        self.assertEqual(gym_run.readiness(None), "GymHost has no window")
        self.assertIn("did not come up", gym_run.readiness([dict(MAIN[0], elements=6, rows=0)]))

    def test_a_page_without_a_list_needs_no_rows(self):
        self.assertIsNone(gym_run.readiness([dict(MAIN[0], rows=0)]))

    def test_mail_tasks_wait_for_every_message_row(self):
        self.assertEqual(gym_run.list_rows(mail.make_task(3)), 8)
        self.assertEqual(gym_run.list_rows(mailmusic.make_task(3)), 8)
        self.assertEqual(gym_run.list_rows(settings.make_task(3)), 0)


class WaitReady(unittest.TestCase):
    def clock(self):
        self.now = 0.0

        def tick(s):
            self.now += s
        return (lambda: self.now), tick

    def test_polls_until_the_rows_come_up(self):
        clock, sleep = self.clock()
        seen = iter([None, [dict(MAIN[0], elements=6, rows=0)], [dict(MAIN[0], rows=3)], MAIN])
        windows, why = gym_run.wait_ready(8, timeout=15, windows=lambda: next(seen), clock=clock, sleep=sleep)
        self.assertIsNone(why)
        self.assertEqual(windows, MAIN)

    def test_gives_up_with_the_reason_when_it_never_comes(self):
        clock, sleep = self.clock()
        windows, why = gym_run.wait_ready(8, timeout=3, windows=lambda: VDISPLAY, clock=clock, sleep=sleep)
        self.assertEqual(windows, VDISPLAY)
        self.assertIn("1 x 573 pt", why)
        self.assertGreaterEqual(self.now, 3)


if __name__ == "__main__":
    unittest.main()
