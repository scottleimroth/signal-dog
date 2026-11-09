"""Runs ffmpeg, feeds its numbers to the detector and reports events. Nothing here records or keeps sound.

Each ffmpeg process has its own reader thread (a `_Stream`). One supervisor thread owns the listener: it starts the
first stream, restarts it if it dies or stalls, says plainly when the microphone cannot be opened, and, every
`recycle_s` seconds, replaces the process without a gap. A live microphone capture slowly costs more processor
the longer one ffmpeg runs (measured: about 5 to 10 percent of a core over 40 minutes, and still rising), so the
plugin starts a second ffmpeg beside the first, lets its detector learn the room, waits until the first detector
is not in the middle of a pattern, then switches over and stops the old one.
"""

from __future__ import annotations

import array
import collections
import logging
import os
import subprocess
import threading
import time

from . import graph
from .detect import Config, Detector
from .ongoing import Alarms

log = logging.getLogger("plugin.signal-dog")

STALL_SECONDS = 10        # no numbers for this long: restart ffmpeg
GIVE_UP_AFTER = 6         # failed starts within a minute before the listener gives up and says why
LOG_LIMIT = 256 * 1024    # bytes in the numbers log before it is rotated
RECYCLE_WARM_FRAMES = 3000      # the new detector must have heard 30 s before it takes over: long enough to have its own
                                # room level, busy count and recent thuds, so only older things need handing over
RECYCLE_WARM_ABORT_S = 60.0     # give up on a recycle if the new ffmpeg has not produced that much by then
RECYCLE_IDLE_WAIT_S = 30.0      # wait this long for the old detector to be between patterns, then swap anyway
RECYCLE_RETRY_S = 60.0          # after a recycle that could not finish, try again in a minute


class NumbersLog:
    """A small log of times, levels and what was detected. Numbers only, never audio. Size limited."""

    def __init__(self, path):
        self.path = path
        self.broken = False

    def line(self, text: str) -> bool:
        """Write one line. Returns whether it was written."""
        if self.broken or not self.path:
            return False
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            if os.path.exists(self.path) and os.path.getsize(self.path) > LOG_LIMIT:
                os.replace(self.path, self.path + ".1")
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), text))
            return True
        except OSError as e:
            self.broken = True
            log.warning("signal-dog: cannot write its numbers log (%s); continuing without it", e)
            return False


def default_log_path():
    """$HERMES_HOME/signal-dog/events.log of the ACTIVE profile. Hermes's get_hermes_home() follows the profile a
    multi-profile gateway is serving; os.environ only names the profile that launched it. Outside Hermes (tests,
    tools) the environment variable or ~/.hermes is used."""
    try:
        from hermes_constants import get_hermes_home
        home = str(get_hermes_home())
    except Exception:
        home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
    return os.path.join(home, "signal-dog", "events.log")


class _Stream:
    """One ffmpeg process, its detector and the thread that reads it. A muted stream learns but reports nothing."""

    def __init__(self, owner, source: str, cfg: Config, muted: bool = False):
        self.owner = owner
        self.source = source
        self.cfg = cfg
        self.muted = muted
        self.det = Detector(cfg)
        self.frames = 0
        self.last_frame_at = None
        self.done = threading.Event()
        self.exit_code = None
        self.stderr_tail = collections.deque(maxlen=4)
        self.proc = None
        self.thread = None

    def start(self):
        """Start ffmpeg and the reader. Raises FileNotFoundError or OSError if ffmpeg cannot be started."""
        self.proc = subprocess.Popen(graph.command(self.source), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=graph.environment(), bufsize=0)
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self.thread = threading.Thread(target=self._read, name="signal-dog-reader", daemon=True)
        self.thread.start()

    def kill(self):
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass

    def _read_stderr(self):
        for raw in iter(self.proc.stderr.readline, b""):
            self.stderr_tail.append(raw.decode("utf-8", "replace").strip()[:200])

    def _read(self):
        owner, p, det = self.owner, self.proc, self.det
        carry = b""
        next_summary = time.monotonic() + 60.0
        n = graph.FRAME_BYTES
        try:
            while not owner.stop_flag.is_set():
                chunk = p.stdout.read(n * 10)
                if not chunk:
                    break
                now = time.monotonic()
                carry += chunk
                usable = len(carry) // n * n
                if usable == 0:
                    continue
                vals = array.array("f")
                vals.frombytes(carry[:usable])
                carry = carry[usable:]
                self.last_frame_at = now
                if not self.muted:
                    owner._heard(now)
                for i in range(0, len(vals), graph.CHANNELS):
                    self.frames += 1
                    muted = self.muted
                    if not muted:
                        owner.frames += 1
                    events = det.feed(vals[i:i + graph.CHANNELS])
                    if not muted:
                        for ev in owner.alarms.feed(owner.frames / 100.0, events, det.beep.sounding):
                            if ev["kind"] == "beep_stopped":
                                det.rearm_beep(ev["hz"])
                            owner._emit(ev)
                if self.cfg.debug and not self.muted:
                    owner._flush_debug(det)
                if now >= next_summary:
                    next_summary = now + 60.0
                    if not self.muted:
                        pk = det.peaks()
                        owner.numlog.line("minute frames=%d knock_db=%s beep_db=%s sharp_dropped=%d restarts=%d" % (
                            owner.frames, pk["knock_db"], pk["beep_db"], det.knock.sharp, owner.restarts))
        finally:
            try:
                self.exit_code = p.wait(2.0)
            except subprocess.TimeoutExpired:
                self.kill()
                self.exit_code = p.poll()
            self.done.set()


