"""The numbers added to the debug log, per thud and per beep pulse, and the replay tool that reads that log.
With debug off none of it is kept. Numbers in, numbers out; no ffmpeg."""
import importlib.util
import os
import shutil
import tempfile
import unittest

import _pkg

_pkg.load()
from signal_dog import detect, graph, listener  # noqa: E402

TOOLS = _pkg.ROOT / "tools"


def load_tool(name):
    """A tool from tools/, told to use the package the tests already loaded rather than load a second copy."""
    spec = importlib.util.spec_from_file_location("tool_" + name, TOOLS / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.load = _pkg.load
    if hasattr(mod, "replay_pulses"):
        mod.replay_pulses.load = _pkg.load
    return mod


def quiet():
    return [1.0] * graph.CHANNELS


def feed_all(det, frames):
    events = []
    for f in frames:
        events += det.feed(f)
    return events


# ---- the thud numbers -------------------------------------------------------------------------------------------

def thud_signal(blocks, lead=100, tail=60):
    """blocks: the low-band energies (nine numbers) of each frame of the thud; the room is 1.0 everywhere else."""
    frames = [quiet() for _ in range(lead)]
    for low in blocks:
        f = quiet()
        f[:graph.N_LOW] = low
        frames.append(f)
    return frames + [quiet() for _ in range(tail)]


def uneven(g1, g2, g3):
    """Nine low-band energies: four bands at g1 (70 to 172 Hz), three at g2 (232 to 420), two at g3 (565 to 760)."""
    return [g1] * 4 + [g2] * 3 + [g3] * 2


class ThudNumberTests(unittest.TestCase):
    def thud(self, blocks):
        det = detect.Detector(detect.Config(debug=True))
        feed_all(det, thud_signal(blocks))
        self.assertEqual(len(det.knock.dlog), 1)
        return det.knock.dlog[0]

    def test_a_clean_thud_has_its_attack_fall_and_shares(self):
        row = self.thud([uneven(100.0, 50.0, 25.0)] * 5)
        self.assertEqual(len(row), 11)                     # the six old fields, then five new ones at the end
        attack, fall, lo, mid, top = row[6:]
        # The smoothed trace (a mean of three frames) climbs 6, 12, 18 dB, holds, and falls 12, 6. The room's usual level
        # creeps up while the thud lasts, so the peak is the first frame of the hold. 10 dB under it is passed
        # 2 frames before the peak and 4 frames after it.
        self.assertEqual(attack, 2)
        self.assertEqual(fall, 4)
        # energy over the room: 4 bands of 99, 3 of 49, 2 of 24 -> 396 : 147 : 48 of 591
        self.assertAlmostEqual(lo, 67.0, delta=0.2)
        self.assertAlmostEqual(mid, 24.9, delta=0.2)
        self.assertAlmostEqual(top, 8.1, delta=0.2)
        self.assertAlmostEqual(lo + mid + top, 100.0, delta=0.2)

    def test_the_old_fields_are_unchanged_and_come_first(self):
        row = self.thud([uneven(100.0, 50.0, 25.0)] * 5)
        tt, level, high, worst, near, sharp = row[:6]
        self.assertAlmostEqual(level, 18.2, delta=0.1)
        self.assertAlmostEqual(high, 0.0, delta=0.1)
        self.assertIs(sharp, False)

    def test_a_thud_that_has_not_fallen_when_it_is_decided_has_no_fall(self):
        # one loud frame, then a long shoulder only 1.8 dB lower: still within 10 dB of the peak twelve frames on
        row = self.thud([[300.0] * 9] + [[200.0] * 9] * 30)
        self.assertEqual(row[6], 2)
        self.assertIsNone(row[7])

    def test_the_shares_follow_where_the_energy_is(self):
        high_part = self.thud([uneven(1.0, 1.0, 300.0)] * 5)
        self.assertGreater(high_part[10], 95.0)
        low_part = self.thud([uneven(300.0, 1.0, 1.0)] * 5)
        self.assertGreater(low_part[8], 95.0)

    def test_a_thud_with_no_frame_history_gets_none_not_a_guess(self):
        k = detect._Knock(detect.Config(debug=True))
        self.assertEqual(k._shape_of(50, 20.0), (None, None, None, None, None))


class ThudStateTests(unittest.TestCase):
    def test_debug_off_keeps_nothing_extra(self):
        det = detect.Detector()
        feed_all(det, thud_signal([uneven(100.0, 50.0, 25.0)] * 5))
        self.assertIsNone(det.knock.shape)
        self.assertIsNone(det.beep.recent)
        self.assertEqual(det.knock.dlog, [])
        self.assertEqual(det.beep.log, [])
        self.assertEqual(det.beep.rise, [None] * graph.N_TONE)

    def test_debug_on_keeps_only_a_short_window(self):
        det = detect.Detector(detect.Config(debug=True))
        feed_all(det, [quiet() for _ in range(3000)])
        self.assertLessEqual(len(det.knock.shape), 40)
        self.assertLessEqual(len(det.beep.recent), 20)

    def test_the_numbers_do_not_change_what_is_decided(self):
        frames = thud_signal([uneven(300.0, 300.0, 300.0)] * 5, tail=300)
        plain, debug = detect.Detector(), detect.Detector(detect.Config(debug=True))
        self.assertEqual(feed_all(plain, frames), feed_all(debug, frames))

    def test_adopt_works_with_debug_on(self):
        old, new = detect.Detector(detect.Config(debug=True)), detect.Detector(detect.Config(debug=True))
        feed_all(old, [quiet() for _ in range(400)])
        feed_all(new, [quiet() for _ in range(200)])
        self.assertEqual(new.adopt(old)["offset_frames"], -200)         # this one started later


# ---- the pulse numbers ------------------------------------------------------------------------------------------

BAND = 30


def pulse_signal(rises, start=25, steps=5, tail=40):
    """One pulse in tone band 30 from step `start`. If `rises`, the rest of the room comes up with it: the tone bands
    more than three bands away by 10 dB and the nine low bands by 20 dB."""
    frames = []
    for i in range((start + steps + tail) * 4):
        f = quiet()
        if start * 4 <= i < (start + steps) * 4:
            f[graph.IDX_TONE + BAND] = 1000.0
            if rises:
                for j in range(graph.N_TONE):
                    if abs(j - BAND) > 3:
                        f[graph.IDX_TONE + j] = 10.0
                for k in range(graph.N_LOW):
                    f[k] = 100.0
        frames.append(f)
    return frames


class PulseNumberTests(unittest.TestCase):
    def pulses(self, frames):
        det = detect.Detector(detect.Config(debug=True))
        feed_all(det, frames)
        return det.beep.log

    def test_a_lone_tone_moves_nothing_else(self):
        log = self.pulses(pulse_signal(rises=False))
        self.assertEqual(len(log), 1)
        hz, start, steps, prom, lit, rise_far, rise_low = log[0]
        self.assertEqual((hz, start, steps), (graph.TONE_BANDS[BAND], 1.0, 5))
        self.assertEqual((rise_far, rise_low), (0.0, 0.0))

    def test_a_pulse_that_lifts_the_whole_room_says_so(self):
        log = self.pulses(pulse_signal(rises=True))
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0][5:], (10.0, 20.0))

    def test_a_pulse_in_the_first_half_second_has_no_rise_because_there_is_no_before(self):
        log = self.pulses(pulse_signal(rises=True, start=3, steps=5))
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0][5:], (None, None))

    def test_each_pulse_has_its_own_rise(self):
        two = pulse_signal(rises=True, start=25, steps=5, tail=40)
        second = pulse_signal(rises=False, start=25, steps=5, tail=40)
        log = self.pulses(two + second)
        self.assertEqual([r[5:] for r in log], [(10.0, 20.0), (0.0, 0.0)])


