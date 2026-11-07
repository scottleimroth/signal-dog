#!/usr/bin/env python3
"""Run the real listener on the microphone until a set time, logging numbers only. No chat, no audio.

  python3 tools/live_run.py --until 2026-11-07T07:00:00+11:00 --log /path/events.log [--source default]

Stops by itself at the end time, if memory gets tight, if the listener fails, or on SIGTERM. Every minute it
writes a resource line (memory and processor use of the listener and its ffmpeg).
"""
import argparse
import datetime
import importlib.util
import os
import pathlib
import signal
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load():
    spec = importlib.util.spec_from_file_location("signal_dog", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["signal_dog"] = mod
    spec.loader.exec_module(mod)
    return mod


def mem_available_mb():
    with open("/proc/meminfo") as fh:
        for line in fh:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 0


def rss_mb(pid):
    try:
        with open("/proc/%d/status" % pid) as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


def ticks(pid):
    try:
        with open("/proc/%d/stat" % pid) as fh:
            parts = fh.read().rsplit(")", 1)[1].split()
        return int(parts[11]) + int(parts[12])
    except (OSError, IndexError, ValueError):
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--source", default="default")
    ap.add_argument("--min-mem-mb", type=int, default=8192)
    ap.add_argument("--max-rss-mb", type=float, default=200.0)
    ap.add_argument("--max-cpu-pct", type=float, default=10.0)
    ap.add_argument("--pidfile", default="")
    ap.add_argument("--debug", action="store_true", help="also log every thud decision and beep pulse (costs processor time)")
    ap.add_argument("--cycle-minutes", type=int, default=0, help="restart the listener this often (0 = never)")
    ap.add_argument("--recycle-minutes", type=float, default=0.0, help="replace the ffmpeg reader this often, without a gap (0 = never)")
    a = ap.parse_args()
    load()
    from signal_dog import detect, listener
    listener.LOG_LIMIT = 8 * 1024 * 1024      # a night of tuning detail must not rotate the evening away

    until = datetime.datetime.fromisoformat(a.until)
    reason = [None]

    def stop_signal(sig, frm):
        reason[0] = "signal %d" % sig

    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGINT, stop_signal)
    if a.pidfile:
        with open(a.pidfile, "w") as fh:
            fh.write(str(os.getpid()))
    def make():
        return listener.Listener(a.source, lambda ev: None, log_path=a.log, cfg=detect.Config(debug=a.debug),
                                 recycle_s=a.recycle_minutes * 60.0)

    ln = make()
    ln.numlog.line("run until=%s source=%s limits: mem_avail>=%dMB rss<=%.0fMB cpu<=%.0f%% cycle=%dmin" % (
        a.until, a.source, a.min_mem_mb, a.max_rss_mb, a.max_cpu_pct, a.cycle_minutes))
    ln.start()
    hz = os.sysconf("SC_CLK_TCK")
    me = os.getpid()
    last_t = time.monotonic()
    last_py = 0             # the first minute's reading counts from the start; it must not be skipped
    last_ff, last_ffpid = 0, None
    next_res = time.monotonic() + 60.0
    cycle_at = time.monotonic() + a.cycle_minutes * 60.0 if a.cycle_minutes else None
    while reason[0] is None:
        time.sleep(2.0)
        now = datetime.datetime.now(datetime.timezone.utc)
        if now >= until:
            reason[0] = "end time reached"
        elif ln.state == "error":
            reason[0] = "listener error: %s" % ln.error
        elif mem_available_mb() < a.min_mem_mb:
            reason[0] = "memory available below %d MB" % a.min_mem_mb
        if reason[0] is None and cycle_at is not None and time.monotonic() >= cycle_at:
            # stopgap while a slow processor climb is being tracked down: start the listener afresh
            ln.numlog.line("cycle restarting the listener (every %d min)" % a.cycle_minutes)
            ln.stop()
            ln = make()
            ln.start()
            cycle_at = time.monotonic() + a.cycle_minutes * 60.0
            last_t, last_py, last_ff, last_ffpid = time.monotonic(), ticks(me), 0, None
            next_res = time.monotonic() + 60.0
        if reason[0] is None and time.monotonic() >= next_res:
            next_res = time.monotonic() + 60.0
            ffpid = ln.proc.pid if ln.proc is not None and ln.proc.poll() is None else None
            py, ff = ticks(me), (ticks(ffpid) if ffpid else 0)
            wall = time.monotonic() - last_t
            py_cpu = 100.0 * (py - last_py) / hz / wall
            ff_cpu = 100.0 * (ff - last_ff) / hz / wall if ffpid is not None and ffpid == last_ffpid else 0.0
            cpu = py_cpu + ff_cpu
            last_t, last_py, last_ff, last_ffpid = time.monotonic(), py, ff, ffpid
            rss = rss_mb(me) + (rss_mb(ffpid) if ffpid else 0.0)
            ln.numlog.line("res rss_mb=%.1f cpu_pct=%.1f py_cpu=%.1f ffmpeg_cpu=%.1f mem_avail_mb=%d state=%s restarts=%d frames=%d" % (
                rss, cpu, py_cpu, ff_cpu, mem_available_mb(), ln.state, ln.restarts, ln.frames))
            if rss > a.max_rss_mb:
                reason[0] = "memory use %.0f MB over the %.0f MB limit" % (rss, a.max_rss_mb)
            elif cpu > a.max_cpu_pct:
                reason[0] = "processor use %.1f%% over the %.0f%% limit" % (cpu, a.max_cpu_pct)
    ln.numlog.line("stopping reason=%s" % reason[0])
    ln.stop()
    ln.numlog.line("stopped")
    if a.pidfile and os.path.exists(a.pidfile):
        os.remove(a.pidfile)


if __name__ == "__main__":
    main()
