"""The knock routes. A: three thuds, any room. B: two dull thuds, quiet room only. Numbers in, events out; no ffmpeg."""
import unittest

import _pkg

_pkg.load()
from signal_dog import detect, graph  # noqa: E402

LEAD = 100          # frames of quiet to learn the room


def frame(low=1.0, high=1.0):
    f = [1.0] * graph.CHANNELS
    for k in range(graph.N_LOW):
        f[k] = low
    f[graph.IDX_HIGH] = high
    return f


def signal(thuds, tail=200):
    """thuds: (frame number after the lead, low energy, high energy), five frames long each."""
    n = LEAD + max(t[0] for t in thuds) + tail
    on = {}
    for at, low, high in thuds:
        for i in range(5):
            on[LEAD + at + i] = (low, high)
    return [frame(*on[i]) if i in on else frame() for i in range(n)]


def run(thuds, tail=200):
    det = detect.Detector()
    ev = []
    for f in signal(thuds, tail):
        ev += det.feed(f)
    return [e for e in ev if e["kind"] == "knock"], det


LOUD = 300.0      # about 25 dB above the room
DULL = 1.0        # nothing in the high band
SHARP = 40.0      # about 16 dB in the high band, still 8 dB under the low band so it counts as a thud, but not dull
# A busy room: four single thuds well apart, each too weak for the three-thud route and not part of any pair.
BUSY = [(100, 40.0, DULL), (350, 40.0, DULL), (600, 40.0, DULL), (850, 40.0, DULL)]
AT = 1100         # a knock comes after the busy thuds


def knock(gaps=(25, 25), low=LOUD, high=DULL, start=AT):
    out, at = [(start, low, high)], start
    for g in gaps:
        at += g
        out.append((at, low, high))
    return out


class QuietRoomTests(unittest.TestCase):
    def test_two_dull_thuds_in_a_quiet_room_alert(self):
        self.assertEqual(len(run(knock((25,), low=40.0, start=100))[0]), 1)

    def test_a_single_thud_does_not(self):
        self.assertEqual(run([(100, LOUD, DULL)])[0], [])

    def test_two_sharp_thuds_do_not(self):
        self.assertEqual(run(knock((25,), high=SHARP, start=100))[0], [])

    def test_the_sharp_limit_is_10_db_in_the_high_band(self):
        # high energy 5 reads about 7 dB near the thud: under 10, so dull; the same pair at 20 (13 dB) is sharp
        self.assertEqual(len(run(knock((25,), low=300.0, high=5.0, start=100))[0]), 1)
        self.assertEqual(run(knock((25,), low=300.0, high=20.0, start=100))[0], [])

    def test_a_bounce_closer_than_150_ms_does_not_break_the_count(self):
        # thud at 0, its bounce 12 frames later, then the second thud at 25: the bounce is ignored
        ev = run([(100, 40.0, DULL), (112, 40.0, DULL), (125, 40.0, DULL)])[0]
        self.assertEqual(len(ev), 1)


