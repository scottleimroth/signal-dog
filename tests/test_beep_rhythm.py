"""The repeat rule for beeps: three pulses of ONE band, evenly spaced. Numbers in, events out; no ffmpeg."""
import unittest

import _pkg

_pkg.load()
from signal_dog import detect, graph  # noqa: E402

LEAD = 25          # steps (one second) of quiet to learn the room
BAND = 30


def frame(tone=None):
    f = [1.0] * graph.CHANNELS
    for k, v in (tone or {}).items():
        f[graph.IDX_TONE + k] = v
    return f


def signal(pulses, tail=60, lead=LEAD):
    """pulses: (band, start step, length in steps), steps counted after the quiet lead. 4 frames make a step."""
    last = max(p[1] + p[2] for p in pulses) if pulses else 0
    n = (lead + last + tail) * 4
    on = {}
    for band, start, length in pulses:
        for i in range((lead + start) * 4, (lead + start + length) * 4):
            on.setdefault(i, {})[band] = 100.0
    return [frame(on.get(i)) for i in range(n)]


def run(pulses, tail=60, cfg=None):
    det = detect.Detector(cfg or detect.Config())
    ev = []
    for f in signal(pulses, tail):
        ev += det.feed(f)
    return [e for e in ev if e["kind"] == "beep"], det


def train(starts, length=5, band=BAND):
    return [(band, s, length) for s in starts]


class RhythmTests(unittest.TestCase):
    def test_three_evenly_spaced_pulses_alert_once(self):
        ev, _ = run(train([0, 10, 20]))
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["pulses"], 3)
        self.assertEqual(ev[0]["hz"], graph.TONE_BANDS[BAND])

    def test_two_pulses_are_not_enough(self):
        self.assertEqual(run(train([0, 10]))[0], [])

    def test_unevenly_spaced_pulses_do_not_alert(self):
        self.assertEqual(run(train([0, 10, 30]))[0], [])

    def test_one_sound_leaking_across_bands_is_one_pulse_not_three_beeps(self):
        leak = [(BAND + d, 0, 5) for d in (-1, 0, 1)]
        self.assertEqual(run(leak)[0], [])

    def test_pulses_that_jump_between_neighbouring_bands_do_not_count_together(self):
        jumping = [(BAND, 0, 5), (BAND + 1, 10, 5), (BAND + 2, 20, 5)]
        self.assertEqual(run(jumping)[0], [])

    def test_a_stray_pulse_in_the_band_does_not_break_the_pattern(self):
        ev, _ = run(train([0, 10, 14, 20]))
        self.assertEqual(len(ev), 1)

    def test_double_beep_repeating_is_caught_on_the_third_cycle(self):
        ev, _ = run(train([0, 8, 25, 33, 50, 58], length=4))
        self.assertEqual(len(ev), 1)

    def test_gap_difference_of_two_steps_passes_and_three_does_not(self):
        self.assertEqual(len(run(train([0, 10, 22]))[0]), 1)
        self.assertEqual(run(train([0, 10, 23]))[0], [])

    def test_slow_patterns_get_no_percentage_allowance(self):
        # a machine keeps time to a step or two at any speed; a slow 12 percent drift is chance, not a machine
        self.assertEqual(len(run(train([0, 80, 160]))[0]), 1)
        self.assertEqual(run(train([0, 80, 172]))[0], [])       # gaps 80 and 92: difference 12

    def test_window_of_eight_seconds(self):
        self.assertEqual(len(run(train([0, 90, 180]))[0]), 1)   # first to last start 180 steps
        self.assertEqual(run(train([0, 110, 220]))[0], [])      # 220 steps: more than 200

    def test_an_old_pulse_stuck_behind_a_newer_one_is_still_out_of_the_window(self):
        # the queue is kept in order of when pulses ended, so a long old pulse can sit behind a short newer one of
        # another band and not be dropped by the queue itself; the rule must still leave it out of the 8 seconds
        stuck = [(BAND, 0, 25), (BAND + 20, 15, 4)] + train([105, 210])
        self.assertEqual(run(stuck)[0], [])

    def test_two_pitches_far_apart_sounding_together_are_one_message(self):
        both = train([0, 10, 20], band=10) + train([0, 10, 20], band=40)
        self.assertEqual(len(run(both)[0]), 1)

    def test_a_second_pitch_after_the_ten_second_hold_is_a_new_message(self):
        later = train([0, 10, 20], band=10) + train([300, 310, 320], band=40)
        self.assertEqual(len(run(later, tail=100)[0]), 2)

    def test_a_second_pitch_inside_the_ten_second_hold_is_held_back(self):
        near = train([0, 10, 20], band=10) + train([200, 210, 220], band=40)   # 8 s later
        self.assertEqual(len(run(near, tail=100)[0]), 1)

    def test_long_tone_still_warns_inside_the_repeat_hold(self):
        mixed = train([0, 10, 20], band=10) + [(40, 15, 90)]
        ev, _ = run(mixed, tail=100)
        self.assertEqual(sorted(e["pulses"] > 0 for e in ev), [False, True])

    def test_same_pitch_stays_quiet_for_a_minute_after_an_alert(self):
        ev, _ = run(train([0, 10, 20, 400, 410, 420]), tail=100)
        self.assertEqual(len(ev), 1)