# ---- the log lines and the tools that read them -----------------------------------------------------------------

OLD_THUD = "2026-11-07T15:05:30+1100 thud frame=10867 low_db=22.5 high_db=1.2 worst_high_minus_low=-8.1 near_high_db=7.1 sharp=True"
OLD_PULSE = "2026-11-06T21:50:31+1100 pulse hz=2408 start_s=12.4 steps=5 prom_min_db=12.3 bands_lit=2"


class LogLineTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.thuds, self.pulses = load_tool("replay_thuds"), load_tool("replay_pulses")

    def flushed(self):
        path = os.path.join(self.dir, "e.log")
        ln = listener.Listener("default", lambda ev: None, log_path=path, cfg=detect.Config(debug=True))
        det = detect.Detector(detect.Config(debug=True))
        feed_all(det, thud_signal([uneven(100.0, 50.0, 25.0)] * 5, tail=63))       # 168 frames: whole steps of 4
        feed_all(det, pulse_signal(rises=True))
        ln._flush_debug(det)
        lines = open(path, encoding="utf-8").read().splitlines()
        return next(x for x in lines if " thud " in x), next(x for x in lines if " pulse " in x)

    def test_the_new_fields_are_at_the_end_of_each_line(self):
        thud, pulse = self.flushed()
        self.assertRegex(thud, r" sharp=False attack=2 fall=4 share_70_172=[\d.]+ share_232_420=[\d.]+ share_565_760=[\d.]+$")
        self.assertRegex(pulse, r" bands_lit=\d+ rise_far=10.0 rise_low=20.0$")

    def test_the_replay_tools_read_old_and_new_lines_alike(self):
        thud, pulse = self.flushed()
        for line in (OLD_THUD, thud):
            m = self.thuds.THUD.search(line)
            self.assertIsNotNone(m)
        self.assertEqual(self.thuds.THUD.search(OLD_THUD).group(1), "10867")
        self.assertEqual(self.thuds.THUD.search(thud).group(2), "18.2")
        for line in (OLD_PULSE, pulse):
            self.assertIsNotNone(self.pulses.PULSE.search(line))
        self.assertEqual(self.pulses.PULSE.search(OLD_PULSE).group(4), "12.3")
        new = self.pulses.PULSE.search(pulse)
        self.assertEqual((new.group(1), new.group(3)), (str(graph.TONE_BANDS[BAND]), "5"))
        self.assertIsNotNone(new.group(4))