class BusyRoomTests(unittest.TestCase):
    def test_four_earlier_thuds_make_the_room_busy_for_two_thud_knocks(self):
        # busy: two soft dull thuds, which a quiet room would take as a knock
        self.assertEqual(run(BUSY + knock((25,), low=40.0))[0], [])
        self.assertEqual(len(run(knock((25,), low=40.0))[0]), 1)

    def test_the_room_is_busy_at_three_earlier_thuds_and_not_at_two(self):
        soft_pair = knock((25,), low=40.0)
        self.assertEqual(len(run(BUSY[:2] + soft_pair)[0]), 1)       # two earlier: still quiet
        self.assertEqual(run(BUSY[:3] + soft_pair)[0], [])           # three earlier: busy

    def test_three_loud_dull_even_thuds_alert_in_a_busy_room(self):
        ev = run(BUSY + knock())[0]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["thuds"], 3)

    def test_three_thuds_whose_gaps_differ_by_more_than_100_ms_do_not(self):
        self.assertEqual(run(BUSY + knock((25, 36)))[0], [])           # difference 11 frames
        self.assertEqual(len(run(BUSY + knock((25, 35)))[0]), 1)       # difference 10 frames: allowed

    def test_gaps_outside_180_to_400_ms_do_not(self):
        self.assertEqual(run(BUSY + knock((15, 15)))[0], [])
        self.assertEqual(run(BUSY + knock((42, 42)))[0], [])

    def test_thuds_of_unlike_strength_do_not(self):
        weak_third = [(AT, LOUD, DULL), (AT + 25, LOUD, DULL), (AT + 50, 20.0, DULL)]
        self.assertEqual(run(BUSY + weak_third)[0], [])

    def test_a_knock_whose_strongest_thud_is_under_20_db_does_not(self):
        self.assertEqual(run(BUSY + knock(low=60.0))[0], [])

    def test_three_sharp_thuds_do_not(self):
        self.assertEqual(run(BUSY + knock(high=SHARP))[0], [])

    def test_the_dull_limit_is_14_db_in_the_high_band_for_the_three_thud_route(self):
        # high energy 20 reads about 13 dB near the thud: under 14, so dull; 40 reads about 16 dB, so it is not
        self.assertEqual(len(run(BUSY + knock(high=20.0))[0]), 1)
        self.assertEqual(run(BUSY + knock(high=40.0))[0], [])
        self.assertEqual(run(BUSY + knock(high=31.0))[0], [])          # about 15 dB: over the limit, so not dull

    def test_one_sharp_thud_among_three_is_allowed(self):
        mixed = [(AT, LOUD, SHARP), (AT + 25, LOUD, DULL), (AT + 50, LOUD, DULL)]
        self.assertEqual(len(run(BUSY + mixed)[0]), 1)

    def test_one_alert_for_a_whole_knocking_session(self):
        again = knock(start=AT) + knock(start=AT + 150)
        self.assertEqual(len(run(BUSY + again)[0]), 1)



# Eight single thuds 2.5 s apart, too weak and too far apart to be a knock themselves: someone moving about nearby.
THUMPING = [(100 + 250 * i, 40.0, DULL) for i in range(8)]
LATE = 2100           # a knock after them, while they are still in the last 30 s


class ThumpingRoomTests(unittest.TestCase):
    """In a room busy with thumps a set of three alerts only as the second set within 20 s."""

    def test_one_set_in_a_thumping_room_does_not_alert_and_a_second_set_does(self):
        self.assertEqual(run(THUMPING + knock(start=LATE))[0], [])
        ev = run(THUMPING + knock(start=LATE) + knock(start=LATE + 800))[0]
        self.assertEqual(len(ev), 1)
        self.assertTrue(ev[0]["busy"])
        self.assertEqual(ev[0]["thuds"], 3)

    def test_a_second_set_more_than_20_s_later_is_a_new_first_set(self):
        self.assertEqual(run(THUMPING + knock(start=LATE) + knock(start=LATE + 2100), tail=200)[0], [])

    def test_the_next_knock_of_the_same_set_is_not_a_second_set(self):
        # four even knocks: the threes (1,2,3) and (2,3,4) are 250 ms apart, the same set
        self.assertEqual(run(THUMPING + knock(gaps=(25, 25, 25), start=LATE))[0], [])

    def test_five_earlier_thuds_are_not_busy_and_six_are(self):
        five = [(100 + 250 * i, 40.0, DULL) for i in range(5)]
        ev = run(five + knock(start=1400))[0]
        self.assertEqual(len(ev), 1)
        self.assertFalse(ev[0]["busy"])
        six = [(100 + 250 * i, 40.0, DULL) for i in range(6)]
        self.assertEqual(run(six + knock(start=1650))[0], [])

    def test_a_quiet_room_knock_is_not_marked_busy(self):
        ev = run(knock(start=100))[0]
        self.assertEqual(len(ev), 1)
        self.assertFalse(ev[0]["busy"])

    def test_a_swap_carries_the_first_set_so_the_second_still_alerts(self):
        old = detect.Detector()
        frames = signal(THUMPING + knock(start=LATE) + knock(start=LATE + 800), tail=200)
        cut = LEAD + LATE + 400                     # after the first set, before the second
        for f in frames[:cut]:
            self.assertEqual([e for e in old.feed(f) if e["kind"] == "knock"], [])
        new = detect.Detector()
        for f in frames[cut - 300:cut]:             # the new one has heard the last 3 s too
            new.feed(f)
        new.adopt(old)
        ev = [e for f in frames[cut:] for e in new.feed(f) if e["kind"] == "knock"]
        self.assertEqual(len(ev), 1)
        self.assertTrue(ev[0]["busy"])