class BusyRoomTests(unittest.TestCase):
    """A television throws up evenly spaced pulses by chance, so a busy room must show more than three."""
    # 30 single pulses 0.4 s apart, alternating between low pitches (bands 0 to 14) and high ones (bands 53 down to 39),
    # all far from the tone's band 30, and no band pulsing twice, so the noise alerts on nothing itself
    BUSY = [((i // 2) if i % 2 == 0 else (53 - i // 2), i * 10, 5) for i in range(30)]
    LAST_NOISE = 29 * 10 + 5                              # step where the last of them ends
    SET_AT = 30 * 10 + 10                                 # the tone starts just after

    @staticmethod
    def T(starts, band=BAND, length=7):
        """Beeps as long as a real machine's: 7 steps, 280 ms."""
        return train(starts, length=length, band=band)

    def room(self, tone, tail=100, busy=True, extra=()):
        return run((self.BUSY if busy else []) + list(extra) + tone, tail=tail)[0]

    def test_the_room_is_busy_enough_to_hold_back_three_pulses(self):
        self.assertEqual(self.room(self.T([self.SET_AT + s for s in (0, 10, 20)])), [])

    def test_the_same_three_pulses_in_a_quiet_room_alert(self):
        self.assertEqual(len(self.room(self.T([self.SET_AT + s for s in (0, 10, 20)]), busy=False)), 1)

    def test_a_run_of_five_gets_through_a_busy_room(self):
        self.assertEqual(len(self.room(self.T([self.SET_AT + s for s in (0, 10, 20, 30, 40)]))), 1)

    def test_a_run_of_four_does_not(self):
        self.assertEqual(self.room(self.T([self.SET_AT + s for s in (0, 10, 20, 30)])), [])
        self.assertEqual(len(self.room(self.T([self.SET_AT + s for s in (0, 10, 20, 30)]), busy=False)), 1)

    def test_a_second_separate_set_gets_through_a_busy_room(self):
        ev = self.room(self.T([self.SET_AT + s for s in (0, 10, 20, 74, 84, 94)]))
        self.assertEqual(len(ev), 1)

    def test_a_second_set_that_is_a_different_pitch_does_not(self):
        two = self.T([self.SET_AT + s for s in (0, 10, 20)]) + self.T([self.SET_AT + s for s in (74, 84, 94)], band=10)
        self.assertEqual(self.room(two), [])

    def test_a_second_set_of_a_different_period_does_not(self):
        two = self.T([self.SET_AT + s for s in (0, 10, 20)]) + self.T([self.SET_AT + s for s in (74, 88, 102)])
        self.assertEqual(self.room(two), [])

    def test_a_second_set_more_than_two_minutes_later_does_not(self):
        # the room is kept busy by more noise just before the second set, which comes 3090 steps after the first ends
        later = [(b, s + 3100, n) for b, s, n in self.BUSY]
        two = self.T([self.SET_AT + s for s in (0, 10, 20)]) + self.T([3400, 3410, 3420])
        self.assertEqual(self.room(two, extra=later), [])
        near = self.T([self.SET_AT + s for s in (0, 10, 20)]) + self.T([3300, 3310, 3320])      # 2955 steps after: inside
        self.assertEqual(len(self.room(near, extra=[(b, s + 3000, n) for b, s, n in self.BUSY])), 1)

    def test_slow_repeats_do_not_count_in_a_busy_room(self):
        slow = self.T([self.SET_AT + s for s in (0, 70, 140, 210)])          # a pulse every 2.8 s
        self.assertEqual(self.room(slow), [])
        self.assertEqual(len(self.room(slow, busy=False)), 1)               # a quiet room hears it

    def test_two_slow_sets_do_not_count_in_a_busy_room_but_do_when_quick(self):
        slow = self.T([self.SET_AT + s for s in (0, 70, 140, 400, 470, 540)])    # period 2.8 s, both sets
        self.assertEqual(self.room(slow), [])
        quick = self.T([self.SET_AT + s for s in (0, 50, 100, 400, 450, 500)])   # period 2.0 s, inside 2.4 s
        self.assertEqual(len(self.room(quick)), 1)

    def test_the_room_stays_busy_for_two_minutes_after_the_noise(self):
        n = self.LAST_NOISE
        self.assertEqual(self.room(self.T([n + 2900 + s for s in (0, 10, 20)])), [])
        self.assertEqual(len(self.room(self.T([n + 3200 + s for s in (0, 10, 20)]))), 1)

    def test_pulses_near_the_tone_do_not_make_the_room_busy(self):
        # thirty pulses, but all within three bands of the tone: they are the tone's own neighbourhood, not the room
        near = [(BAND - 3 + (i * 3) % 7, i * 10 + (i * i * 7) % 6, 5) for i in range(30)]
        _, det = run(near + self.T([30 * 10 + 10 + s for s in (0, 10, 20)]), tail=100)
        self.assertEqual(det.beep.busy_until, -1)
        far = [(BAND + 6 + (i * 3) % 7, i * 10 + (i * i * 7) % 6, 5) for i in range(30)]      # four or more bands away
        _, det = run(far + self.T([30 * 10 + 10 + s for s in (0, 10, 20)]), tail=100)
        self.assertGreater(det.beep.busy_until, 0)

    def lengths(self, starts, lengths, band=BAND):
        return [(band, s, n) for s, n in zip(starts, lengths)]

    def test_in_a_busy_room_a_set_of_short_beeps_is_a_television_not_a_machine(self):
        at = [self.SET_AT + s for s in (0, 10, 20, 400, 410, 420)]
        self.assertEqual(self.room(self.lengths(at, [7, 7, 7, 7, 7, 7])).__len__(), 1)
        self.assertEqual(self.room(self.lengths(at, [5, 5, 5, 5, 5, 5])), [])
        self.assertEqual(self.room(self.lengths(at, [7, 7, 5, 7, 7, 5])).__len__(), 1)     # one short beep a set is allowed
        self.assertEqual(self.room(self.lengths(at, [7, 5, 5, 7, 7, 7])), [])             # two are not, in the first set
        self.assertEqual(self.room(self.lengths(at, [7, 7, 7, 7, 5, 5])), [])              # nor in the second

    def test_a_run_of_five_with_two_short_beeps_does_not_alert(self):
        at = [self.SET_AT + s for s in (0, 10, 20, 30, 40)]
        self.assertEqual(len(self.room(self.lengths(at, [7, 7, 7, 7, 5]))), 1)
        self.assertEqual(self.room(self.lengths(at, [7, 5, 7, 5, 7])), [])

    def test_six_steps_is_long_enough_and_five_is_short(self):
        at = [self.SET_AT + s for s in (0, 10, 20, 30, 40)]
        self.assertEqual(len(self.room(self.lengths(at, [6, 6, 6, 6, 6]))), 1)
        self.assertEqual(self.room(self.lengths(at, [5, 5, 6, 6, 6])), [])

    def test_a_quiet_room_takes_short_beeps(self):
        self.assertEqual(len(self.room(train([self.SET_AT + s for s in (0, 10, 20)], length=4), busy=False)), 1)

    def test_an_alert_from_a_busy_room_says_so_and_a_quiet_one_does_not(self):
        busy = self.room(self.T([self.SET_AT + s for s in (0, 10, 20, 30, 40)]))
        quiet = self.room(self.T([self.SET_AT + s for s in (0, 10, 20)]), busy=False)
        self.assertTrue(busy[0]["busy"])
        self.assertFalse(quiet[0]["busy"])
        import signal_dog
        self.assertIn("room is noisy", signal_dog.caption(busy[0]))
        self.assertNotIn("noisy", signal_dog.caption(quiet[0]))

    def test_a_long_tone_alert_never_carries_the_noisy_room_sentence(self):
        import signal_dog
        tone = [(BAND, 0, 90)]
        ev, _ = run(self.BUSY + [(b, s + 300, n) for b, s, n in tone], tail=100)
        sustained = [e for e in ev if e["pulses"] == 0]
        self.assertEqual(len(sustained), 1)
        self.assertNotIn("noisy", signal_dog.caption(sustained[0]))


class BeepStateTests(unittest.TestCase):
    def test_debug_pulse_log_carries_prominence_and_bands_lit(self):
        _, det = run(train([0, 10, 20]), cfg=detect.Config(debug=True))
        self.assertTrue(det.beep.log)
        hz, start, steps, prom, lit = det.beep.log[0][:5]
        self.assertEqual(hz, graph.TONE_BANDS[BAND])
        self.assertGreaterEqual(prom, 8.0)
        self.assertGreaterEqual(lit, 0)


if __name__ == "__main__":
    unittest.main()