# ---- the replay of thuds with the beep gate ---------------------------------------------------------------------

def thud_line(frame, sec):
    return ("2026-11-07T10:00:%02d+1100 thud frame=%d low_db=14.0 high_db=0.0 worst_high_minus_low=-11.0 "
            "near_high_db=0.5 sharp=False" % (sec, frame))


def noise_lines():
    """Thirty pulses 0.4 s apart in far-apart bands (the NOISE of the knock tests), then nothing."""
    out = []
    for i in range(30):
        band = (i // 2) if i % 2 == 0 else (53 - i // 2)
        out.append("2026-11-07T10:00:%02d+1100 pulse hz=%d start_s=%.2f steps=5 prom_min_db=12.0 bands_lit=1" % (
            i // 2, graph.TONE_BANDS[band], 1.0 + i * 0.4))
    return out


class ReplayGateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.rt = load_tool("replay_thuds")

    def log(self, lines):
        path = os.path.join(self.dir, "e.log")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return path

    def alerts(self, lines, gate=True, first="", last="~"):
        segs = self.rt.read_log(self.log(lines), first, last)
        cfg = detect.Config()
        return [a for s in segs for a in self.rt.replay(s["rows"], cfg, s["pulses"] if gate else None)]

    PAIR = [thud_line(1500, 15), thud_line(1525, 15)]       # two dull thuds 250 ms apart

    def test_the_logged_rise_decides_a_pair_and_an_old_line_without_it_passes(self):
        start = ["2026-11-07T10:00:00+1100 start source=default"]
        with_rise = lambda a, b: [self.PAIR[0] + " attack=%s fall=6" % a, self.PAIR[1] + " attack=%s fall=6" % b]
        self.assertEqual(len(self.alerts(start + with_rise(3, 4))), 1)
        self.assertEqual(self.alerts(start + with_rise(14, 20)), [])
        self.assertEqual(len(self.alerts(start + with_rise("None", "None"))), 1)
        self.assertEqual(len(self.alerts(start + self.PAIR)), 1)

    def test_two_dull_thuds_alert_when_the_log_has_no_busy_room(self):
        self.assertEqual(len(self.alerts(["2026-11-07T10:00:00+1100 start source=default"] + self.PAIR)), 1)

    def test_the_same_two_thuds_are_held_back_by_the_pulses_before_them(self):
        lines = ["2026-11-07T10:00:00+1100 start source=default"] + noise_lines() + self.PAIR
        self.assertEqual(self.alerts(lines), [])
        self.assertEqual(len(self.alerts(lines, gate=False)), 1)

    def test_pulses_in_the_log_before_the_first_wanted_line_still_count(self):
        lines = ["2026-11-07T10:00:00+1100 start source=default"] + noise_lines() + self.PAIR
        self.assertEqual(self.alerts(lines, first="2026-11-07T10:00:15"), [])

    def test_pulses_of_an_earlier_life_of_the_detector_do_not_count(self):
        lines = (["2026-11-07T10:00:00+1100 start source=default"] + noise_lines() +
                 ["2026-11-07T10:00:14+1100 start source=default"] + self.PAIR)
        self.assertEqual(len(self.alerts(lines)), 1)

    def test_a_new_life_that_starts_at_lower_frame_numbers_keeps_its_own_pulses(self):
        lines = (["2026-11-07T10:00:00+1100 start source=default", thud_line(5000, 0),
                  "2026-11-07T10:00:01+1100 recycle done waited_for_idle=0.0s"] + noise_lines() + self.PAIR)
        self.assertEqual(self.alerts(lines), [])

    def test_the_pair_decided_before_the_room_is_busy_is_not_held_back(self):
        early = [thud_line(60, 1), thud_line(85, 1)]          # at 0.6 s: no pulses yet
        lines = ["2026-11-07T10:00:00+1100 start source=default"] + early + noise_lines()
        self.assertEqual(len(self.alerts(lines)), 1)

    def test_reading_stops_after_the_last_wanted_time(self):
        later = ["2026-11-07T10:00:40+1100 thud frame=not-a-number"]    # would fail to read if it were read at all
        lines = ["2026-11-07T10:00:00+1100 start source=default"] + self.PAIR + later
        self.assertEqual(len(self.alerts(lines, last="2026-11-07T10:00:30")), 1)

    def test_a_route_a_knock_is_not_held_back(self):
        loud = ["2026-11-07T10:00:%02d+1100 thud frame=%d low_db=26.0 high_db=0.0 worst_high_minus_low=-12.0 "
                "near_high_db=0.5 sharp=False" % (15, f) for f in (1500, 1525, 1550)]
        lines = ["2026-11-07T10:00:00+1100 start source=default"] + noise_lines() + loud
        self.assertEqual(len(self.alerts(lines)), 1)


if __name__ == "__main__":
    unittest.main()
