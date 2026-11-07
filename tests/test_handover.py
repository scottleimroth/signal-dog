"""What a new detector takes over from the one it replaces: only what is older than its own memory."""
import unittest

import _pkg

_pkg.load()
from signal_dog import detect, graph  # noqa: E402
import test_beep_rhythm as B  # noqa: E402

BAND = B.BAND


def feed(det, frames):
    ev = []
    for f in frames:
        ev += det.feed(f)
    return [e for e in ev if e["kind"] == "beep"]


class CarryOverTests(unittest.TestCase):
    def two_sets(self):
        # set one, then set two 16 s later, in the same pitch: inside the one-minute quiet time of the first
        return B.signal(B.train([0, 10, 20, 400, 410, 420]), tail=100)

    def test_the_old_detector_alerts_once_for_the_two_sets(self):
        self.assertEqual(len(feed(detect.Detector(), self.two_sets())), 1)

    def test_a_new_detector_that_missed_set_one_would_alert_again(self):
        frames = self.two_sets()
        self.assertEqual(len(feed(detect.Detector(), frames[4 * 200:])), 1)

    def test_a_new_detector_that_adopted_the_old_one_stays_quiet_for_the_rest_of_the_minute(self):
        frames = self.two_sets()
        old, new = detect.Detector(), detect.Detector()
        self.assertEqual(len(feed(old, frames[:4 * 200])), 1)            # old heard set one and alerted
        feed(new, frames[4 * 200:4 * 330])                               # new started 8 s later, before set two
        moved = new.adopt(old)
        self.assertLess(moved["offset_frames"], 0)
        self.assertEqual(feed(new, frames[4 * 330:]), [])                # set two: still inside the first's minute

    def test_the_ten_second_hold_is_carried_across_pitches(self):
        old, new = detect.Detector(), detect.Detector()
        old.beep.s, new.beep.s = 1000, 300
        old.t, new.t = 4000, 1200
        old.beep.repeat_hold = 1100                                      # 100 steps (4 s) still to run
        new.adopt(old)
        self.assertEqual(new.beep.repeat_hold, 1100 + (300 - 1000))      # 400 on the new clock, 100 ahead of 300

    def test_it_never_goes_backwards(self):
        old, new = detect.Detector(), detect.Detector()
        old.beep.s, new.beep.s = 1000, 300
        new.beep.repeat_hold = 900                                       # the new one already knows of a longer hold
        old.beep.repeat_hold = 1100
        new.adopt(old)
        self.assertEqual(new.beep.repeat_hold, 900)

    def test_a_busy_room_is_carried_but_not_counted_twice(self):
        old, new = detect.Detector(), detect.Detector()
        old.beep.s, new.beep.s = 1000, 300
        old.beep.busy_until = 3900
        new.beep.elsewhere = [(40, 100), (41, 120)]
        new.adopt(old)
        self.assertEqual(new.beep.busy_until, 3900 - 700)
        self.assertEqual(new.beep.elsewhere, [(40, 100), (41, 120)])     # its own count, untouched

    def test_only_sets_older_than_the_new_detector_are_carried(self):
        old, new = detect.Detector(), detect.Detector()
        old.beep.s, new.beep.s = 1000, 300                               # the new one started at old step 700
        old.beep.sets = [(500, 30, 10.0, (480, 490, 500), (7, 7, 6)), (800, 30, 10.0, (780, 790, 800), (7, 7, 6))]
        new.beep.sets = [(100, 30, 10.0, (80, 90, 100), (7, 7, 6))]      # the new one's own copy of the second
        new.adopt(old)
        self.assertEqual(new.beep.sets, [(-200, 30, 10.0, (-220, -210, -200), (7, 7, 6)), (100, 30, 10.0, (80, 90, 100), (7, 7, 6))])

    def test_a_knocking_session_is_carried_and_older_thuds_with_it(self):
        old, new = detect.Detector(), detect.Detector()
        old.t, new.t = 6000, 3000
        old.knock.session_until = 5000
        old.knock.thuds = [(100, 22.0, 1.0), (4000, 22.0, 1.0)]          # the second is newer than the new one's start
        new.knock.thuds = [(1000, 22.0, 1.0)]
        new.adopt(old)
        self.assertEqual(new.knock.session_until, 2000)
        self.assertEqual(new.knock.thuds, [(-2900, 22.0, 1.0), (1000, 22.0, 1.0)])


if __name__ == "__main__":
    unittest.main()
