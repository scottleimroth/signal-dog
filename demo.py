"""A synthetic recording made by code, for `/signaldog demo` and for the tests. No sound from a microphone is involved
and nothing binary is shipped: the wave file is written at run time, played once, and deleted."""

from __future__ import annotations

import math
import random
import struct
import wave

RATE = 16000
DEMO_SECONDS = 14.0
DEMO_KNOCK_AT = 3.0
DEMO_BEEPS = (8.0, 4, 3600)         # start, count, hertz


def synth(path, seconds, knocks=(), thumps=(), beeps=(), fan=True, seed=1):
    """Room noise, a steady fan hum, and the sounds Signal Dog should react to, written as a 16 kHz mono wave file.
    knocks: start times of a knock (three dull thuds 250 ms apart).
    thumps: start times of a sharp desk thump (a broadband click followed by a dull rebound).
    beeps: (start, count, hz) groups of 200 ms beeps with 120 ms gaps."""
    rnd = random.Random(seed)
    n = int(seconds * RATE)
    x = [rnd.gauss(0.0, 0.003) for _ in range(n)]                   # room noise, about -50 dBFS
    if fan:
        for i in range(n):
            t = i / RATE
            x[i] += 0.004 * math.sin(2 * math.pi * 57 * t) + 0.003 * math.sin(2 * math.pi * 230 * t) + 0.003 * math.sin(2 * math.pi * 2300 * t)

    def add(start, f1, f2, amp, decay, length):
        s = int(start * RATE)
        for i in range(int(length * RATE)):
            if s + i >= n:
                break
            t = i / RATE
            x[s + i] += amp * math.exp(-t / decay) * (math.sin(2 * math.pi * f1 * t) + 0.6 * math.sin(2 * math.pi * f2 * t))

    for t0 in knocks:
        for k in range(3):
            add(t0 + 0.25 * k, 120, 190, 0.15, 0.025, 0.1)
    for t0 in thumps:
        s = int(t0 * RATE)
        for i in range(int(0.02 * RATE)):                           # the strike: a short broadband click
            if s + i < n:
                x[s + i] += rnd.gauss(0.0, 0.25)
        add(t0, 120, 190, 0.02, 0.03, 0.1)                          # a little low thud under the click
        add(t0 + 0.22, 120, 190, 0.03, 0.025, 0.1)                  # the dull rebound
    for t0, count, hz in beeps:
        for k in range(count):
            s = int((t0 + 0.32 * k) * RATE)
            for i in range(int(0.2 * RATE)):
                if s + i < n:
                    x[s + i] += 0.03 * math.sin(2 * math.pi * hz * i / RATE)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", max(-32768, min(32767, int(v * 32767)))) for v in x))


def write_demo(path) -> float:
    """Write the demo recording (a knock, then four beeps) to `path`. Returns its length in seconds."""
    synth(path, DEMO_SECONDS, knocks=[DEMO_KNOCK_AT], beeps=[DEMO_BEEPS])
    return DEMO_SECONDS
