#!/usr/bin/env python3
"""Public recordings, mixed into steady room noise as audio, through the real ffmpeg graph and the real rules.

  python3 tools/public_clips.py <folder with ESC-50 .wav files and esc50.csv> [--level 25] [--set knock_even=8 ...]

ESC-50 is a public set of 5-second recordings in 50 categories (Piczak 2015, https://github.com/karolpiczak/ESC-50,
CC BY-NC for the set). Nothing here is stored in the repo; give it a folder that holds the files you downloaded (any
subset is fine: it uses the categories it finds). For each clip it
  1. makes 60 s of steady pink noise as the "room" (same every time),
  2. scales the clip as audio so that its loudest moment stands `--level` dB above that room (low bands for sounds
     that are not tones, the strongest tone band for the alarm-clock clips),
  3. adds it to the room after a 4 s lead-in, with a 3 s tail,
  4. runs the mix through the real ffmpeg graph and the real detector, and counts the clips that raised a knock alert
     and the clips that raised a beep alert.
A clip counts once however many alerts it raised. The level is the loudness of the clip's loudest moment, so a
footstep or a mouse click is scored at the loudness of a knock: this shows what each kind of sound can look like to the
rules at that loudness, not how often a house will produce it. It reads clip files only and writes only to a
temporary folder it removes.
"""
import array
import csv
import math
import os
import shutil
import subprocess
import sys
import tempfile
import wave

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import replay  # noqa: E402

RATE = 16000
LEAD, TAIL = RATE * 4, RATE * 3
KNOCK = "door_wood_knock"
ALARM = "clock_alarm"
OTHERS = ["footsteps", "keyboard_typing", "mouse_click", "clock_tick", "door_wood_creaks", "can_opening", "snoring"]


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def pcm16(path, tmp, name):
    dst = os.path.join(tmp, name + ".s16")
    ffmpeg("-i", path, "-ar", str(RATE), "-ac", "1", "-f", "s16le", dst)
    a = array.array("h")
    a.frombytes(open(dst, "rb").read())
    return a


def wave_of(samples, path):
    w = wave.open(path, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(RATE)
    w.writeframes(samples.tobytes())
    w.close()


def parse_settings(args):
    settings, rest = {}, []
    it = iter(args)
    for a in it:
        if a == "--set":
            k, v = next(it).split("=")
            settings[k] = float(v) if "." in v else int(v)
        else:
            rest.append(a)
    return settings, rest


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    settings, rest = parse_settings(argv)
    level, folder = 25, None
    while rest:
        a = rest.pop(0)
        if a == "--level":
            level = int(rest.pop(0))
        else:
            folder = a
    if folder is None or not os.path.isfile(os.path.join(folder, "esc50.csv")):
        print(__doc__)
        return 2
    replay.load()
    from signal_dog import detect, graph  # noqa: E402

    cats = {}
    for row in csv.DictReader(open(os.path.join(folder, "esc50.csv"), encoding="utf-8")):
        if os.path.isfile(os.path.join(folder, row["filename"])):
            cats.setdefault(row["category"], []).append(row["filename"])
    wanted = [c for c in [KNOCK, ALARM] + OTHERS if c in cats]
    if KNOCK not in wanted:
        print("No %s clips in %s." % (KNOCK, folder))
        return 2
    tmp = tempfile.mkdtemp(prefix="signal-dog-clips-")
    try:
        noise_wav = os.path.join(tmp, "room.wav")
        ffmpeg("-f", "lavfi", "-i", "anoisesrc=color=pink:amplitude=0.002:seed=7:sample_rate=%d:duration=60" % RATE, noise_wav)
        noise = pcm16(noise_wav, tmp, "room")

        def frames(wav):
            p = subprocess.run(graph.command("file:" + wav, realtime=False), capture_output=True, env=graph.environment())
            if p.returncode != 0:
                raise RuntimeError(p.stderr.decode("utf-8", "replace")[:300])
            return _split(graph, p.stdout)

        room = frames(noise_wav)
        n = graph.CHANNELS
        med = [sorted(f[k] for f in room)[len(room) // 2] for k in range(n)]

        def peak(fr, alarm):
            ks = range(graph.IDX_TONE, n) if alarm else range(graph.N_LOW)
            top = lambda f: max(max(f[k], 0.0) / med[k] for k in ks) if alarm else sum(max(f[k], 0.0) / med[k] for k in ks) / graph.N_LOW
            return max(top(f) for f in fr)

        found = {c: [0, 0, 0] for c in wanted}   # clips: knock alerts, beep alerts, total
        for c in wanted:
            for name in sorted(cats[c]):
                path = os.path.join(folder, name)
                clip = pcm16(path, tmp, "clip")
                alone = os.path.join(tmp, "alone.wav")
                wave_of(clip, alone)
                g = (10 ** (level / 10.0) - 1.0) / max(peak(frames(alone), c == ALARM), 1e-12)
                amp = math.sqrt(g)
                out = array.array("h", (noise[i % len(noise)] for i in range(LEAD + len(clip) + TAIL)))
                for i, v in enumerate(clip):
                    out[LEAD + i] = max(-32768, min(32767, out[LEAD + i] + int(v * amp)))
                mixed = os.path.join(tmp, "mixed.wav")
                wave_of(out, mixed)
                det = detect.Detector(detect.Config(**settings))
                ev = []
                for f in frames(mixed):
                    ev += det.feed(f)
                found[c][0] += any(e["kind"] == "knock" for e in ev)
                found[c][1] += any(e["kind"] == "beep" for e in ev)
                found[c][2] += 1
        det = detect.Detector(detect.Config(**settings))
        quiet = [e for f in room for e in det.feed(f)]
        print("Public clips mixed as audio into 60 s of steady room noise, each clip's loudest moment %d dB over the room." % level)
        if settings:
            print("Settings changed: %s" % ", ".join("%s=%s" % kv for kv in sorted(settings.items())))
        k = found[KNOCK]
        print("Door knocks that raised a knock alert: %d of %d" % (k[0], k[2]))
        others = [c for c in OTHERS if c in found]
        if others:
            print("Other sounds that raised a knock alert: %d of %d (%s)" % (
                sum(found[c][0] for c in others), sum(found[c][2] for c in others),
                ", ".join("%s %d of %d" % (c.replace("_", " "), found[c][0], found[c][2]) for c in others)))
        if ALARM in found:
            print("Alarm-clock clips that raised a beep alert: %d of %d" % (found[ALARM][1], found[ALARM][2]))
        if others:
            print("Other sounds that raised a beep alert: %d of %d (%s)" % (
                sum(found[c][1] for c in others) + found[KNOCK][1], sum(found[c][2] for c in others) + found[KNOCK][2],
                ", ".join("%s %d of %d" % (c.replace("_", " "), found[c][1], found[c][2]) for c in [KNOCK] + others if found[c][1])
                or "none"))
        print("The room noise alone raised %d alerts." % len(quiet))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _split(graph, data):
    a = array.array("f")
    a.frombytes(data[: len(data) // graph.FRAME_BYTES * graph.FRAME_BYTES])
    n = graph.CHANNELS
    return [a[i * n:(i + 1) * n] for i in range(len(a) // n)]


if __name__ == "__main__":
    sys.exit(main())
