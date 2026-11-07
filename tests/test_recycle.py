"""The overlapping restart. Streams are scripted, so no ffmpeg is needed and the timing is exact."""
import os
import shutil
import tempfile
import threading
import time
import unittest

import _pkg

_pkg.load()
from signal_dog import detect, graph, listener  # noqa: E402


def frame(low=1.0, high=1.0):
    f = [1.0] * graph.CHANNELS
    for k in range(graph.N_LOW):
        f[k] = low
    f[graph.IDX_HIGH] = high
    return f


def quiet(n):
    return [frame() for _ in range(n)]


def knock():
    """Three dull thuds 250 ms apart, about 25 dB above the room (a real door knock reads 20 to 25 dB or more). Loud
    enough for the three-thud route, which a second knock within 30 s of the first needs."""
    out = []
    for _ in range(3):
        for i in range(25):
            out.append(frame(low=300.0 if i < 5 else 1.0))
    return out


class Feeder:
    """Sends each frame to every live stream, the way one microphone feeds two ffmpeg processes."""
    current = None

    def __init__(self):
        self.streams = []
        Feeder.current = self

    def send(self, frames):
        for fr in frames:
            for s in list(self.streams):
                s.process(fr)
            time.sleep(0.0004)


class FakeStream:
    linger = 0        # frames still delivered after kill(), like data already sitting in a pipe

    def __init__(self, owner, source, cfg, muted=False):
        self.owner, self.muted, self.cfg = owner, muted, cfg
        self.linger_left = 0
        self.det = detect.Detector(cfg)
        self.frames = 0
        self.last_frame_at = None
        self.done = threading.Event()
        self.exit_code = None
        self.stderr_tail = []
        self.proc = type("P", (), {"pid": 0, "poll": lambda self: None})()
        self.killed = False

    def start(self):
        Feeder.current.streams.append(self)

    def kill(self):
        self.killed = True
        self.linger_left = FakeStream.linger
        if self.linger_left <= 0 and self in Feeder.current.streams:
            Feeder.current.streams.remove(self)
        self.exit_code = -9
        self.done.set()

    def process(self, fr):
        if self.killed:
            if self.linger_left <= 0:
                if self in Feeder.current.streams:
                    Feeder.current.streams.remove(self)
                return
            self.linger_left -= 1
        self.frames += 1
        self.last_frame_at = time.monotonic()
        if not self.muted:
            self.owner._heard(self.last_frame_at)
            self.owner.frames += 1
        for ev in self.det.feed(fr):
            if not self.muted:
                self.owner._emit(ev)


class RecycleTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.feeder = Feeder()
        self.got = []
        self.addCleanup(self.stop)
        orig = listener._Stream
        listener._Stream = FakeStream
        self.addCleanup(setattr, listener, "_Stream", orig)
        # the real swap waits for 30 s of sound (3000 frames); these scripted streams use a tenth of that
        warm = listener.RECYCLE_WARM_FRAMES
        listener.RECYCLE_WARM_FRAMES = 300
        self.addCleanup(setattr, listener, "RECYCLE_WARM_FRAMES", warm)
        self.ln = listener.Listener("default", self.got.append, log_path=os.path.join(self.dir, "e.log"), recycle_s=3600.0)
        self.ln.start()
        self.wait(lambda: self.ln.active is not None)

    def stop(self):
        self.ln.stop()

    def wait(self, cond, seconds=10.0):
        end = time.time() + seconds
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.01)
        self.fail("timed out waiting")

    def knocks(self):
        return [e for e in self.got if e["kind"] == "knock"]

    def test_swap_happens_when_quiet_and_no_event_is_lost_or_doubled(self):
        first = self.ln.active
        self.feeder.send(quiet(700))
        self.ln.next_recycle = 0                      # time to recycle
        self.wait(lambda: self.ln.standby is not None)
        self.feeder.send(quiet(400))                  # the new one learns the room
        self.wait(lambda: self.ln.recycles == 1)
        self.assertTrue(first.killed)
        self.assertIsNot(self.ln.active, first)
        self.feeder.send(knock() + quiet(100))        # a knock after the swap is reported once, by the new stream
        self.wait(lambda: len(self.knocks()) == 1)
        self.assertEqual(len(self.knocks()), 1)
        self.assertEqual(self.ln.restarts, 0)

    def test_swap_waits_for_a_knock_in_progress_so_it_is_reported_once(self):
        first = self.ln.active
        self.feeder.send(quiet(700))
        self.ln.next_recycle = 0
        self.wait(lambda: self.ln.standby is not None)
        # The new stream is warm after 300 frames and the swap is allowed as soon as the old detector is idle. Feed
        # fewer than 300 quiet frames, so the warm-up completes only while the knock is under way; with more, the
        # swap could legitimately happen in the quiet before it, depending on when the supervisor thread looks.
        self.feeder.send(quiet(280))
        self.feeder.send(knock()[:40])                # a knock begins: the old detector is busy
        self.assertEqual(self.ln.recycles, 0)
        self.feeder.send(knock()[40:] + quiet(150))
        self.wait(lambda: self.ln.recycles == 1)
        self.assertTrue(first.killed)
        self.assertEqual(len(self.knocks()), 1)       # reported by the old one, muted twin did not repeat it
        self.feeder.send(quiet(600) + knock() + quiet(100))   # a new knocking session, reported by the new one
        self.wait(lambda: len(self.knocks()) == 2)
        self.assertEqual(len(self.knocks()), 2)

    def test_a_recycle_that_cannot_warm_up_leaves_the_old_stream_reporting(self):
        first = self.ln.active
        listener.RECYCLE_WARM_ABORT_S, orig = 0.3, listener.RECYCLE_WARM_ABORT_S
        self.addCleanup(setattr, listener, "RECYCLE_WARM_ABORT_S", orig)
        self.ln.next_recycle = 0
        self.wait(lambda: self.ln.standby is not None)
        time.sleep(0.5)                               # no frames reach the new stream: it never warms
        self.wait(lambda: self.ln.standby is None)
        self.assertEqual(self.ln.recycles, 0)
        self.assertIs(self.ln.active, first)
        self.assertFalse(first.killed)
        self.assertGreater(self.ln.next_recycle, time.monotonic())   # a retry is scheduled, not a failure
        self.assertEqual(self.ln.restarts, 0)

    def test_old_stream_still_delivering_after_the_swap_does_not_double_a_knock(self):
        orig = FakeStream.linger
        FakeStream.linger = 400
        self.addCleanup(setattr, FakeStream, "linger", orig)
        first = self.ln.active
        self.feeder.send(quiet(700))
        self.ln.next_recycle = 0
        self.wait(lambda: self.ln.standby is not None)
        self.feeder.send(quiet(400))
        self.wait(lambda: self.ln.recycles == 1)
        self.assertTrue(first.killed)
        self.feeder.send(knock() + quiet(150))        # the dying stream's pipe still carries this knock too
        self.wait(lambda: len(self.knocks()) >= 1)
        time.sleep(0.3)
        self.assertEqual(len(self.knocks()), 1)       # only the new stream may report it

    def test_idle_means_nothing_is_part_way_through(self):
        d = detect.Detector()
        for fr in quiet(100):
            d.feed(fr)
        self.assertTrue(d.idle())
        for fr in knock()[:24]:                       # one thud; by now it is a finished thud, not a pending one
            d.feed(fr)
        self.assertIsNone(d.knock.cand)
        self.assertFalse(d.idle())                    # a second thud could still make it a knock
        for fr in quiet(20):
            d.feed(fr)
        self.assertFalse(d.idle())                    # still inside the window between thuds of one knock
        for fr in quiet(40):
            d.feed(fr)
        self.assertTrue(d.idle())

    def test_recycling_is_off_for_a_recording_and_when_set_to_zero(self):
        self.assertEqual(listener.Listener("file:/x.wav", lambda e: None, recycle_s=60).recycle_s, 0.0)
        self.assertEqual(listener.Listener("default", lambda e: None, recycle_s=0).recycle_s, 0.0)


if __name__ == "__main__":
    unittest.main()