class Listener:
    def __init__(self, source: str, on_event, log_path=None, cfg: Config = None, recycle_s: float = 0.0,
                 reminder_s: float = 180.0, clear_s: float = 15.0):
        self.source = source or "default"
        self.on_event = on_event
        self.cfg = cfg or Config()
        self.alarms = Alarms(reminder_s, clear_s, self.cfg.beep_spread)
        self.recycle_s = 0.0 if self.source.startswith("file:") else max(0.0, float(recycle_s))
        self.next_recycle = time.monotonic() + self.recycle_s
        self.numlog = NumbersLog(log_path if log_path is not None else default_log_path())
        self.state = "stopped"          # stopped | starting | listening | finished | error
        self.error = ""
        self.stop_flag = threading.Event()
        self.thread = None
        self.proc = None                # the ffmpeg process currently reporting
        self.active = None              # the stream currently reporting
        self.standby = None             # a stream being warmed up for a recycle
        self.frames = 0
        self.started_at = None
        self.last_frame_at = None
        self.restarts = 0
        self.recycles = 0
        self.event_count = 0
        self.recent = collections.deque(maxlen=5)
        self.lock = threading.Lock()

    # ---- control -----------------------------------------------------------------------------------------
    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_flag.clear()
        self.error = ""
        self.state = "starting"
        self.started_at = time.monotonic()
        self.next_recycle = time.monotonic() + self.recycle_s
        self.thread = threading.Thread(target=self._run, name="signal-dog-listener", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag.set()
        self._kill()
        t = self.thread
        if t is not None and t is not threading.current_thread():
            t.join(5.0)
        self.state = "stopped"

    def status(self) -> dict:
        now = time.monotonic()
        up = (now - self.started_at) if self.started_at else 0.0
        age = (now - self.last_frame_at) if self.last_frame_at else None
        return {"state": self.state, "error": self.error, "source": self.source, "uptime_s": round(up),
                "frames": self.frames, "last_frame_age_s": None if age is None else round(age, 1),
                "restarts": self.restarts, "recycles": self.recycles, "events": self.event_count,
                "recent": list(self.recent)}

    # ---- internals ---------------------------------------------------------------------------------------
    def _kill(self):
        for s in (self.active, self.standby):
            if s is not None:
                s.kill()

    def _fail(self, text: str):
        for ev in self.alarms.interrupted():
            self._emit(ev)
        self.error = text
        self.state = "error"
        self.numlog.line("down reason=%s" % text.replace("\n", " ")[:200])
        self.on_event({"kind": "down", "text": text})

    def _heard(self, now: float):
        """Called by the reporting stream whenever it receives numbers."""
        self.last_frame_at = now
        if self.state != "listening":
            self.state = "listening"
            self.error = ""

    def _new_stream(self, muted: bool = False):
        s = _Stream(self, self.source, self.cfg, muted=muted)
        s.start()
        return s

    def _watch(self, stream):
        """Wait while `stream` reports. Returns the stream that is reporting when it ends (a recycle may have
        replaced the one passed in)."""
        while not self.stop_flag.is_set():
            if stream.done.wait(1.0):
                break
            last = stream.last_frame_at
            if self.state == "listening" and last is not None and time.monotonic() - last > STALL_SECONDS:
                log.warning("signal-dog: no numbers from ffmpeg for %ds; restarting it", STALL_SECONDS)
                stream.kill()
                break
            if self.recycle_s and time.monotonic() >= self.next_recycle:
                new = self._recycle(stream)
                if new is not None:
                    stream = new
        return stream

    def _recycle(self, old):
        """Swap `old` for a fresh ffmpeg and detector without a gap. Returns the new stream, or None if the swap was
        not made (the old one keeps reporting and a retry is scheduled)."""
        self.numlog.line("recycle start")
        try:
            new = self._new_stream(muted=True)
        except OSError as e:
            self.numlog.line("recycle abandoned: ffmpeg could not be started (%s)" % e)
            self.next_recycle = time.monotonic() + RECYCLE_RETRY_S
            return None
        self.standby = new
        started = time.monotonic()
        try:
            while not self.stop_flag.is_set() and new.frames < RECYCLE_WARM_FRAMES:
                if new.done.is_set() or old.done.is_set() or time.monotonic() - started > RECYCLE_WARM_ABORT_S:
                    new.kill()
                    self.numlog.line("recycle abandoned: the new ffmpeg did not start delivering")
                    self.next_recycle = time.monotonic() + RECYCLE_RETRY_S
                    return None
                time.sleep(0.05)
            waited = time.monotonic()
            while (not self.stop_flag.is_set() and not old.det.idle() and time.monotonic() - waited < RECYCLE_IDLE_WAIT_S
                   and not old.done.is_set() and not new.done.is_set()):
                time.sleep(0.05)
            if self.stop_flag.is_set() or old.done.is_set() or new.done.is_set():
                new.kill()
                self.next_recycle = time.monotonic() + RECYCLE_RETRY_S
                return None
            idle = old.det.idle()
            moved = new.det.adopt(old.det)     # what only the old one knows: quiet times, holds, a knocking session
            if self.cfg.debug:
                # the new one's tuning lines for the 30 s it listened muted repeat what the old one already wrote
                new.det.knock.dlog.clear()
                new.det.beep.log.clear()
            new.muted = False          # the new detector reports first, then the old one goes quiet
            old.muted = True
            self.active, self.proc = new, new.proc
            old.kill()
            self.recycles += 1
            self.next_recycle = time.monotonic() + self.recycle_s
            self.numlog.line("recycle done waited_for_idle=%.1fs idle=%s recycles=%d offset_frames=%d offset_steps=%d" % (
                time.monotonic() - waited, idle, self.recycles, moved["offset_frames"], moved["offset_steps"]))
            return new
        finally:
            self.standby = None

    def _run(self):
        failures = collections.deque(maxlen=GIVE_UP_AFTER)
        backoff = 2.0
        while not self.stop_flag.is_set():
            begun = time.monotonic()
            try:
                stream = self._new_stream()
            except FileNotFoundError:
                self._fail("ffmpeg was not found. Install ffmpeg, then run /signaldog on again.")
                return
            except OSError as e:
                self._fail("ffmpeg could not be started (%s)." % e)
                return
            self.active, self.proc = stream, stream.proc
            self.numlog.line("start source=%s" % self.source)
            stream = self._watch(stream)
            stream.kill()
            stream.done.wait(3.0)
            code = stream.exit_code
            if self.stop_flag.is_set():
                break
            if self.source.startswith("file:") and code == 0:
                self.state = "finished"
                self.numlog.line("finished source=file")
                return
            for ev in self.alarms.interrupted():
                self._emit(ev)
            reason = " | ".join(stream.stderr_tail) or "ffmpeg stopped (exit %s)" % code
            self.restarts += 1
            self.numlog.line("restart exit=%s reason=%s" % (code, reason[:160]))
            failures.append(time.monotonic())
            if len(failures) == GIVE_UP_AFTER and failures[-1] - failures[0] < 60.0:
                self._fail("The microphone could not be opened: %s" % reason[:160])
                return
            self.state = "starting"
            backoff = 2.0 if time.monotonic() - begun > 60.0 else min(backoff * 2, 30.0)
            self.next_recycle = time.monotonic() + self.recycle_s
            self.stop_flag.wait(backoff)

    def _flush_debug(self, det: Detector):
        """Tuning aid: log why each thud was kept or thrown out, and each beep pulse. Numbers only."""
        while det.knock.dlog:
            tt, low, high, worst, near, sharp, attack, fall, lo, mid, top = det.knock.dlog.pop(0)
            self.numlog.line("thud frame=%d low_db=%s high_db=%s worst_high_minus_low=%s near_high_db=%s sharp=%s "
                             "attack=%s fall=%s share_70_172=%s share_232_420=%s share_565_760=%s" % (
                                 tt, low, high, worst, near, sharp, attack, fall, lo, mid, top))
        while det.beep.log:
            hz, start, steps, prom, lit, rise_far, rise_low = det.beep.log.pop(0)
            self.numlog.line("pulse hz=%d start_s=%s steps=%d prom_min_db=%s bands_lit=%d rise_far=%s rise_low=%s" % (
                hz, start, steps, prom, lit, rise_far, rise_low))

    def _emit(self, ev: dict):
        if ev["kind"] in ("knock", "beep"):
            self.event_count += 1
        self.recent.append(time.strftime("%H:%M:%S ") + ev["kind"])
        self.numlog.line("event " + " ".join("%s=%s" % kv for kv in ev.items()))
        try:
            self.on_event(ev)
        except Exception:
            log.exception("signal-dog: event handler failed")
