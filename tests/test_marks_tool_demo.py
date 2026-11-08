"""/signaldog wrong and right, the agent's alerts tool, and /signaldog demo.
A fake chat stands in for Hermes. Only the last class needs ffmpeg."""
import contextvars
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import types
import unittest
import wave
from unittest import mock

import _pkg

_pkg.load()
from signal_dog import demo, history, listener  # noqa: E402
import signal_dog  # noqa: E402
from test_debug_numbers import load_tool  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def private_dir(path):
    """Only the owner can read the folder. Windows reports no POSIX mode bits; there mkdtemp makes the folder inside the
    user's own temporary folder, which only that user can open, so the check is that it is under it."""
    if os.name == "nt":
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(tempfile.gettempdir())]) == os.path.abspath(tempfile.gettempdir())
    return (os.stat(path).st_mode & 0o077) == 0


def stamp(ago_s):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - ago_s))


class Handle:
    def __init__(self, ok, error=None):
        self.ok, self.error, self.result = ok, error, {"ok": ok}


class Ctx:
    def __init__(self):
        self.posted, self.cfg, self.surface = [], {}, "cli"
        self.commands, self.hooks, self.unload, self.tools = {}, {}, [], {}
        self.fail = False

    def register_command(self, name, fn, **kw):
        self.commands[name] = (fn, kw)

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = (toolset, schema, handler, kw)

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def on_unload(self, fn):
        self.unload.append(fn)

    def get_config(self, key, default=None):
        return self.cfg.get(key, default)

    def session_surface(self):
        return self.surface

    def post_message(self, text):
        if self.fail:
            return Handle(False, "no_conversation")
        self.posted.append(text)
        return Handle(True)


