#!/usr/bin/env python3
"""Run the beep pulses in a numbers-only log through the REAL repeat rule (detect._Beep), not a copy of it.

  python3 tools/replay_pulses.py <events.log> [--from 2026-11-06T21:50:30] [--to 2026-11-06T22:25:14]
                                             [--set beep_even_steps=2] [--list]

The log is the one the live runner writes with --debug: lines like
  2026-11-06T21:50:31+1100 pulse hz=2408 start_s=12.4 steps=5 [prom_min_db=12.3 bands_lit=2]
Only numbers are read; there is no audio. Each pulse is handed to the detector at the step it ended, and the
detector's own state (window, evenness test, settle, one-minute quiet, ten-second hold) decides what alerts.
Logs from before 7 November have no prom_min_db; those pulses are given a prominence of 10 dB, which only
affects which of several leaking bands is named.
"""
import importlib.util
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load():
    spec = importlib.util.spec_from_file_location("signal_dog", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["signal_dog"] = mod
    spec.loader.exec_module(mod)
    return mod


PULSE = re.compile(r" pulse hz=(\d+) start_s=([\d.]+) steps=(\d+)(?: prom_min_db=(-?[\d.]+))?")


def read(path, first="", last="~"):
    out = []
    for line in open(path, encoding="utf-8"):
        if line[:2] == "20" and line[:19] > last:
            break                       # the log is in time order: never read past `last`
        m = PULSE.search(line)
        if m and first <= line[:19] <= last:
            out.append((int(m.group(1)), float(m.group(2)), int(m.group(3)), float(m.group(4)) if m.group(4) else 10.0))
    return out


def replay(pulses, cfg=None):
    return drive(pulses, cfg)[0]


def drive(pulses, cfg=None):
    """Hand the pulses to the real detector. Returns (alerts, timeline); the timeline is (step, busy_until) after each
    step handled, which tells when the detector called the room busy (tools/replay_thuds.py uses it for route B)."""
    load()
    from signal_dog import detect, graph  # noqa: E402
    cfg = cfg or detect.Config()
    b = detect._Beep(cfg)
    b.own = [0.0] * graph.N_TONE
    quiet = [0.0] * graph.N_TONE
    ends = {}
    for hz, start_s, steps, prom in pulses:
        k = graph.TONE_BANDS.index(hz)
        start = round(start_s * detect.STEPS_PER_SECOND)
        ends.setdefault(start + steps, []).append((k, start, steps, prom))
    ticks = sorted(set(ends) | {e + d for e in ends for d in range(1, cfg.beep_settle + 1)})
    alerts, timeline = [], []
    for t in ticks:
        for k, start, steps, prom in ends.get(t, ()):
            b.run[k], b.run_start[k], b.run_min[k] = steps, start, prom
        for ev in b._judge(t, quiet, t * 4):
            alerts.append((t / detect.STEPS_PER_SECOND, ev))
        timeline.append((t, b.busy_until))
    return alerts, timeline


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    path, first, last, sets, show = args.pop(0), "", "~", {}, False
    while args:
        a = args.pop(0)
        if a == "--from":
            first = args.pop(0)
        elif a == "--to":
            last = args.pop(0)
        elif a == "--list":
            show = True
        elif a == "--set":
            k, v = args.pop(0).split("=")
            sets[k] = float(v) if "." in v else int(v)
    pulses = read(path, first, last)
    if not pulses:
        print("no pulse lines in that range")
        return 1
    load()
    from signal_dog import detect  # noqa: E402
    alerts = replay(pulses, detect.Config(**sets))
    span = (max(p[1] + p[2] / 25.0 for p in pulses) - min(p[1] for p in pulses)) / 60.0
    repeats = [a for a in alerts if a[1]["pulses"] > 0]
    print("pulses %d over %.1f minutes; repeat alerts %d (%.1f an hour); long-tone alerts %d" % (
        len(pulses), span, len(repeats), len(repeats) / span * 60, len(alerts) - len(repeats)))
    if show:
        for t, ev in alerts:
            print("  %8.1f s  %5d Hz  pulses=%d  %.1f s" % (t, ev["hz"], ev["pulses"], ev["seconds"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
