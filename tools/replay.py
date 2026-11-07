#!/usr/bin/env python3
"""Replay recordings through the real ffmpeg graph and the detector, and print what would have been alerted.

  python3 tools/replay.py <file.wav> [<file.wav> ...]

Recordings are never stored by the plugin; this tool only reads files you give it.
"""
import array
import importlib.util
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load():
    spec = importlib.util.spec_from_file_location("signal_dog", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["signal_dog"] = mod
    spec.loader.exec_module(mod)
    return mod


def frames_of(graph, path):
    p = subprocess.run(graph.command("file:" + path, realtime=False), capture_output=True, env=graph.environment())
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[:300])
    a = array.array("f")
    a.frombytes(p.stdout[: len(p.stdout) // graph.FRAME_BYTES * graph.FRAME_BYTES])
    n = graph.CHANNELS
    return [a[i * n:(i + 1) * n] for i in range(len(a) // n)]


def describe(ev):
    if ev["kind"] == "knock":
        return "KNOCK x%d at %.2fs (%.0f dB)" % (ev["thuds"], ev["frame"] / 100.0, ev["level_db"])
    return "BEEP %d Hz pulses=%d %.1fs at %.2fs" % (ev["hz"], ev["pulses"], ev["seconds"], ev["frame"] / 100.0)


def main():
    pkg = load()
    from signal_dog import detect, graph  # noqa: E402
    debug = "--pulses" in sys.argv
    settings = {}
    args = sys.argv[1:]
    paths = []
    while args:
        a = args.pop(0)
        if a == "--set":
            k, v = args.pop(0).split("=")
            settings[k] = float(v) if "." in v else int(v)
        elif not a.startswith("--"):
            paths.append(a)
    for path in paths:
        try:
            det = detect.Detector(detect.Config(debug=debug, **settings))
            events = []
            for fr in frames_of(graph, path):
                events += det.feed(fr)
            extra = ""
            if debug:
                extra = " | sharp thuds dropped=%d; pulses(Hz,start s,steps)=%s" % (det.knock.sharp, det.beep.log)
            print("%-24s %s%s" % (pathlib.Path(path).name, "; ".join(describe(e) for e in events) or "-", extra), flush=True)
        except Exception as e:
            print("%-24s ERROR %s" % (pathlib.Path(path).name, str(e)[:200]))


if __name__ == "__main__":
    main()
