#!/usr/bin/env python3
"""Run the thud decisions in a numbers-only log through the REAL knock rule (detect._Knock._finalize), not a copy.

  python3 tools/replay_thuds.py <events.log> [--from 2026-11-07T15:05:15] [--to 2026-11-07T15:06:23]
                                             [--set knock_floor_db=18] [--list] [--no-beep-gate]

The log is the one the live runner writes with --debug: lines like
  2026-11-07T15:05:30+1100 thud frame=10867 low_db=22.5 high_db=1.2 worst_high_minus_low=-8.1 near_high_db=7.1 sharp=True
Only numbers are read; there is no audio. For every thud the detector is given the frame numbers and levels the log
holds, rebuilt as the few smoothed frames its own tests look at, then asked what it would have done. A backwards jump
in the frame number, or a line saying the reader started or was replaced, means the log's detector was replaced; the
replay starts afresh there too.

Route B (two thuds) stays silent while the beep side says the room is busy, so the pulse lines of the same log are
handed to the real beep rule (as tools/replay_pulses.py does) and each thud is told what that rule said when the thud
was decided (peak_merge frames after its peak). Pulse lines from before --from are used too, because a busy room stays
busy for two minutes; --to ends the reading. --no-beep-gate leaves the beep side out, as the replay did before.
Older lines, with the fields added later at their end (attack, fall, shares), are read the same way.
"""
import bisect
import collections
import importlib.util
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import replay_pulses  # noqa: E402


def load():
    spec = importlib.util.spec_from_file_location("signal_dog", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["signal_dog"] = mod
    spec.loader.exec_module(mod)
    return mod


THUD = re.compile(r" thud frame=(\d+) low_db=(-?[\d.]+) high_db=(-?[\d.]+) worst_high_minus_low=(\S+) near_high_db=(\S+) sharp=\w+")
START = re.compile(r" (?:start source=|recycle done)")


def read_log(path, first="", last="~", rows_only=True):
    """One entry per life of the log's detector that has thuds in it: {"rows": the thud rows from `first` to `last`,
    "pulses": every pulse line of that life up to `last`}. The log is in time order, so reading stops after `last`.
    Each life also has "rows_at" and "pulses_at" (the whole 19-character time of each line) and
    "began" (the time of the line that began it, "" if it is the log's first) and "swapped" (True when it began at a
    `recycle done` line that carries offsets: the live detector then carried the old one's busy hold and thud memory
    over, which this reader does not replay). rows_only=False keeps every life, also
    one with pulses but no thuds, or with no lines yet by `last`."""
    def life(began="", swapped=False):
        return {"rows": [], "pulses": [], "rows_at": [], "pulses_at": [], "began": began, "swapped": swapped}

    segs, prev = [life()], -1
    for line in open(path, encoding="utf-8"):
        if line[:2] == "20" and line[:19] > last:
            break
        if START.search(line):
            swapped = "recycle done" in line and "offset_frames=" in line
            if segs[-1]["rows"] or segs[-1]["pulses"]:
                segs.append(life(line[:19], swapped))
            else:
                segs[-1]["began"], segs[-1]["swapped"] = line[:19], swapped
            prev = -1
            continue
        m = THUD.search(line)
        if m:
            frame = int(m.group(1))
            if frame < prev and (segs[-1]["rows"] or segs[-1]["pulses"]):
                segs.append(life(line[:19]))
            prev = frame
            if line[:19] >= first:
                num = lambda s: None if s == "None" else float(s)
                segs[-1]["rows"].append((line[11:19], frame, float(m.group(2)), float(m.group(3)), num(m.group(4)), num(m.group(5))))
                segs[-1]["rows_at"].append(line[:19])
            continue
        m = replay_pulses.PULSE.search(line)
        if m:
            segs[-1]["pulses"].append((int(m.group(1)), float(m.group(2)), int(m.group(3)), float(m.group(4)) if m.group(4) else 10.0))
            segs[-1]["pulses_at"].append(line[:19])
    return [s for s in segs if s["rows"] or not rows_only]


def read(path, first="", last="~"):
    return [row for seg in read_log(path, first, last) for row in seg["rows"]]


def replay(rows, cfg=None, pulses=None):
    """The knock alerts for the thud rows of one life of the detector. With `pulses` (that life's pulse lines, as
    replay_pulses.read gives them) route B is also held back while the real beep rule calls the room busy."""
    load()
    from signal_dog import detect  # noqa: E402
    cfg = cfg or detect.Config()
    timeline = replay_pulses.drive(pulses, cfg)[1] if pulses else []
    steps = [t for t, _ in timeline]

    def beep_busy(frame):
        # the detector asks at the frame it decides the thud, when it has judged every step before frame // 4
        step = frame // 4
        i = bisect.bisect_right(steps, step - 1) - 1
        return i >= 0 and step <= timeline[i][1]

    k, prev, alerts = detect._Knock(cfg), -1, []
    for when, frame, low, high, worst, near in rows:
        if frame < prev:
            k = detect._Knock(cfg)
        prev = frame
        k.beep_busy = lambda at=frame + cfg.peak_merge: beep_busy(at)
        k.hist = collections.deque(maxlen=40)
        if worst is not None:
            # The log's `worst` is the largest high-minus-low over the loud frames, and that frame is not always the
            # peak, so give it the lowest strength that still counts as loud (10 dB), never the peak's level: that
            # would overstate its high band. In real data the near-high figure is never below 10 + worst.
            k.hist.append((frame, 10.0, 10.0 + worst))
        if near is not None:
            k.hist.append((frame - 1, 0.0, near))                # a quiet moment just before, carrying the near high
        k.cand = (frame, low, high)
        for ev in k._finalize(frame):
            alerts.append((when, ev))
    return alerts


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    path, first, last, sets, show, gate = args.pop(0), "", "~", {}, False, True
    while args:
        a = args.pop(0)
        if a == "--from":
            first = args.pop(0)
        elif a == "--to":
            last = args.pop(0)
        elif a == "--list":
            show = True
        elif a == "--no-beep-gate":
            gate = False
        elif a == "--set":
            key, v = args.pop(0).split("=")
            sets[key] = float(v) if "." in v else int(v)
    segs = read_log(path, first, last)
    rows = [row for seg in segs for row in seg["rows"]]
    if not rows:
        print("no thud lines in that range")
        return 1
    load()
    from signal_dog import detect  # noqa: E402
    cfg = detect.Config(**sets)
    alerts = [a for seg in segs for a in replay(seg["rows"], cfg, seg["pulses"] if gate else None)]
    print("thuds %d; knock alerts %d" % (len(rows), len(alerts)))
    if show:
        for when, ev in alerts:
            print("  %s  thuds=%d level=%.1f dB" % (when, ev["thuds"], ev["level_db"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
