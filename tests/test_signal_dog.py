import contextvars
import os
import shutil
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import _pkg
import audio

_pkg.load()
from signal_dog import detect, graph, listener  # noqa: E402
import signal_dog  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def frame(low=1.0, high=1.0, tone=None):
    """One frame of 64 numbers: room level 1.0 everywhere unless told otherwise."""
    f = [1.0] * graph.CHANNELS
    for k in range(graph.N_LOW):
        f[k] = low
    f[graph.IDX_HIGH] = high
    for k, v in (tone or {}).items():
        f[graph.IDX_TONE + k] = v
    return f


def run(det, frames):
    out = []
    for f in frames:
        out += det.feed(f)
    return out


def knock_frames(sharp=False):
    """Three thuds 250 ms apart, 20 dB above the room in the low bands."""
    fr = []
    for k in range(3):
        for i in range(25):
            on = i < 5
            fr.append(frame(low=100.0 if on else 1.0, high=(100.0 if sharp else 1.0) if on else 1.0))
    return fr


class GraphTests(unittest.TestCase):
    def test_sixty_four_channels(self):
        self.assertEqual(graph.CHANNELS, 64)
        g = graph.filter_graph()
        self.assertIn("join=inputs=64:channel_layout=64C", g)
        self.assertNotIn("record", " ".join(graph.command()))

    def test_command_sources(self):
        self.assertIn("-re", graph.command("file:/x.wav"))
        self.assertNotIn("-re", graph.command("file:/x.wav", realtime=False))

    def test_each_system_opens_its_own_microphone(self):
        self.assertEqual(graph.microphone_input("default", "Linux"), ["-fflags", "nobuffer", "-f", "pulse", "-i", "default"])
        self.assertEqual(graph.microphone_input("alsa_input.usb", "Linux")[-1], "alsa_input.usb")
        self.assertEqual(graph.microphone_input("default", "Darwin"), ["-f", "avfoundation", "-i", ":0"])
        self.assertEqual(graph.microphone_input("2", "Darwin")[-1], ":2")
        self.assertEqual(graph.microphone_input("Microphone (USB)", "Windows")[-1], "audio=Microphone (USB)")
        with mock.patch.object(graph, "default_windows_microphone", return_value="Microphone (Rapoo Camera)"):
            self.assertEqual(graph.microphone_input("default", "Windows"),
                             ["-f", "dshow", "-audio_buffer_size", "50", "-i", "audio=Microphone (Rapoo Camera)"])

    def test_the_first_windows_microphone_is_read_from_ffmpegs_device_list(self):
        listing = "\n".join(['[in#0 @ 0] "Integrated Webcam" (video)', '[in#0 @ 0] "Microphone (Rapoo Camera)" (audio)',
                             '[in#0 @ 0]   Alternative name "@device_cm_x"', '[in#0 @ 0] "Line In (Realtek)" (audio)'])
        done = types.SimpleNamespace(stderr=listing.encode("utf-8"))
        with mock.patch.object(graph.subprocess, "run", return_value=done):
            self.assertEqual(graph.default_windows_microphone(), "Microphone (Rapoo Camera)")
        with mock.patch.object(graph.subprocess, "run", side_effect=OSError("no ffmpeg")):
            self.assertIsNone(graph.default_windows_microphone())

    # Review of 28bcd0a6: ffmpeg must not receive the agent's whole environment.
    _SECRETS = {"OPENAI_API_KEY": "sk-test", "ANTHROPIC_API_KEY": "x", "MATRIX_ACCESS_TOKEN": "y",
                "HERMES_DASHBOARD_SESSION_TOKEN": "z", "GITHUB_TOKEN": "g", "AWS_SECRET_ACCESS_KEY": "a"}

    def test_ffmpeg_environment_keeps_only_what_capture_needs(self):
        fake = {"PATH": "/usr/bin", "HOME": "/home/u", "LANG": "C.UTF-8", "XDG_RUNTIME_DIR": "/run/user/1000",
                "PULSE_SERVER": "unix:/run/pulse/native", "PULSE_COOKIE": "/home/u/.pulse-cookie", **self._SECRETS}
        env = graph.environment(fake)
        for name in ("PATH", "HOME", "LANG", "XDG_RUNTIME_DIR", "PULSE_SERVER", "PULSE_COOKIE"):
            self.assertEqual(env[name], fake[name])
        for name in self._SECRETS:
            self.assertNotIn(name, env)

    def test_ffmpeg_environment_on_windows_keeps_system_names_in_any_case(self):
        # os.environ on Windows reports names upper-cased; a mixed-case spelling must be kept too.
        fake = {"SYSTEMROOT": r"C:\Windows", "TEMP": r"C:\t", "USERPROFILE": r"C:\Users\u", "Path": r"C:\bin",
                "SystemDrive": "C:", **self._SECRETS}
        env = graph.environment(fake)
        for name in ("SYSTEMROOT", "TEMP", "USERPROFILE", "Path"):
            self.assertEqual(env[name], fake[name])
        self.assertNotIn("SystemDrive", env)
        for name in self._SECRETS:
            self.assertNotIn(name, env)

    def test_both_ffmpeg_launches_get_the_reduced_environment(self):
        with mock.patch.dict(os.environ, self._SECRETS):
            done = types.SimpleNamespace(stderr=b"")
            with mock.patch.object(graph.subprocess, "run", return_value=done) as run:
                graph.default_windows_microphone()
            env = run.call_args.kwargs.get("env")
            self.assertIsNotNone(env, "the -list_devices call must pass env= explicitly")
            for name in self._SECRETS:
                self.assertNotIn(name, env)
            with mock.patch.object(listener.subprocess, "Popen", side_effect=OSError("stop here")) as popen:
                stream = listener._Stream(owner=types.SimpleNamespace(), source="default", cfg=detect.Config())
                with self.assertRaises(OSError):
                    stream.start()
            env = popen.call_args.kwargs.get("env")
            self.assertIsNotNone(env, "the listener's ffmpeg must pass env= explicitly")
            for name in self._SECRETS:
                self.assertNotIn(name, env)

    def test_log_path_follows_the_active_hermes_profile(self):
        fake = types.ModuleType("hermes_constants")
        fake.get_hermes_home = lambda: "/profiles/active"
        with mock.patch.dict("sys.modules", {"hermes_constants": fake}), \
                mock.patch.dict(os.environ, {"HERMES_HOME": "/profiles/launcher"}):
            self.assertEqual(listener.default_log_path(), os.path.join("/profiles/active", "signal-dog", "events.log"))
        with mock.patch.dict("sys.modules", {"hermes_constants": None}), \
                mock.patch.dict(os.environ, {"HERMES_HOME": "/profiles/launcher"}):
            self.assertEqual(listener.default_log_path(), os.path.join("/profiles/launcher", "signal-dog", "events.log"))