def ramp_signal(thuds, rise_frames, tail=200):
    """Like signal(), but each thud builds up evenly over rise_frames before its five loud frames."""
    n = LEAD + max(t[0] for t in thuds) + tail
    on = {}
    for t in thuds:
        at, low, high = t[:3]
        r = t[3] if len(t) > 3 else rise_frames          # a thud may carry its own build-up
        for i in range(r):
            on[LEAD + at - r + i] = (1.0 + (low - 1.0) * (i + 1) / r, 1.0)
        for i in range(5):
            on[LEAD + at + i] = (low, high)
    return [frame(*on[i]) if i in on else frame() for i in range(n)]


def run_frames(frames):
    det = detect.Detector()
    ev = []
    for f in frames:
        ev += det.feed(f)
    return [e for e in ev if e["kind"] == "knock"]


class StrikeTests(unittest.TestCase):
    """A knock is a strike: its thuds reach their peak at once. Sounds that build up slowly are not knocks."""

    def test_a_pair_that_builds_up_slowly_is_not_a_knock_and_the_same_pair_struck_is(self):
        pair = knock((25,), low=300.0, start=200)
        self.assertEqual(len(run_frames(ramp_signal(pair, 1))), 1)
        self.assertEqual(run_frames(ramp_signal(pair, 20)), [])

    def test_three_slow_thuds_in_a_busy_room_are_not_a_knock(self):
        self.assertEqual(len(run_frames(ramp_signal(BUSY + knock(), 1))), 1)
        self.assertEqual(run_frames(ramp_signal(BUSY + knock(), 20)), [])

    def test_each_thud_keeps_its_own_rise_so_two_slow_ones_spoil_a_set_that_ends_struck(self):
        slow_slow_struck = [(AT, LOUD, DULL, 20), (AT + 25, LOUD, DULL, 20), (AT + 50, LOUD, DULL, 1)]
        struck_struck_slow = [(AT, LOUD, DULL, 1), (AT + 25, LOUD, DULL, 1), (AT + 50, LOUD, DULL, 20)]
        self.assertEqual(run_frames(ramp_signal(BUSY + slow_slow_struck, 1)), [])
        self.assertEqual(len(run_frames(ramp_signal(BUSY + struck_struck_slow, 1))), 1)

    def test_one_unknown_rise_in_three_leaves_the_faster_known_one_to_decide(self):
        k = detect._Knock(detect.Config())
        self.assertTrue(k._struck([None, 3, 30]))
        self.assertFalse(k._struck([None, 12, 30]))

    def test_the_limit_is_five_frames_from_ten_db_under_the_peak(self):
        k = detect._Knock(detect.Config())
        self.assertTrue(k._struck([5, 5, 5]))
        self.assertFalse(k._struck([6, 6, 6]))

    def test_the_middle_of_three_and_the_faster_of_two_decide_and_unknown_passes(self):
        k = detect._Knock(detect.Config())
        self.assertTrue(k._struck([3, 4, 30]))           # one slow thud among three struck ones
        self.assertFalse(k._struck([3, 30, 30]))         # two slow of three
        self.assertTrue(k._struck([3, 30]))              # a pair: the faster one
        self.assertFalse(k._struck([12, 30]))
        self.assertTrue(k._struck([None, None, None]))   # loud room, rise not measurable: passes
        self.assertTrue(k._struck([None, 30]) is False)  # the only known rise is slow
        self.assertTrue(k._struck([None, 2]))

    def test_rise_is_measured_on_the_smoothed_trace_and_unknown_when_history_stays_loud(self):
        k = detect._Knock(detect.Config())
        k.hist.extend([(90, 2.0, 0.0), (95, 20.0, 0.0), (100, 25.0, 0.0)])
        self.assertEqual(k._rise(100, 25.0), 10)        # 2 dB at frame 90 is the last moment 10 dB under 25
        k.hist.clear()
        k.hist.extend([(90, 18.0, 0.0), (100, 25.0, 0.0)])
        self.assertIsNone(k._rise(100, 25.0))