class StubListener:
    """Stands in for a real listener. `state` and the numbers it reports can be set by the test."""
    state, uptime, age, error = "listening", 125, 0.1, ""

    def __init__(self, source, on_event, **kw):
        self.source = source

    def start(self):
        pass

    def stop(self):
        pass

    def status(self):
        return {"state": self.state, "error": self.error, "source": self.source, "uptime_s": self.uptime, "frames": 1,
                "last_frame_age_s": self.age, "restarts": 0, "events": 0, "recent": []}


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"HERMES_HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.log = os.path.join(self.home, "signal-dog", "events.log")
        self.ctx = Ctx()
        self.p = signal_dog.register(self.ctx)
        self.p.stopping.set()                       # no waits between delivery retries
        self.addCleanup(self.p.shutdown)

    def put(self, lines, name="events.log"):
        path = os.path.join(self.home, "signal-dog", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for ago_s, text in lines:
                fh.write("%s %s\n" % (stamp(ago_s), text))

    def knock(self, ago_s):
        return (ago_s, "event kind=knock frame=100 thuds=3 level_db=30.1 high_db=2.0")

    def beep(self, ago_s, hz=3624, pulses=4, busy=False, seconds=1.2):
        return (ago_s, "event kind=beep frame=200 hz=%d pulses=%d seconds=%s busy=%s" % (hz, pulses, seconds, busy))

    def cmd(self, text, platform="cli", source=None, key="k1"):
        return contextvars.copy_context().run(self.p.handle, text, platform=platform, session_key=key, source=source)

    def lines(self):
        with open(self.log, encoding="utf-8") as fh:
            return fh.read().splitlines()

    def use_stub(self, state="listening", **attrs):
        patcher = mock.patch.object(signal_dog, "Listener", type("S", (StubListener,), dict(attrs, state=state)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cmd("on")

    def tool(self, **args):
        return json.loads(self.ctx.tools[signal_dog.TOOL_NAME][2](args))


class MarkTests(Base):
    def test_wrong_marks_the_latest_alert_with_its_kind_pitch_and_time(self):
        self.put([self.knock(7200), self.beep(3600)])
        reply = self.cmd("wrong the fridge was fine")
        self.assertIn("Marked the beep", reply)
        self.assertIn("as wrong", reply)
        self.assertIn("3.6 kHz", reply)
        last = self.lines()[-1]
        at = [ln for ln in self.lines() if " event kind=beep" in ln][0].split(" ")[0]
        self.assertEqual(last.split(" ", 1)[1], "mark verdict=wrong kind=beep hz=3624 alert_at=%s note=the fridge was fine" % at)

    def test_right_marks_a_knock_which_has_no_pitch(self):
        self.put([self.knock(60)])
        self.assertIn("Marked the knock", self.cmd("right"))
        last = self.lines()[-1]
        self.assertIn(" mark verdict=right kind=knock alert_at=", last)
        self.assertNotIn(" hz=", last)
        self.assertNotIn(" note=", last)

    def test_no_alert_to_mark_says_so_and_writes_nothing(self):
        self.assertIn("no alert to mark", self.cmd("wrong"))
        self.assertFalse(os.path.exists(self.log))                       # no log at all: none is made
        for line in ((60, "minute frames=6000 knock_db=1 beep_db=2 sharp_dropped=0 restarts=0"), self.knock(30 * 3600)):
            shutil.rmtree(os.path.dirname(self.log), ignore_errors=True)
            self.put([line])
            before = self.lines()
            self.assertIn("no alert to mark", self.cmd("wrong"))
            self.assertEqual(self.lines(), before)

    def test_an_alert_can_be_marked_again_and_the_latest_mark_counts(self):
        self.put([self.beep(60)])
        self.cmd("wrong")
        reply = self.cmd("right")
        self.assertIn("it was marked wrong before", reply)
        snap = history.read(self.log)
        self.assertEqual(snap.counts(), {"right": 1, "wrong": 0})
        self.assertEqual(snap.alerts[0]["mark"], "right")

    def test_the_note_is_short_one_line_and_cannot_look_like_a_log_field(self):
        self.put([self.knock(60)])
        self.cmd("wrong pulse hz=1 start_s=1 steps=1\x07 recycle done thud frame=1=\n and more")
        self.cmd("wrong " + "x" * 300)
        marks = [ln for ln in self.lines() if " mark " in ln]
        self.assertEqual(len(marks), 2)
        self.assertTrue(marks[0].endswith(" note=pulse hz 1 start_s 1 steps 1 recycle done thud frame 1 and more"))
        self.assertEqual(len(marks[1].split(" note=", 1)[1]), history.NOTE_LIMIT)
        self.assertEqual(len(self.lines()), 3)                            # the alert and two single-line marks
        self.assertEqual(history.clean_note("a\tb\x00c  d=e"), "a b c d e")

    def test_a_mark_line_is_ignored_by_the_replay_tools(self):
        self.put([self.knock(60)])
        self.cmd("wrong recycle done start source thud pulse frame low_db steps")
        line = [ln for ln in self.lines() if " mark " in ln][0]
        thuds = load_tool("replay_thuds")
        self.assertIsNone(thuds.START.search(line))
        self.assertIsNone(thuds.THUD.search(line))
        self.assertIsNone(thuds.replay_pulses.PULSE.search(line))

    def test_status_shows_how_many_right_and_wrong_with_the_listener_off_and_on(self):
        self.put([self.knock(300), self.beep(200), self.beep(100)])
        snap = history.read(self.log)
        for a, verdict in zip(snap.alerts[:2], ("right", "wrong")):
            self.put([(50, "mark verdict=%s kind=%s alert_at=%s" % (verdict, a["kind"], a["at"]))])
        text = self.cmd("status")
        self.assertIn("1 right, 1 wrong; 1 alerts in the numbers log are not marked", text)
        self.assertIn("not listening", text)
        self.use_stub()
        self.assertIn("1 right, 1 wrong; 1 alerts in the numbers log are not marked", self.cmd("status"))

    def test_right_and_wrong_refuse_for_ten_minutes_after_a_demo_and_write_nothing(self):
        """A demo's alerts are not in the log, so the newest logged alert is an older real one: it must not be marked."""
        self.put([self.knock(3600)])
        before = self.lines()
        self.p.demo_done_at = time.time() - 60
        for word in ("wrong", "right"):
            reply = self.cmd(word)
            self.assertIn("demo", reply)
            self.assertIn("nothing to mark", reply)
        self.assertEqual(self.lines(), before)
        self.p.demo_done_at = time.time() - signal_dog.DEMO_MARK_PAUSE_S - 5
        self.assertIn("Marked the knock", self.cmd("wrong"))
        self.p.demo_done_at = None
        self.assertIn("Marked the knock", self.cmd("right"))

    def test_the_tool_description_starts_with_what_it_is_for(self):
        """Hermes lists a deferred tool by the first sentence of its description, so that sentence must tell the agent
        when to reach for it."""
        first = signal_dog.TOOL_DESCRIPTION.split(". ")[0].lower()
        for word in ("hear", "knock", "beep"):
            self.assertIn(word, first)
        self.assertNotEqual(first.strip(), "read-only")

    def test_status_is_quiet_about_marks_when_there_is_no_log(self):
        self.assertNotIn("marks", self.cmd("status"))

    def test_marks_follow_the_owner_rules(self):
        self.put([self.knock(60)])
        who = types.SimpleNamespace(user_id="999", chat_id="c", chat_type="dm")
        before = self.lines()
        self.assertIn("cannot tell who sent", self.cmd("wrong", platform="telegram", source=None))
        self.assertIn("Only the owner", self.cmd("wrong", platform="telegram", source=who))
        self.p.pin = ("telegram", "123")
        self.assertIn("Only the owner", self.cmd("right", platform="telegram", source=who))
        self.assertEqual(self.lines(), before)
        owner = types.SimpleNamespace(user_id="123", chat_id="c", chat_type="dm")
        self.assertIn("Marked the knock", self.cmd("wrong", platform="telegram", source=owner))
        self.ctx.cfg["owner_ids"] = ["555"]
        self.assertIn("Only the owner", self.cmd("wrong", platform="telegram", source=owner))

    def test_the_log_size_limit_still_applies(self):
        self.put([self.knock(60), (50, "minute " + "x" * 300)])
        with mock.patch.object(listener, "LOG_LIMIT", 200):
            self.cmd("wrong")
        self.assertTrue(os.path.exists(self.log + ".1"))
        self.assertEqual(len(self.lines()), 1)             # rotated: the mark is alone in the new file
        self.assertIn(" mark verdict=wrong", self.lines()[0])
        snap = history.read(self.log)                      # the alert is in the old file, the mark in the new one
        self.assertEqual(snap.alerts[0]["mark"], "wrong")

    def test_a_log_that_cannot_be_read_or_written_is_reported(self):
        os.makedirs(self.log)                              # a folder where the log should be
        self.assertIn("cannot read its numbers log", self.cmd("wrong"))
        self.assertIn("cannot be read", self.cmd("status"))
        os.rmdir(self.log)
        self.put([self.knock(60)])
        with mock.patch.object(signal_dog.NumbersLog, "line", return_value=False):
            self.assertIn("could not be saved", self.cmd("wrong"))

    def test_usage_lists_the_new_commands(self):
        text = self.cmd("bogus")
        for word in ("wrong", "right", "demo", "knocks on|off"):
            self.assertIn(word, text)


class KnockPauseTests(Base):
    def queued(self):
        out = []
        while not self.p.outbox.empty():
            out.append(self.p.outbox.get_nowait())
        return out

    def test_knocks_off_holds_back_knock_messages_only_and_on_brings_them_back(self):
        self.assertIn("Knock messages are on", self.cmd("knocks"))
        self.assertIn("paused", self.cmd("knocks off"))
        self.queued()
        self.p.on_event({"kind": "knock", "frame": 1, "thuds": 3, "level_db": 25.0, "high_db": 0.0, "busy": False})
        self.p.on_event({"kind": "beep", "frame": 2, "hz": 3624, "pulses": 3, "seconds": 1.2, "busy": False})
        self.p.on_event({"kind": "down", "text": "test"})
        got = self.queued()
        self.assertEqual(len(got), 2)
        self.assertTrue(got[0].startswith("Beeping"))
        self.assertIn("stopped listening", got[1])
        self.assertIn("Knock messages are paused", self.cmd("status"))
        self.assertIn("Knock messages are on", self.cmd("knocks on"))
        self.p.on_event({"kind": "knock", "frame": 3, "thuds": 3, "level_db": 25.0, "high_db": 0.0, "busy": False})
        self.assertEqual(self.queued(), ["Knock at the door."])
        self.assertNotIn("paused", self.cmd("status"))

    def test_knocks_follows_the_owner_rules(self):
        who = types.SimpleNamespace(user_id="999", chat_id="c", chat_type="group")
        self.p.pin = ("telegram", "123")
        self.assertIn("Only the owner", self.cmd("knocks off", platform="telegram", source=who))
        self.assertTrue(self.p.knocks_on)

    def test_a_knock_in_a_thumping_room_says_so(self):
        text = signal_dog.caption({"kind": "knock", "busy": True})
        self.assertIn("maybe", text)
        self.assertIn("moving about", text)
        self.assertEqual(signal_dog.caption({"kind": "knock", "busy": False}), "Knock at the door.")
        self.assertEqual(signal_dog.caption({"kind": "knock"}), "Knock at the door.")


class ToolTests(Base):
    def test_registered_and_declared(self):
        toolset, schema, handler, kw = self.ctx.tools[signal_dog.TOOL_NAME]
        self.assertEqual(schema["name"], signal_dog.TOOL_NAME)
        self.assertEqual(schema["parameters"]["type"], "object")
        self.assertEqual(handler, self.p.recent_alerts)
        self.assertEqual(toolset, "signal_dog")
        text = (_pkg.ROOT / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("provides_tools:\n- " + signal_dog.TOOL_NAME, text)
        self.assertLess(len(schema["description"]), 800)
        # Hermes finds a deferred tool by keywords that appear in its text, so the plain words people use must be there
        for word in ("microphone", "sounds", "heard", "door", "knock", "beep", "alarm", "smoke alarm", "timer", "alerts", "doorbell"):
            self.assertIn(word, schema["description"].lower())

    def test_lists_recent_alerts_newest_first_with_times_kinds_pitches_and_marks(self):
        self.put([self.knock(7200), self.beep(3600), self.beep(1800, hz=2920, pulses=0, seconds=4.0, busy=False)])
        self.cmd("wrong")                                  # marks the newest (the steady tone)
        out = self.tool()
        rows = out["alerts"]
        self.assertEqual([r["kind"] for r in rows], ["beep", "beep", "knock"])
        self.assertEqual(rows[0]["pitch_hz"], 2920)
        self.assertEqual(rows[0]["pattern"], "steady tone for 4.0 s")
        self.assertEqual(rows[0]["mark"], "wrong")
        self.assertEqual(rows[1]["pitch_hz"], 3624)
        self.assertEqual(rows[1]["pattern"], "repeating beeps")
        self.assertEqual(rows[1]["mark"], "not marked")
        self.assertNotIn("pitch_hz", rows[2])
        self.assertRegex(rows[2]["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
        self.assertEqual(out["alerts_in_window"], 3)
        self.assertEqual(out["marked_in_window"], {"right": 0, "wrong": 1})
        self.assertIn("listener", out)

    def test_returns_a_json_string(self):
        self.assertIsInstance(self.ctx.tools[signal_dog.TOOL_NAME][2]({}), str)

    def test_notes_are_not_given_to_the_agent(self):
        self.put([self.knock(60)])
        self.cmd("wrong my private words")
        self.assertNotIn("private", self.ctx.tools[signal_dog.TOOL_NAME][2]({}))

    def test_missing_log_is_a_clear_answer_not_a_crash(self):
        out = self.tool()
        self.assertEqual(out["alerts"], [])
        self.assertEqual(out["log"]["status"], "missing")
        self.assertIn("nothing has been recorded", " ".join(out["notes"]))
        self.assertFalse(out["listener"]["listening"])

    def test_unreadable_log_is_a_clear_answer_not_a_crash(self):
        os.makedirs(self.log)
        out = self.tool()
        self.assertEqual(out["log"]["status"], "unreadable")
        self.assertIn("cannot read its numbers log", " ".join(out["notes"]))

    def test_a_handler_that_fails_inside_still_answers(self):
        with mock.patch.object(history, "read", side_effect=RuntimeError("boom")):
            out = json.loads(self.p.recent_alerts({}))
        self.assertIn("could not read", out["error"])
        self.assertNotIn("boom", out["error"])

    def test_a_listener_that_is_off_never_lets_an_empty_list_look_like_a_quiet_house(self):
        self.put([(100, "minute frames=1 knock_db=1 beep_db=1 sharp_dropped=0 restarts=0")])
        out = self.tool()
        self.assertEqual(out["alerts"], [])
        self.assertFalse(out["listener"]["listening"])
        self.assertEqual(out["listener"]["state"], "off")
        joined = " ".join(out["notes"])
        self.assertIn("NOT listening", joined)
        self.assertIn("does not mean the house was quiet", joined)
        self.assertIn("last written at", joined)

    def test_a_listener_with_no_numbers_or_an_error_is_not_reported_as_listening(self):
        self.use_stub(age=120.0)
        out = self.tool()
        self.assertFalse(out["listener"]["listening"])
        self.assertIn("NOT listening", " ".join(out["notes"]))
        self.p.listener.__class__.age, self.p.listener.__class__.state = 0.1, "error"
        self.p.listener.__class__.error = "The microphone could not be opened: x"
        out = self.tool()
        self.assertFalse(out["listener"]["listening"])
        self.assertIn("microphone could not be opened", out["listener"]["problem"])

    def test_a_listener_that_is_on_says_so_and_admits_when_it_began_late(self):
        self.use_stub()
        out = self.tool()
        self.assertTrue(out["listener"]["listening"])
        joined = " ".join(out["notes"])
        self.assertNotIn("NOT listening", joined)
        self.assertIn("Live listening began at", joined)
        self.assertIn("quiet there proves nothing", joined)

    def test_a_listener_that_has_been_on_the_whole_window_makes_no_late_start_claim(self):
        self.use_stub(uptime=60 * 3600)
        joined = " ".join(self.tool(hours=24)["notes"])
        self.assertNotIn("Live listening began", joined)
        self.assertIn("Live listening began", " ".join(self.tool(hours=100)["notes"]))

    def test_the_window_and_the_limit(self):
        self.put([self.knock(30 * 3600)] + [self.beep(600 - i, hz=3000 + i) for i in range(30)])
        out = self.tool()
        self.assertEqual(out["alerts_in_window"], 30)         # the old knock is outside 24 hours
        self.assertEqual(len(out["alerts"]), 20)
        self.assertIn("Only the newest 20 of 30", " ".join(out["notes"]))
        self.assertEqual([r["pitch_hz"] for r in self.tool(limit=5)["alerts"]], [3029, 3028, 3027, 3026, 3025])
        self.assertEqual(self.tool(hours=48)["alerts_in_window"], 31)
        self.assertEqual(self.tool(hours=-5)["window_hours"], 0.1)
        self.assertEqual(self.tool(hours=99999)["window_hours"], 168.0)
        self.assertEqual(len(self.tool(limit=999)["alerts"]), 30)
        for bad in ({"hours": "soon", "limit": None}, {"hours": float("nan")}):
            self.assertEqual(self.tool(**bad)["window_hours"], 24.0)
        self.assertEqual(json.loads(self.p.recent_alerts(None))["window_hours"], 24.0)

    def test_the_log_rotates_so_the_answer_says_where_it_starts(self):
        self.put([self.knock(100 * 3600)], name="events.log.1")
        self.put([self.beep(3600)])
        out = self.tool(hours=168)
        self.assertEqual([r["kind"] for r in out["alerts"]], ["beep", "knock"])      # both files are read
        self.assertIn("rotates", out["log"]["note"])
        self.assertIn("The log starts at", " ".join(out["notes"]))          # it begins 100 hours back, the window 168
        self.assertNotIn("The log starts at", " ".join(self.tool(hours=48)["notes"]))
        os.remove(self.log + ".1")                                         # a rotation that has dropped the older file
        self.assertEqual([r["kind"] for r in self.tool(hours=168)["alerts"]], ["beep"])

    def test_it_shows_when_the_listener_gave_up_but_not_when_that_was_before_the_window(self):
        self.put([(30 * 3600, "down reason=old trouble"), (600, "down reason=The microphone could not be opened: busy")])
        notes = " ".join(self.tool()["notes"])
        self.assertIn("gave up", notes)
        self.assertIn("busy", notes)
        self.assertNotIn("old trouble", notes)

    def test_a_beep_in_a_noisy_room_says_so(self):
        self.put([self.beep(60, busy=True), self.beep(30, busy=False)])
        rows = self.tool()["alerts"]
        self.assertNotIn("noisy_room", rows[0])
        self.assertTrue(rows[1]["noisy_room"])

    def test_a_mark_line_with_an_unknown_verdict_counts_for_nothing(self):
        self.put([self.knock(60)])
        at = history.read(self.log).alerts[0]["at"]
        self.put([(30, "mark verdict=maybe kind=knock alert_at=%s" % at), (20, "mark kind=knock alert_at=%s" % at),
                  (10, "mark verdict=wrong kind=knock"), (5, "mark verdict=wrong alert_at=%s" % at)])
        snap = history.read(self.log)
        self.assertEqual(snap.counts(), {"right": 0, "wrong": 0})
        self.assertIsNone(snap.alerts[0]["mark"])

    def test_a_log_longer_than_the_read_limit_is_read_from_its_end_without_the_cut_line(self):
        self.put([self.knock(3000 - i) for i in range(40)])
        full = len(history.read(self.log).alerts)
        with mock.patch.object(history, "READ_LIMIT", 600):
            cut = history.read(self.log)
        self.assertEqual(full, 40)
        self.assertTrue(0 < len(cut.alerts) < 40)
        self.assertEqual(cut.alerts[-1]["at"], history.read(self.log).alerts[-1]["at"])      # the newest is kept
        self.assertTrue(all(a["kind"] == "knock" for a in cut.alerts))                      # and no half line sneaks in

    def test_stamps_and_ages(self):
        self.assertIsNotNone(history.parse_stamp("2026-11-07T17:02:11+1100"))
        self.assertEqual(history.parse_stamp("2026-11-07T17:02:11") - history.parse_stamp("2026-11-07T17:00:11"), 120)
        self.assertIsNone(history.parse_stamp("not a time"))
        self.assertEqual([history.ago(s) for s in (5, 600, 7200, 3 * 86400)], ["5 s ago", "10 min ago", "2.0 h ago", "3 days ago"])

    def test_marking_again_with_the_same_verdict_does_not_claim_a_change(self):
        self.put([self.beep(60)])
        self.cmd("wrong")
        self.assertNotIn("before", self.cmd("wrong"))
        self.assertEqual(history.read(self.log).counts(), {"right": 0, "wrong": 1})

    def test_a_mark_is_not_a_sign_the_listener_was_running(self):
        self.put([self.knock(3 * 3600)])
        self.cmd("wrong")
        snap = history.read(self.log)
        self.assertEqual(snap.last_line, snap.alerts[0]["ts"])             # the mark line is newer but does not count

    def test_it_only_reads_and_uses_no_network(self):
        self.put([self.knock(60), self.beep(30)])
        before = {n: (os.path.getsize(os.path.join(self.home, "signal-dog", n)), os.path.getmtime(os.path.join(self.home, "signal-dog", n)))
                  for n in os.listdir(os.path.dirname(self.log))}
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("process")):
            self.tool()
        after = {n: (os.path.getsize(os.path.join(self.home, "signal-dog", n)), os.path.getmtime(os.path.join(self.home, "signal-dog", n)))
                 for n in os.listdir(os.path.dirname(self.log))}
        self.assertEqual(before, after)


class FakeDemoListener:
    """Plays the part of the real listener for a demo: raises its alerts at once and finishes."""
    seen = {}
    events = [{"kind": "knock", "frame": 1}, {"kind": "beep", "frame": 2, "hz": 3624, "pulses": 4, "seconds": 1.0, "busy": False}]

    def __init__(self, source, on_event, **kw):
        FakeDemoListener.seen = {"source": source, "kw": kw, "existed": os.path.exists(source[5:]),
                                 "private": private_dir(os.path.dirname(source[5:]))}
        self.on_event, self.state, self.error = on_event, "stopped", ""

    def start(self):
        self.state = "starting"
        for ev in self.events:
            self.on_event(ev)
        self.state = "finished"

    def stop(self):
        pass

    def status(self):
        return {"state": self.state}


class DemoTests(Base):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.object(tempfile, "tempdir", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def have_ffmpeg(self):
        patcher = mock.patch.object(signal_dog.shutil, "which", return_value="/usr/bin/ffmpeg")
        patcher.start()
        self.addCleanup(patcher.stop)

    def wait_demo(self, seconds=20):
        t = self.p.demo_thread
        t.join(seconds)
        self.assertFalse(t.is_alive())

    def test_refuses_while_real_listening_is_on(self):
        self.have_ffmpeg()
        for state in ("listening", "starting"):
            self.use_stub(state=state)
            reply = self.cmd("demo")
            self.assertIn("listening for real", reply)
            self.assertIsNone(self.p.demo_thread)
            self.cmd("off")
        self.assertEqual(os.listdir(self.tmp), [])

    def test_runs_when_the_real_listener_has_given_up(self):
        self.have_ffmpeg()
        self.use_stub(state="error")
        with mock.patch.object(signal_dog, "Listener", FakeDemoListener):
            self.assertIn("demo started", self.cmd("demo"))
            self.wait_demo()

    def test_says_so_when_ffmpeg_is_missing(self):
        with mock.patch.dict(os.environ, {"PATH": self.tmp}):
            reply = self.cmd("demo")
        self.assertIn("needs ffmpeg", reply)
        self.assertIn("No microphone is needed", reply)
        self.assertIsNone(self.p.demo_thread)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_follows_the_owner_rules(self):
        self.have_ffmpeg()
        who = types.SimpleNamespace(user_id="999", chat_id="c", chat_type="dm")
        self.assertIn("cannot tell who sent", self.cmd("demo", platform="telegram", source=None))
        self.assertIn("Only the owner", self.cmd("demo", platform="telegram", source=who))
        self.p.pin = ("telegram", "123")
        self.assertIn("Only the owner", self.cmd("demo", platform="telegram", source=who))
        self.assertIsNone(self.p.demo_thread)

    def test_every_alert_is_labelled_the_recording_is_private_and_deleted_and_nothing_is_logged(self):
        self.have_ffmpeg()
        with mock.patch.object(signal_dog, "Listener", FakeDemoListener):
            self.assertIn("demo started", self.cmd("demo"))
            self.wait_demo()
        seen = FakeDemoListener.seen
        self.assertTrue(seen["source"].startswith("file:"))
        self.assertTrue(seen["existed"])
        self.assertTrue(seen["private"])
        self.assertEqual(seen["kw"]["log_path"], "")
        self.assertEqual(os.listdir(self.tmp), [])
        self.assertEqual(self.ctx.posted[0], "DEMO (not a real sound): Knock at the door.")
        self.assertTrue(self.ctx.posted[1].startswith("DEMO (not a real sound): Beeping"))
        self.assertIn("None of it was a real sound", self.ctx.posted[2])
        self.assertTrue(all("DEMO" in m or "demo" in m for m in self.ctx.posted))
        self.assertEqual(self.p.demo_result, {"alerts": ["knock", "beep"], "posted": 3, "failed": 0})
        self.assertFalse(os.path.exists(self.log))
        self.assertEqual((self.p.sent, self.p.failed, len(self.p.alert_times), self.p.last_alert, len(self.p.missed)), (0, 0, 0, "", 0))

    def test_a_demo_that_cannot_deliver_does_not_crash_or_queue_demo_alerts(self):
        self.have_ffmpeg()
        self.ctx.fail = True
        with mock.patch.object(signal_dog, "Listener", FakeDemoListener):
            self.cmd("demo")
            self.wait_demo()
        self.assertEqual(self.p.demo_result["failed"], 3)
        self.assertEqual(len(self.p.missed), 0)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_a_demo_that_raises_nothing_says_it_is_a_fault(self):
        self.have_ffmpeg()
        with mock.patch.object(signal_dog, "Listener", FakeDemoListener), mock.patch.object(FakeDemoListener, "events", []):
            self.cmd("demo")
            self.wait_demo()
        self.assertIn("which is a fault", self.ctx.posted[-1])

    def test_a_listener_error_is_reported_as_a_demo_stop(self):
        self.have_ffmpeg()

        class Failing(FakeDemoListener):
            events = [{"kind": "down", "text": "ffmpeg was not found."}]

            def start(self):
                super().start()
                self.state, self.error = "error", "ffmpeg was not found."

        with mock.patch.object(signal_dog, "Listener", Failing):
            self.cmd("demo")
            self.wait_demo()
        self.assertEqual(self.ctx.posted[0], "DEMO stopped: ffmpeg was not found.")
        self.assertEqual(os.listdir(self.tmp), [])

    def test_one_demo_at_a_time_and_off_stops_it(self):
        self.have_ffmpeg()
        t = threading.Thread(target=lambda: self.p.demo_stop.wait(30), daemon=True)
        t.start()
        self.p.demo_thread = t
        self.assertIn("already running", self.cmd("demo"))
        self.assertIn("A demo is running", self.cmd("status"))
        self.assertIn("demo was stopped", self.cmd("off"))
        self.assertFalse(t.is_alive())
        self.assertNotIn("A demo is running", self.cmd("status"))

    def test_a_demo_that_never_finishes_is_stopped_after_its_grace_time_and_says_so(self):
        self.have_ffmpeg()

        class Silent(FakeDemoListener):
            events = []

            def start(self):
                self.state = "starting"               # never finishes

        with mock.patch.object(signal_dog, "Listener", Silent), mock.patch.object(signal_dog, "DEMO_GRACE_S", -100.0):
            self.cmd("demo")
            self.wait_demo()
        self.assertIn("which is a fault", self.ctx.posted[-1])
        self.assertEqual(os.listdir(self.tmp), [])

    def test_off_and_unload_stop_a_demo_quietly_and_clean_up(self):
        self.have_ffmpeg()

        class Silent(FakeDemoListener):
            events = []

            def start(self):
                self.state = "starting"

        for stop in (lambda: self.cmd("off"), self.p.shutdown):
            self.ctx.posted.clear()
            with mock.patch.object(signal_dog, "Listener", Silent):
                self.cmd("demo")
                t = self.p.demo_thread
                deadline = time.time() + 10
                while not FakeDemoListener.seen.get("existed") and time.time() < deadline:
                    time.sleep(0.05)
                stop()
            self.assertFalse(t.is_alive())
            self.assertEqual(self.ctx.posted, [])                  # stopped by the owner: no "finished" message
            self.assertEqual(os.listdir(self.tmp), [])
            FakeDemoListener.seen = {}

    def test_a_demo_can_run_after_an_earlier_one_was_stopped(self):
        self.have_ffmpeg()

        class Silent(FakeDemoListener):
            events = []

            def start(self):
                self.state = "starting"

        with mock.patch.object(signal_dog, "Listener", Silent):
            self.cmd("demo")
            self.cmd("off")
        with mock.patch.object(signal_dog, "Listener", FakeDemoListener):
            self.assertIn("demo started", self.cmd("demo"))
            self.wait_demo()
        self.assertEqual(self.p.demo_result["alerts"], ["knock", "beep"])
        self.assertIn("None of it was a real sound", self.ctx.posted[-1])
        self.assertIn("nothing to mark", self.cmd("wrong"))        # a demo just ended: marking would hit an older real alert

    def test_switching_on_stops_a_demo_first(self):
        t = threading.Thread(target=lambda: self.p.demo_stop.wait(30), daemon=True)
        t.start()
        self.p.demo_thread = t
        with mock.patch.object(signal_dog, "Listener", StubListener):
            self.assertIn("listening", self.cmd("on"))
        self.assertFalse(t.is_alive())

    def test_the_recording_is_made_by_code_and_is_a_valid_wave_file(self):
        path = os.path.join(self.tmp, "d.wav")
        seconds = demo.write_demo(path)
        with wave.open(path, "rb") as w:
            self.assertEqual((w.getnchannels(), w.getframerate()), (1, demo.RATE))
            self.assertAlmostEqual(w.getnframes() / demo.RATE, seconds, places=2)
        audio = _pkg.ROOT / "tests" / "audio.py"
        self.assertNotIn("def synth", audio.read_text(encoding="utf-8"))      # one copy of the logic, in the plugin


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is not installed")
class DemoEndToEndTests(Base):
    def test_the_demo_through_the_real_ffmpeg_graph_detector_and_delivery(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        made = {}
        real_write = demo.write_demo

        def spy(path):
            made["dir_private"] = private_dir(os.path.dirname(path))
            made["path"] = path
            return real_write(path)

        self.p.stopping.clear()
        with mock.patch.object(tempfile, "tempdir", tmp), mock.patch.object(signal_dog.demo_mod, "write_demo", spy):
            reply = self.cmd("demo")
            self.assertIn("demo started", reply)
            self.p.demo_thread.join(90)
            self.assertFalse(self.p.demo_thread.is_alive())               # it stopped by itself
        self.assertTrue(made["dir_private"])
        self.assertFalse(os.path.exists(made["path"]))                    # and deleted the recording
        self.assertEqual(os.listdir(tmp), [])
        self.assertEqual(self.p.demo_result["alerts"], ["knock", "beep"])
        alerts = self.ctx.posted[:-1]
        self.assertEqual(len(alerts), 2)
        self.assertTrue(all(m.startswith(signal_dog.DEMO_LABEL) for m in alerts))
        self.assertIn("Knock at the door.", alerts[0])
        self.assertIn("3.6 kHz", alerts[1])
        self.assertNotIn("Knock at the door.", self.ctx.posted)           # never an unlabelled alert
        self.assertIn("None of it was a real sound", self.ctx.posted[-1])
        self.assertFalse(os.path.exists(self.log))                        # no numbers log line, so no real-alert history
        self.assertEqual(self.p.demo_result["failed"], 0)
        self.assertEqual(self.tool()["alerts"], [])


if __name__ == "__main__":
    unittest.main()
