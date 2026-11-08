"""Pattern rules on the 64 numbers per frame (see graph.py). Numbers in, events out. No audio, no ffmpeg.

Knock: a thud is a peak well above the room in the low bands, with little energy in the high band (a knock
is dull; a desk thump or a mug is sharp). Two routes. A: three thuds evenly spaced, alike in strength and loud,
at least two of them dull: works in any room, but in a room busy with thumps (typing, cooking, walking about) a set
counts only as the second within 20 s, and even then a clatter can pass it. B: in a quiet room only (few
thuds lately and the beep side does not call the room busy), two dull thuds 150 to 450 ms apart.

Beep: a narrow band that stands out from the bands around it and from its own usual level, holding for at
least 160 ms (a pulse), and then either repeating (three pulses of that one band, evenly spaced, within eight
seconds) or holding for three seconds. Steady tones, such as fan whine, are part of each band's own usual level
and so do not count. After a repeat alert, no other repeat alert at any pitch for ten seconds.
"""

from __future__ import annotations

import math
from collections import deque

from .graph import IDX_HIGH, IDX_TONE, N_LOW, N_TONE, TONE_BANDS

STEPS_PER_SECOND = 25      # the beep rules look at the numbers in groups of four frames
FLOOR = 1e-10              # energy below this is treated as silence (about -100 dBFS)


class Config:
    warm_frames = 50               # 0.5 s to learn the room before judging
    bg_alpha = 1.0 / 3000          # the room's usual level adapts over about 30 s
    bg_clamp = 3.0                 # loud frames count at most this much towards the usual level
    thud_db = 10.0                 # a thud is at least this far above the room
    thud_high_gap_db = 8.0         # and its high band sits at least this far below its low band
    peak_merge = 12                # frames: two peaks closer than this are one thud
    sharp_shadow = 60              # frames: a dull thud this soon after a sharp one is its rebound
    sharp_high_db = 10.0           # a burst this far above the room in the high band is a click: two thuds in a quiet
                                   # room need to stay under it (a real door-knock set read up to 9.7 dB; a desk thump
                                   # or a mug 6 to 13 dB)
    gap_min = 15                   # frames between thuds of one knock: 150 ms
    gap_max = 45                   # 450 ms
    knock_session = 500            # frames of quiet after a knock before the next alert: 5 s
    # Route A, any room, however busy: three thuds evenly spaced, alike in strength, loud, and mostly dull.
    knock_gap_min = 18             # frames between the thuds of a three: 180 ms ...
    knock_gap_max = 40             # ... to 400 ms
    knock_even = 10                # the two gaps differ by at most 100 ms
    knock_level_span = 8.0         # dB: the three thuds are within this of each other
    knock_floor_db = 20.0          # dB: and the strongest is at least this far above the room
    knock_dull_db = 14.0           # at least two of the three are under this in the high band
    knock_reach = 100              # frames: all three within one second
    # A room busy with thumps (someone typing, cooking, walking about) throws up even threes by chance, and people at a
    # door knock again. So in such a room a set alerts only when it is the second one within 20 s.
    knock_busy_thuds = 6           # thuds in the 30 s before a set (not counting its own last second) make the room busy
    knock_confirm = 2000           # frames: in a busy room, the set before must be this recent ...
    knock_set_gap = 50             # ... and at least this much earlier (500 ms), so it is a set of its own
    # A knock is a strike: each thud reaches its peak almost at once. Typing, footsteps, snoring and most household
    # thumping build up more slowly. Rise: frames on the smoothed low-band trace from 10 dB under the peak up to it.
    # A set passes when its middle rise (of three; the faster of a pair) is at most this. A rise that cannot be
    # measured passes: the trace never fell 10 dB under the peak in the history kept (about 270 ms before the peak),
    # as with loud television. In a very echoey room a knock can land on the echo of the last one and read as slow:
    # measured on public knocks with echo added, this costs about 1 in 10 (room echo) to 1 in 4 (hall echo).
    knock_rise = 5
    # Route B, quiet room only: two dull thuds. A room is busy when this many thuds came in the last 30 s, or while
    # the beep side says the room is busy (_Beep.busy_until).
    knock_busy = 3
    knock_memory = 3000            # frames: 30 s
    beep_db = 8.0                  # a band stands out by at least this much
    beep_hold = 4                  # steps: 160 ms
    beep_neighbours = 10           # bands either side compared against
    beep_spread = 3                # bands either side a loud tone leaks into; one alert for the lot
    beep_settle = 3                # steps to wait so the strongest of the leaking bands is the one reported
    beep_repeat = 3                # pulses in ONE band, evenly spaced ...
    beep_window = 8 * STEPS_PER_SECOND      # ... within eight seconds
    beep_even_steps = 2            # "evenly": the two gaps between pulse starts differ by at most 2 steps (80 ms)
    beep_lookback = 8              # earlier pulses of the band that are tried as the first two of the three
    beep_merge = 10 * STEPS_PER_SECOND      # after a repeat alert, no other repeat alert at any pitch for this long
    # A busy room (television, talking) throws up evenly spaced pulses by chance, so it must show more.
    beep_far = 3                   # bands: pulses farther than this from the tone are "elsewhere in the room"
    beep_busy_window = 30 * STEPS_PER_SECOND    # count pulses elsewhere over this long ...
    beep_busy_pulses = 15          # ... and the room is busy at this many ...
    beep_busy_hold = 120 * STEPS_PER_SECOND     # ... and stays busy this long after (a lull in a programme is not quiet)
    beep_busy_period = 60          # steps (2.4 s): in a busy room only quick repeats count
    beep_run = 5                   # busy room: a run of this many pulses, each one period after the last ...
    beep_confirm = 120 * STEPS_PER_SECOND       # ... or a second, separate set of three at the same pitch within this long
    beep_busy_min = 6              # steps (240 ms): in a busy room the evidence may hold at most beep_busy_short beeps
    beep_busy_short = 1            # shorter than that (a television's chance rhythms are mostly blips of 4 or 5 steps)
    beep_sustain = 3 * STEPS_PER_SECOND     # or one tone this long
    beep_cooldown = 60 * STEPS_PER_SECOND   # then quiet about that tone for a minute
    own_alpha = 1.0 / (30 * STEPS_PER_SECOND)
    own_clamp_db = 3.0
    warm_steps = 12
    debug = False

    def __init__(self, **kw):
        for k, v in kw.items():
            if not hasattr(Config, k):
                raise TypeError("unknown setting " + k)
            setattr(self, k, v)