# Thirty single pulses 0.4 s apart in tone bands far from one another: the beep side calls the room busy, and the pulses
# themselves repeat nothing, so they alert on nothing. Frames, from the start of the signal.
NOISE = [(((i // 2) if i % 2 == 0 else (53 - i // 2)), 100 + i * 40, 20) for i in range(30)]
AFTER_NOISE = 1300      # a thud pair here starts after the last pulse (frame 1280)


def run_with_noise(thuds, cfg=None, tail=200, noise=NOISE):
    """Like run(), with tone pulses laid over the same frames."""
    frames = signal(thuds, tail)
    for band, at, n in noise:
        for i in range(at, at + n):
            frames[i][graph.IDX_TONE + band] = 100.0
    det = detect.Detector(cfg)
    ev = []
    for f in frames:
        ev += det.feed(f)
    return [e for e in ev if e["kind"] == "knock"], det


class BeepBusyTests(unittest.TestCase):
    """Route B stays silent while the beep side calls the room busy; route A does not care."""

    SOFT_PAIR = knock((25,), low=40.0, start=AFTER_NOISE)

    def test_two_dull_thuds_are_a_knock_when_the_beep_side_is_quiet(self):
        ev, det = run_with_noise(self.SOFT_PAIR, noise=[])
        self.assertEqual(len(ev), 1)
        self.assertFalse(det._beep_busy())

    def test_the_same_two_thuds_stay_silent_while_the_beep_side_says_busy(self):
        ev, det = run_with_noise(self.SOFT_PAIR)
        self.assertTrue(det._beep_busy())
        self.assertEqual(len(det.knock.thuds), 2)          # not the thud count that is holding it back
        self.assertEqual(ev, [])

    def test_route_b_speaks_again_once_the_beep_side_stops_saying_busy(self):
        cfg = dict(beep_busy_hold=100)                     # busy for 100 steps (4 s) after the last far pulse, not 120 s
        ev, det = run_with_noise(self.SOFT_PAIR, cfg=detect.Config(**cfg))
        self.assertEqual(ev, [])                           # still busy just after the pulses
        later = knock((25,), low=40.0, start=AFTER_NOISE + 500)
        ev, det = run_with_noise(later, cfg=detect.Config(**cfg))
        self.assertGreater(det.beep.busy_until, 0)         # it had been busy ...
        self.assertFalse(det._beep_busy())                 # ... and is not now
        self.assertEqual(len(ev), 1)

    def test_route_a_still_alerts_while_the_beep_side_says_busy(self):
        ev, det = run_with_noise(knock(start=AFTER_NOISE))
        self.assertTrue(det._beep_busy())
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["thuds"], 3)

    def test_a_swap_keeps_route_b_silent_while_the_old_reader_said_busy(self):
        old, new = detect.Detector(), detect.Detector()
        for _ in range(400):
            old.feed(frame())
        for _ in range(200):
            new.feed(frame())
        old.beep.busy_until = old.beep.s + 500
        self.assertFalse(new.knock.beep_busy())
        new.adopt(old)
        self.assertTrue(new.knock.beep_busy())

    def test_a_knock_alone_on_the_knock_side_is_not_held_back(self):
        # a _Knock that nobody has told anything is a quiet room
        k = detect._Knock(detect.Config())
        self.assertFalse(k.beep_busy())


if __name__ == "__main__":
    unittest.main()