class DetectorTests(unittest.TestCase):
    def quiet(self, seconds=1.0):
        return [frame() for _ in range(int(seconds * 100))]

    def test_quiet_room_raises_nothing(self):
        self.assertEqual(run(detect.Detector(), self.quiet(30)), [])

    def test_dull_knock_is_one_alert(self):
        ev = run(detect.Detector(), self.quiet() + knock_frames() + self.quiet(2))
        self.assertEqual([e["kind"] for e in ev], ["knock"])

    def test_sharp_thump_is_not_a_knock(self):
        ev = run(detect.Detector(), self.quiet() + knock_frames(sharp=True) + self.quiet(2))
        self.assertEqual(ev, [])

    def test_one_thud_is_not_a_knock(self):
        one = knock_frames()[:25] + self.quiet(2)
        self.assertEqual(run(detect.Detector(), self.quiet() + one), [])

    def test_knocking_session_alerts_once(self):
        ev = run(detect.Detector(), self.quiet() + knock_frames() + self.quiet(1) + knock_frames() + self.quiet(2))
        self.assertEqual(len([e for e in ev if e["kind"] == "knock"]), 1)

    def test_repeating_beeps_alert(self):
        beep = [frame(tone={30: 100.0}) if (i % 32) < 20 else frame() for i in range(96)]
        ev = run(detect.Detector(), self.quiet() + beep + self.quiet(2))
        beeps = [e for e in ev if e["kind"] == "beep"]
        self.assertEqual(len(beeps), 1)
        self.assertEqual(beeps[0]["hz"], graph.TONE_BANDS[30])

    def test_steady_fan_tone_is_not_a_beep(self):
        steady = [frame(tone={20: 100.0}) for _ in range(3000)]
        self.assertEqual(run(detect.Detector(), steady), [])

    def test_long_steady_tone_alerts_once_after_three_seconds(self):
        ev = run(detect.Detector(), self.quiet() + [frame(tone={10: 100.0}) for _ in range(700)])
        beeps = [e for e in ev if e["kind"] == "beep"]
        self.assertEqual(len(beeps), 1)
        self.assertEqual(beeps[0]["pulses"], 0)

    def test_unknown_setting_is_refused(self):
        with self.assertRaises(TypeError):
            detect.Config(nonsense=1)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is not installed")
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def events(self, **kw):
        path = os.path.join(self.dir, "t.wav")
        audio.synth(path, kw.pop("seconds"), **kw)
        import array
        import subprocess
        p = subprocess.run(graph.command("file:" + path, realtime=False), capture_output=True, env=graph.environment())
        self.assertEqual(p.returncode, 0, p.stderr.decode()[:300])
        a = array.array("f")
        a.frombytes(p.stdout[: len(p.stdout) // graph.FRAME_BYTES * graph.FRAME_BYTES])
        det = detect.Detector()
        out = []
        for i in range(len(a) // graph.CHANNELS):
            out += det.feed(a[i * graph.CHANNELS:(i + 1) * graph.CHANNELS])
        return out

    def test_room_with_fan_only(self):
        self.assertEqual(self.events(seconds=20), [])

    def test_real_knock_through_the_graph(self):
        ev = self.events(seconds=8, knocks=[3.0])
        self.assertEqual([e["kind"] for e in ev], ["knock"])

    def test_desk_thump_through_the_graph(self):
        self.assertEqual(self.events(seconds=8, thumps=[3.0]), [])

    def test_beeps_through_the_graph(self):
        ev = self.events(seconds=10, beeps=[(3.0, 3, 3600)])
        beeps = [e for e in ev if e["kind"] == "beep"]
        self.assertEqual(len(beeps), 1)
        self.assertLess(abs(beeps[0]["hz"] - 3600), 140)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is not installed")
class ListenerAndPluginTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.wav = os.path.join(self.dir, "t.wav")
        audio.synth(self.wav, 7, knocks=[3.0])

    def test_listener_reports_then_finishes(self):
        got = []
        ln = listener.Listener("file:" + self.wav, got.append, log_path=os.path.join(self.dir, "events.log"))
        ln.start()
        deadline = time.time() + 30
        while ln.state not in ("finished", "error") and time.time() < deadline:
            time.sleep(0.2)
        self.assertEqual(ln.state, "finished")
        self.assertEqual([e["kind"] for e in got], ["knock"])
        text = open(os.path.join(self.dir, "events.log"), encoding="utf-8").read()
        self.assertIn("event kind=knock", text)
        self.assertNotIn("RIFF", text)          # numbers only, never audio

    def test_missing_ffmpeg_is_reported_not_hidden(self):
        got = []
        old = os.environ.get("PATH")
        os.environ["PATH"] = self.dir           # no ffmpeg here
        try:
            ln = listener.Listener("default", got.append, log_path=os.path.join(self.dir, "e.log"))
            ln.start()
            ln.thread.join(10)
        finally:
            os.environ["PATH"] = old
        self.assertEqual(ln.state, "error")
        self.assertEqual(got[0]["kind"], "down")
        self.assertIn("ffmpeg was not found", got[0]["text"])

    def test_plugin_posts_alert_and_reports_failures(self):
        os.environ["HERMES_HOME"] = self.dir
        self.addCleanup(os.environ.pop, "HERMES_HOME", None)
        posted, fail = [], [False]

        class Handle:
            def __init__(self, ok, error=None):
                self.ok, self.error = ok, error

        class Ctx:
            commands, hooks, unload, tools = {}, {}, [], {}

            def register_command(self, name, fn, **kw):
                self.commands[name] = fn

            def register_tool(self, name, toolset, schema, handler, **kw):
                self.tools[name] = handler

            def register_hook(self, name, fn):
                self.hooks[name] = fn

            def on_unload(self, fn):
                self.unload.append(fn)

            def get_config(self, key, default=None):
                return default

            def session_surface(self):
                return "cli"

            def post_message(self, text):
                if fail[0]:
                    return Handle(False, "no_conversation")
                posted.append(text)
                return Handle(True)

        ctx = Ctx()
        plugin = signal_dog.register(ctx)
        self.assertEqual(list(ctx.commands), ["signaldog"])
        self.assertEqual(list(ctx.hooks), ["pre_llm_call"])
        reply = contextvars.copy_context().run(ctx.commands["signaldog"], "on file:" + self.wav, platform="cli")
        self.assertIn("listening", reply)
        deadline = time.time() + 30
        while "Knock at the door." not in posted and time.time() < deadline:
            time.sleep(0.2)
        self.assertIn("Knock at the door.", posted)
        self.assertEqual(posted.count("Knock at the door."), 1)          # the desk thump raised nothing
        status = contextvars.copy_context().run(ctx.commands["signaldog"], "status", platform="cli")
        self.assertIn("0 could not be sent at first", status)
        fail[0] = True
        plugin.stopping.set()                    # no retries in this test
        plugin._deliver("another alert")
        self.assertEqual(plugin.failed, 1)
        self.assertIn("no_conversation", plugin.last_fail)
        status = contextvars.copy_context().run(ctx.commands["signaldog"], "status", platform="cli")
        self.assertIn("1 could not be sent at first", status)
        self.assertIn("Waiting to be delivered: 1", status)
        plugin.shutdown()


if __name__ == "__main__":
    unittest.main()