def _db(x: float) -> float:
    return 10.0 * math.log10(x if x > 1e-14 else 1e-14)


class _Knock:
    def __init__(self, c: Config):
        self.c = c
        self.warm = []
        self.bg = None
        self.raw = deque(maxlen=3)
        self.sm = deque(maxlen=3)
        self.cand = None
        self.last_peak = None
        self.train = 0
        self.session_until = -1
        self.max_db = -99.0
        self.hist = deque(maxlen=40)       # (frame, low dB, high dB) of recent smoothed frames
        self.sharp = 0                     # thuds thrown out for being sharp (a thump, a mug) rather than dull
        self.last_sharp = None
        self.thuds = []                    # (frame, strength dB, high-band dB near it) of every thud in the last 30 s
        self.last_set = None               # frame of the last thud of the last set of three that route A accepted
        self.rises = {}                    # frame -> rise (frames, or None if it cannot be measured) of each thud in self.thuds
        self.forced_rise = False           # tools/replay_thuds.py sets the logged rise here; False means measure it
        self.dlog = []                     # why each thud was kept or thrown out, only when debug is on
        self.shape = deque(maxlen=40) if c.debug else None     # (frame, energy over the room in each low band), debug only
        self.beep_busy = lambda: False     # Detector sets this: does the beep side say the room is busy right now?

    def step(self, t: int, frame) -> list:
        # energies cannot be negative; the resampler rings after a loud click and can say otherwise
        x = tuple(max(v, 0.0) for v in frame[:N_LOW]) + (max(frame[IDX_HIGH], 0.0),)
        if self.bg is None:
            self.warm.append((t, x))
            if len(self.warm) < self.c.warm_frames:
                return []
            self.bg = [max(sorted(col)[len(col) // 2], FLOOR) for col in zip(*[w[1] for w in self.warm])]
            out = []
            for tt, xx in self.warm:
                out += self._run(tt, xx, t)
            self.warm = []
            return out
        return self._run(t, x, t)

    def _run(self, t: int, x, now: int) -> list:
        bg, c = self.bg, self.c
        low = _db(sum(x[k] / bg[k] for k in range(N_LOW)) / N_LOW)
        high = _db(x[N_LOW] / bg[N_LOW])
        if self.shape is not None:
            self.shape.append((t, tuple(max(x[k] - bg[k], 0.0) for k in range(N_LOW))))
        for k in range(N_LOW + 1):
            bg[k] = max(bg[k] + c.bg_alpha * (min(x[k], c.bg_clamp * bg[k]) - bg[k]), FLOOR)
        self.raw.append((low, high))
        n = len(self.raw)
        self.sm.append((sum(r[0] for r in self.raw) / n, sum(r[1] for r in self.raw) / n, t))
        out = []
        if len(self.sm) == 3:
            a, b, d = self.sm
            self.hist.append((b[2], b[0], b[1]))
            self.max_db = max(self.max_db, b[0])
            if b[0] >= c.thud_db and b[0] >= a[0] and b[0] > d[0] and b[1] <= b[0] - c.thud_high_gap_db:
                out += self._candidate((b[2], b[0], b[1]), now)
        if self.cand is not None and t - self.cand[0] >= c.peak_merge:
            out += self._finalize(now)
        return out

    def _candidate(self, p, now):
        if self.cand is not None and p[0] - self.cand[0] < self.c.peak_merge:
            if p[1] > self.cand[1]:
                self.cand = p
            return []
        out = self._finalize(now) if self.cand is not None else []
        self.cand = p
        return out

    def _finalize(self, now) -> list:
        c = self.c
        tt, level, high = self.cand
        self.cand = None
        # a knock is dull at every loud moment of the thud; a thump or a mug is sharp at its sharpest
        loud = [h[2] - h[1] for h in self.hist if abs(h[0] - tt) <= 8 and h[1] >= c.thud_db]
        sharp = bool(loud) and max(loud) > -c.thud_high_gap_db
        # a strong burst in the high band just before or during the thud is a click, not a knock
        near = [h[2] for h in self.hist if tt - 15 <= h[0] <= tt + 8]
        sharp = sharp or (bool(near) and max(near) >= c.sharp_high_db)
        if c.debug:
            self.dlog.append((tt, round(level, 1), round(high, 1), round(max(loud), 1) if loud else None,
                              round(max(near), 1) if near else None, sharp) + self._shape_of(tt, level))
        near_db = max(near) if near else -99.0
        rise = self._rise(tt, level) if self.forced_rise is False else self.forced_rise
        # Route A, any room: three thuds, evenly spaced, alike, loud, mostly dull. A television throws up sharp
        # transients that spoil two-thud knocks, but it does not keep time like a hand on a door.
        self.thuds = [h for h in self.thuds if tt - h[0] <= c.knock_memory]
        self.rises = {f: r for f, r in self.rises.items() if tt - f <= c.knock_memory}
        three = self._three([h for h in self.thuds if tt - h[0] <= c.knock_reach], tt, level, near_db, rise)
        thud_busy = False
        if three:
            thud_busy = sum(1 for h in self.thuds if c.knock_reach < tt - h[0] <= c.knock_memory) >= c.knock_busy_thuds
            last, self.last_set = self.last_set, tt
            if thud_busy and not (last is not None and c.knock_set_gap < tt - last <= c.knock_confirm):
                three = False              # the first set in a busy room: wait for a second
        busy = sum(1 for h in self.thuds if tt - h[0] > c.gap_max) >= c.knock_busy
        self.thuds.append((tt, level, near_db))
        self.rises[tt] = rise
        route = "three" if three else None
        # a hand strike on a desk is a sharp impact followed by a dull rebound: throw out both
        if sharp or (self.last_sharp is not None and tt - self.last_sharp <= c.sharp_shadow):
            self.sharp += 1
            self.last_sharp = tt
            self.train = 0
        else:
            gap = None if self.last_peak is None else tt - self.last_peak
            # a thud closer than 150 ms is an echo or a bounce of the last one, not a new thud
            if not (gap is not None and gap < c.gap_min and self.train >= 1):
                self.train = self.train + 1 if gap is not None and c.gap_min <= gap <= c.gap_max else 1
                prev, self.last_peak = self.last_peak, tt
                # Route B, quiet room only: two dull thuds, struck (the faster of the two rises quickly)
                if (route is None and self.train >= 2 and not busy and not self.beep_busy()
                        and self._struck([self.rises.get(prev), rise])):
                    route = "two"
        out = []
        if route:
            if tt >= self.session_until:
                out.append({"kind": "knock", "frame": now, "thuds": 3 if route == "three" else self.train,
                            "level_db": round(level, 1), "high_db": round(high, 1), "busy": thud_busy})
            self.session_until = tt + c.knock_session
        return out

    def _rise(self, tt: int, level: float):
        """Frames from the last moment the smoothed low band was 10 dB under this thud's peak up to the peak, or None
        if the 40 frames of history never fall that low."""
        cut = level - 10.0
        return next((tt - h[0] for h in reversed(self.hist) if h[0] < tt and h[1] <= cut), None)

    def _struck(self, rises) -> bool:
        """Does a set of thuds rise like a strike? The middle rise of three, the faster of two; unknown passes."""
        known = sorted(r for r in rises if r is not None)
        if not known:
            return True
        return (known[1] if len(known) == 3 else known[0]) <= self.c.knock_rise

    def _shape_of(self, tt: int, level: float) -> tuple:
        """Debug numbers for one thud: (attack, fall, share of 70-172 Hz, 232-420 Hz, 565-760 Hz). Attack is the frames
        from 10 dB under the peak up to it; fall the frames from the peak down to 10 dB under it (None if not reached
        by the time the thud is decided, or if the history does not reach back that far). The shares are percentages
        of the energy over the room's usual level in the nine low bands, at the peak frame."""
        cut = level - 10.0
        attack = self._rise(tt, level)
        fall = next((h[0] - tt for h in self.hist if h[0] > tt and h[1] <= cut), None)
        over = next((e for f, e in (self.shape or ()) if f == tt), None)
        total = sum(over) if over else 0.0
        if total <= 0.0:
            return (attack, fall, None, None, None)
        return (attack, fall) + tuple(round(100.0 * sum(over[a:b]) / total, 1) for a, b in ((0, 4), (4, 7), (7, 9)))

    def _three(self, recent, tt: int, level: float, near_db: float, rise=None) -> bool:
        c = self.c
        for i in range(len(recent)):
            for j in range(i + 1, len(recent)):
                a, b = recent[j][0] - recent[i][0], tt - recent[j][0]
                if not (c.knock_gap_min <= a <= c.knock_gap_max and c.knock_gap_min <= b <= c.knock_gap_max
                        and abs(a - b) <= c.knock_even):
                    continue
                lows = (recent[i][1], recent[j][1], level)
                if max(lows) - min(lows) > c.knock_level_span or max(lows) < c.knock_floor_db:
                    continue
                if (sum(1 for x in (recent[i][2], recent[j][2], near_db) if x < c.knock_dull_db) >= 2
                        and self._struck([self.rises.get(recent[i][0]), self.rises.get(recent[j][0]), rise])):
                    return True
        return False


class _Beep:
    def __init__(self, c: Config):
        self.c = c
        self.acc = [0.0] * N_TONE
        self.n = 0
        self.warm = []
        self.own = None
        self.run = [0] * N_TONE
        self.run_min = [99.0] * N_TONE
        self.run_start = [0] * N_TONE
        self.sus_sent = [False] * N_TONE
        self.pulses = deque()
        self.pending = []                  # (ready step, band, pulses, seconds, kind, strength, from a busy room)
        self.log = []                      # every pulse, only when debug is on
        self.recent = deque(maxlen=20) if c.debug else None     # (step, tone energies, low-band energy), debug only
        self.low_acc = 0.0                 # the nine low bands summed over the four frames of this step, debug only
        self.rise = [None] * N_TONE        # (far, low) rise in dB when the band's running pulse began, debug only
        self.lit = 0                       # bands standing out on the step being judged (for the debug log)
        self.repeat_hold = -1              # step before which no further repeat alert is sent, at any pitch
        self.elsewhere = []                # (band, start step) of pulses in the last 30 s, to tell a busy room
        self.busy_until = -1               # step until which the room counts as busy
        self.sets = []                     # (end step, band, period, (three starts), (their lengths)) of recent sets
        self.quiet_until = {"repeat": [-1] * N_TONE, "sustained": [-1] * N_TONE}
        self.s = 0
        self.max_prom = -99.0
        self._nb = [[j for j in range(max(0, k - c.beep_neighbours), min(N_TONE, k + c.beep_neighbours + 1)) if abs(j - k) >= 2]
                    for k in range(N_TONE)]

    def step(self, t: int, frame) -> list:
        acc = self.acc
        for k in range(N_TONE):
            v = frame[IDX_TONE + k]
            acc[k] += v if v > 0.0 else 0.0
        if self.recent is not None:
            self.low_acc += sum(v if v > 0.0 else 0.0 for v in frame[:N_LOW]) / N_LOW
        self.n += 1
        if self.n < 4:
            return []
        d = [_db(max(a / 4.0, FLOOR)) for a in acc]
        self.acc = [0.0] * N_TONE
        self.n = 0
        s, self.s = self.s, self.s + 1
        if self.recent is not None:
            self.recent.append((s, acc, self.low_acc))      # acc is not reused: self.acc is a fresh list
            self.low_acc = 0.0
        if self.own is None:
            self.warm.append(d)
            if len(self.warm) < self.c.warm_steps:
                return []
            self.own = [sorted(col)[len(col) // 2] for col in zip(*self.warm)]
            out = []
            for i, dd in enumerate(self.warm):
                out += self._judge(i, dd, t)
            self.warm = []
            return out
        return self._judge(s, d, t)

    def _judge(self, s: int, d, now: int) -> list:
        c, own, out = self.c, self.own, []
        proms = []
        for k in range(N_TONE):
            nb = sorted(d[j] for j in self._nb[k])
            proms.append(min(d[k] - nb[len(nb) // 2], d[k] - own[k]))
        self.lit = sum(1 for p in proms if p >= c.beep_db)
        for k in range(N_TONE):
            prom = proms[k]
            if prom > self.max_prom:
                self.max_prom = prom
            if prom >= c.beep_db:
                if self.run[k] == 0:
                    self.run_start[k] = s
                    self.run_min[k] = 99.0
                self.run[k] += 1
                self.run_min[k] = min(self.run_min[k], prom)
                if self.recent is not None and self.run[k] == c.beep_hold:
                    self.rise[k] = self._rise(k, self.run_start[k])
                if self.run[k] >= c.beep_sustain and not self.sus_sent[k]:
                    self.sus_sent[k] = True
                    self._candidate(s, k, 0, self.run[k] / STEPS_PER_SECOND, prom)
            else:
                if self.run[k] >= c.beep_hold:
                    self._pulse(k, s)
                self.run[k] = 0
                self.sus_sent[k] = False
            own[k] += c.own_alpha * (min(d[k], own[k] + c.own_clamp_db) - own[k])
        ready = [p for p in self.pending if p[0] <= s]
        if ready:
            self.pending = [p for p in self.pending if p[0] > s]
            for p in sorted(ready, key=lambda p: -p[5]):
                out += self._alert(p[1], s, now, p[2], p[3], p[4], p[6])
        return out

    def _pulse(self, k: int, s: int):
        """A pulse in band k has just ended. A repeat is three pulses of this one band, evenly spaced: the same place
        in three cycles of whatever pattern the sound makes. Pulses in one band cannot overlap, so one sound leaking
        across neighbouring bands is never counted as several beeps, and a stray pulse between two beeps does not
        break the pattern (any two earlier pulses may be the first two)."""
        c = self.c
        start, steps = self.run_start[k], self.run[k]
        if c.debug:
            self.log.append((TONE_BANDS[k], round(start / STEPS_PER_SECOND, 2), steps, round(self.run_min[k], 1), self.lit)
                            + (self.rise[k] or (None, None)))
        self.pulses.append((k, start, steps, self.run_min[k]))
        while self.pulses and s - self.pulses[0][1] > c.beep_window:
            self.pulses.popleft()
        # How busy is the room? Count the pulses in pitches far from this one over the last 30 s.
        self.elsewhere = [p for p in self.elsewhere if s - p[1] <= c.beep_busy_window]
        if sum(1 for p in self.elsewhere if abs(p[0] - k) > c.beep_far) >= c.beep_busy_pulses:
            self.busy_until = s + c.beep_busy_hold
        busy = s <= self.busy_until
        self.elsewhere.append((k, start))
        mine = [p for p in self.pulses if p[0] == k and s - p[1] <= c.beep_window]
        if len(mine) < c.beep_repeat:
            return
        starts = [p[1] for p in mine]
        found = self._evenly_spaced(starts[:-1][-c.beep_lookback:], start)
        if not found:
            return
        length = {p[1]: p[2] for p in mine}           # beep length in steps, by start
        route = None
        for period, three in found:
            if not busy:
                route = "quiet"
            elif period <= c.beep_busy_period:
                # A machine's beeps are long enough to cross the threshold for a while; a television's chance rhythms
                # are often blips of the shortest length the detector can see. At most one short beep is allowed.
                chain = self._run(starts, period)
                if len(chain) >= c.beep_run and self._long_enough([length[t] for t in chain[:c.beep_run]]):
                    route = "run"
                if route is None and self._long_enough([length[t] for t in three]):
                    for end, band, per, other, other_len in self.sets:
                        if (band == k and s - end <= c.beep_confirm and abs(per - period) <= c.beep_even_steps
                                and not set(other) & set(three) and self._long_enough(other_len)):
                            route = "second set"
                            break
            if route:
                break
        self.sets = [x for x in self.sets if s - x[0] <= c.beep_confirm] + [
            (s, k, per, tri, tuple(length[t] for t in tri)) for per, tri in found]
        if route:
            self._candidate(s, k, len(mine), sum(p[2] for p in mine) / STEPS_PER_SECOND, self.run_min[k], busy)

    def _rise(self, k: int, start: int):
        """Debug numbers for a pulse in band k that began at step `start`: how much did the rest of the room move when it
        began? (far, low) in dB: the mean energy of the tone bands more than beep_far away, and of the nine low bands,
        over the pulse's first beep_hold steps against the half second before it. None if too little history."""
        c = self.c
        before = [r for r in self.recent if start - STEPS_PER_SECOND // 2 <= r[0] < start]
        during = [r for r in self.recent if start <= r[0] < start + c.beep_hold]
        if len(before) < STEPS_PER_SECOND // 4 or len(during) < c.beep_hold:
            return (None, None)
        far = [j for j in range(N_TONE) if abs(j - k) > c.beep_far]

        def change(pick):
            now = sum(pick(r) for r in during) / len(during)
            then = sum(pick(r) for r in before) / len(before)
            return round(_db(max(now / 4.0, FLOOR)) - _db(max(then / 4.0, FLOOR)), 1)

        return (change(lambda r: sum(r[1][j] for j in far) / len(far)), change(lambda r: r[2]))

    def _long_enough(self, lengths) -> bool:
        return sum(1 for x in lengths if x < self.c.beep_busy_min) <= self.c.beep_busy_short

    def _evenly_spaced(self, earlier, start: int):
        """Every (period, (first, second, start)) where the pulse starting at `start` and two of the `earlier` starts
        are evenly spaced."""
        out = []
        for i in range(len(earlier)):
            for j in range(i + 1, len(earlier)):
                a, b = earlier[j] - earlier[i], start - earlier[j]
                if abs(a - b) <= self.c.beep_even_steps:
                    out.append(((a + b) / 2.0, (earlier[i], earlier[j], start)))
        return out

    def _run(self, starts, period: float):
        """The pulses in a run, newest first: each one `period` before the next."""
        cur, chain = starts[-1], [starts[-1]]
        while True:
            prev = [t for t in starts if t < cur and abs(cur - period - t) <= self.c.beep_even_steps]
            if not prev:
                return chain
            cur = prev[-1]
            chain.append(cur)

    def _candidate(self, s: int, k: int, pulses: int, seconds: float, strength: float, busy: bool = False):
        """Queue an alert. A loud tone leaks into the bands either side, so wait a moment and keep the strongest."""
        kind = "sustained" if pulses == 0 else "repeat"
        for i, p in enumerate(self.pending):
            if p[4] == kind and abs(p[1] - k) <= self.c.beep_spread:
                if strength > p[5]:
                    self.pending[i] = (p[0], k, pulses, seconds, kind, strength, busy)
                return
        self.pending.append((s + self.c.beep_settle, k, pulses, seconds, kind, strength, busy))

    def _alert(self, k: int, s: int, now: int, pulses: int, seconds: float, kind: str, busy: bool = False) -> list:
        # repeating beeps and one long tone are separate warnings: a fridge that has beeped in threes for a
        # while and then goes continuous must still raise the second one
        quiet = self.quiet_until[kind]
        sp = self.c.beep_spread
        for j in range(max(0, k - sp), min(N_TONE, k + sp + 1)):
            if s < quiet[j]:
                return []
        # a chord or one loud sound lights several pitches that are far apart: one message, not one per pitch
        if kind == "repeat":
            if s < self.repeat_hold:
                return []
            self.repeat_hold = s + self.c.beep_merge
        for j in range(max(0, k - sp), min(N_TONE, k + sp + 1)):
            quiet[j] = s + self.c.beep_cooldown
        return [{"kind": "beep", "frame": now, "hz": TONE_BANDS[k], "pulses": pulses, "seconds": round(seconds, 1),
                 "busy": busy}]


class Detector:
    """Feed one frame of 64 numbers at a time; get back a list of events (usually empty)."""

    def __init__(self, cfg: Config = None):
        self.c = cfg or Config()
        self.t = 0
        self.knock = _Knock(self.c)
        self.beep = _Beep(self.c)
        self.knock.beep_busy = self._beep_busy      # route B stays silent while the beep side says the room is busy

    def _beep_busy(self) -> bool:
        return self.beep.s <= self.beep.busy_until

    def feed(self, frame) -> list:
        t = self.t
        self.t += 1
        return self.knock.step(t, frame) + self.beep.step(t, frame)

    def adopt(self, old: "Detector") -> dict:
        """Take over from `old`, a detector that has been running longer, what it knows that this one cannot yet.

        Both have heard the same sound, this one for a shorter time, so everything newer than this one's own start it
        already has: carrying it again would count the same pulse twice. What it lacks is what happened before it
        started: a quiet time or hold still running, a session of knocking, a room that went busy a minute ago, a set
        of beeps that a second set may now confirm. Each detector counts its own frames and steps from zero, so the
        old one's times are moved onto this one's clock by the difference between the two counters right now. Things
        that last (a quiet time, a hold) take the later of the two ends; nothing is added together."""
        df = self.t - old.t                       # frames: this clock minus the old one (negative: this started later)
        ds = self.beep.s - old.beep.s             # the same in beep steps
        kn, okn, bp, obp = self.knock, old.knock, self.beep, old.beep
        kn.session_until = max(kn.session_until, okn.session_until + df)
        before = old.t - self.t                   # the old clock's frame at which this one started listening
        kn.thuds = sorted([(f + df, lo, nr) for f, lo, nr in okn.thuds if f <= before] + kn.thuds)
        kn.rises.update({f + df: r for f, r in okn.rises.items() if f <= before and f + df not in kn.rises})
        if kn.last_set is None and okn.last_set is not None and okn.last_set <= before:
            kn.last_set = okn.last_set + df
        for kind in bp.quiet_until:
            for j in range(N_TONE):
                bp.quiet_until[kind][j] = max(bp.quiet_until[kind][j], obp.quiet_until[kind][j] + ds)
        bp.repeat_hold = max(bp.repeat_hold, obp.repeat_hold + ds)
        bp.busy_until = max(bp.busy_until, obp.busy_until + ds)
        start = obp.s - bp.s                      # the old clock's step at which this one started listening
        bp.sets = [(e + ds, band, per, tuple(x + ds for x in tri), ls) for e, band, per, tri, ls in obp.sets if e <= start] + bp.sets
        return {"offset_frames": df, "offset_steps": ds}

    def idle(self) -> bool:
        """True when no pattern is part-way through, so this detector can be replaced without losing half an event:
        no thud pending or recent enough to start a knock, no tone being held, none queued to alert, and no recent
        pulse that could be the first of three."""
        c, k, b = self.c, self.knock, self.beep
        if k.cand is not None:
            return False
        if k.last_peak is not None and self.t - k.last_peak <= c.gap_max + c.peak_merge:
            return False
        if b.pending or any(r > 0 for r in b.run):
            return False
        if b.pulses:
            last = b.pulses[-1]
            if b.s - (last[1] + last[2]) <= c.beep_window:
                return False
        return True

    def peaks(self) -> dict:
        """Strongest knock-band rise and beep-band prominence since the last call, in dB. For the numbers-only log."""
        out = {"knock_db": round(self.knock.max_db, 1), "beep_db": round(self.beep.max_prom, 1)}
        self.knock.max_db = -99.0
        self.beep.max_prom = -99.0
        return out
