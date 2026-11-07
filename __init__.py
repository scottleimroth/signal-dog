"""Signal Dog: listens through the microphone for household sounds and tells you in chat.

  /signaldog on [source]   start listening, and send alerts to this conversation
  /signaldog off           stop listening
  /signaldog status        what it is doing, and whether alerts are getting through
  /signaldog test          send a test message through the same route alerts use
  /signaldog wrong [note]  mark the latest alert as a false alarm (kept in the numbers log; changes nothing else)
  /signaldog right [note]  mark the latest alert as a true one
  /signaldog demo          play a recording made by code through the real listener, with no microphone

It is off until you turn it on. It does not keep audio, makes no network calls of its own, and is not a
certified smoke alarm. Alerts that cannot be delivered are kept (in memory, at most 20) and delivered as soon
as a route works again. The agent gets one read-only tool, signal_dog_recent_alerts, that answers "what did you
hear while I was out" from the numbers log.
"""

from __future__ import annotations

import collections
import contextvars
import json
import logging
import os
import queue
import shutil
import tempfile
import threading
import time

from . import demo as demo_mod
from . import history
from .listener import Listener, NumbersLog, default_log_path

log = logging.getLogger("plugin.signal-dog")

DEFAULT_HEARTBEAT_HOURS = 24
DEFAULT_RECYCLE_MINUTES = 10     # a live microphone capture gets slowly dearer, so the reader is replaced this often
DELIVERY_TRIES = 3
RETRY_WAITS = (2.0, 5.0, 0.0)      # seconds before the second and third try; a dead route should not hold an alert long
QUEUE_LIMIT = 20
SUMMARY_LISTED = 10
RETRY_SECONDS = 60.0
# the surfaces of a person's own machine; anything else is a messaging chat
LOCAL_SURFACES = ("local", "cli", "tui", "desktop", "acp")
DM_TYPES = ("dm", "private", "direct")
COMMANDS = ("on", "off", "status", "test", "wrong", "right", "demo", "knocks")
USAGE = "Usage: /signaldog on [source] | off | status | test | wrong [note] | right [note] | demo | knocks on|off"
MARK_HOURS = 24                  # /signaldog wrong and right only reach back this far
DEMO_LABEL = "DEMO (not a real sound): "
DEMO_MARK_PAUSE_S = 600.0        # after a demo, /signaldog wrong and right refuse for this long (they would hit an older real alert)
DEMO_GRACE_S = 40.0              # a demo that has not finished this long after its recording ends is stopped
TOOL_NAME = "signal_dog_recent_alerts"
TOOL_HOURS = (24.0, 0.1, 168.0)  # default, least, most
TOOL_LIMIT = (20, 1, 50)
TOOL_DESCRIPTION = (
    "What did the house microphone hear: lists recent knocks and beeps (alarms, fridge door, timers) that Signal Dog, "
    "the household sound listener, raised, from its own numbers log, with time, pitch, whether the owner marked each "
    "right or wrong, and whether it is listening now. Use it when the user asks what the house sounded like or what "
    "was heard while they were out. Read-only. It never reads audio and makes no network call. An empty list is not "
    "proof the house was quiet: the answer says whether the listener was on.")
TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "hours": {"type": "number", "description": "How far back to look, in hours. Default 24, most 168."},
            "limit": {"type": "integer", "description": "The most alerts to list, newest first. Default 20, most 50."},
        },
        "required": [],
    },
}


def caption(ev: dict) -> str:
    kind = ev.get("kind")
    if kind == "knock":
        if ev.get("busy"):
            return ("Knock at the door, maybe. There has been a lot of thumping nearby, so this may be someone moving "
                    "about. Worth a look.")
        return "Knock at the door."
    if kind == "beep":
        khz = ev["hz"] / 1000.0
        if ev.get("pulses"):
            text = "Beeping: a tone around %.1f kHz keeps repeating. An appliance or an alarm." % khz
            if ev.get("busy"):
                text += " The room is noisy right now, so this may be the television or other sound. Worth a look."
            return text
        return "A steady tone around %.1f kHz has been sounding for %d seconds. An appliance or an alarm." % (khz, round(ev["seconds"]))
    if kind == "down":
        return "Signal Dog has stopped listening: %s" % ev.get("text", "unknown reason")
    return "Signal Dog heard something (%s)." % kind


def _hours(value, default):
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return float(default)


def _bounded(value, spec):
    """A number from a tool argument, kept inside (default, least, most); anything unusable gives the default."""
    default, least, most = spec
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:
        return default
    return min(most, max(least, number))


def _ids(value):
    """A list of owner ids as strings; a single value or a comma separated string is accepted too."""
    if value is None or value == "":
        return []
    if isinstance(value, (str, int)):
        value = str(value).split(",")
    try:
        return [str(v).strip() for v in value if str(v).strip()]
    except TypeError:
        return []


class _Plugin:
    def __init__(self, ctx):
        self.ctx = ctx
        self.lock = threading.RLock()
        self.bound = None          # the conversation /signaldog on was run in
        self.latest = None         # a spare route: the latest turn of a local surface, never a messaging chat
        self.pin = None            # (platform, user id as text) of the owner, or ("local", "")
        self.pin_key = ""          # the routing key of the chat /signaldog on was run in
        self.listener = None
        self.outbox = queue.Queue()
        self.worker = None
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.sent = 0
        self.failed = 0
        self.last_ok = ""
        self.last_fail = ""
        self.missed = collections.deque()      # (time text, alert text) waiting for a route that works
        self.lost = 0                          # alerts dropped because the queue was full
        self.alert_times = collections.deque(maxlen=500)
        self.last_alert = ""
        self.heartbeat_at = time.monotonic()
        self.demo_thread = None
        self.demo_stop = threading.Event()
        self.demo_done_at = None               # when the last demo ended (time.time())
        self.knocks_on = True                  # /signaldog knocks off pauses knock messages; beeps and alarms still come
        self.demo_result = None                # what the last demo did: {"alerts": [...], "posted": n, "failed": n}

    # ---- delivery: only the sender thread posts -----------------------------------------------------------
    def _post_in(self, cv, text):
        """Post inside a saved conversation. Success needs `ok` AND not `delivered == False`: a local chat with
        nobody watching answers ok and delivered False."""
        result = []

        def run():
            handle = self.ctx.post_message(text)
            res = getattr(handle, "result", None)
            ok = bool(getattr(handle, "ok", False))
            err = str(getattr(handle, "error", "") or "")
            if ok and isinstance(res, dict) and res.get("delivered") is False:
                ok, err = False, "not_delivered (nobody is watching this chat)"
            result.append((ok, err))

        try:
            cv.copy().run(run)
        except Exception as e:  # post_message never raises, but a bad context could
            return False, "%s: %s" % (type(e).__name__, e)
        return result[0] if result else (False, "no result")

    def _routes(self):
        routes = []
        with self.lock:
            candidates = [self.bound]
            if self.pin is not None and self.pin[0] == "local":
                candidates.append(self.latest)          # a spare is only ever used on a local surface
        for cv in candidates:
            if cv is not None and cv not in routes:
                routes.append(cv)
        return routes

    def _post(self, text):
        routes = self._routes()
        if not routes:
            err = "no_conversation (run /signaldog on in the chat you want alerts in)"
        else:
            err = ""
            for cv in routes:
                ok, err = self._post_in(cv, text)
                if ok:
                    with self.lock:
                        if cv is not self.bound:
                            self.bound = cv       # the spare worked: it is the live conversation now
                        self.last_ok = time.strftime("%H:%M:%S")
                    return True, ""
        with self.lock:
            self.last_fail = "%s %s" % (time.strftime("%H:%M:%S"), err)
        return False, err

    def _queue(self, text):
        with self.lock:
            self.missed.append((time.strftime("%H:%M"), text))
            while len(self.missed) > QUEUE_LIMIT:
                self.missed.popleft()
                self.lost += 1

    def _flush(self):
        """Send everything waiting as one summary. Nothing leaves the queue unless the post worked."""
        with self.lock:
            items = list(self.missed)
            lost = self.lost
        if not items:
            return True
        shown = items[-SUMMARY_LISTED:]
        parts = ["%s %s" % (t, text.rstrip(".")) for t, text in shown]
        head = "Missed while this chat was disconnected"
        notes = []
        if len(items) > len(shown):
            notes.append("%d earlier ones not listed" % (len(items) - len(shown)))
        if lost:
            notes.append("%d older ones lost" % lost)
        message = "%s: %s.%s" % (head, "; ".join(parts), " (%s)" % ", ".join(notes) if notes else "")
        ok, _err = self._post(message)
        if ok:
            with self.lock:
                for _ in range(len(items)):
                    if self.missed:
                        self.missed.popleft()
                self.lost = 0
                self.sent += 1
        return ok

    def _deliver(self, text):
        """Deliver one alert: waiting alerts first (so they arrive in order), then this one with retries."""
        with self.lock:
            waiting = bool(self.missed)
        if waiting and not self._flush():
            self._queue(text)                  # the route is known to be dead: do not make it wait through retries
            return False
        err = ""
        for attempt in range(DELIVERY_TRIES):
            ok, err = self._post(text)
            if ok:
                with self.lock:
                    self.sent += 1
                return True
            if self.stopping.wait(RETRY_WAITS[attempt]):
                break
        with self.lock:
            self.failed += 1
        self._queue(text)
        log.warning("signal-dog: an alert could not be delivered and is waiting (%s)", err)
        return False

    def _work(self):
        next_retry = time.monotonic() + RETRY_SECONDS
        while not self.stopping.is_set():
            try:
                text = self.outbox.get(timeout=2.0)
            except queue.Empty:
                text = None
            if text is not None:
                self._deliver(text)
            with self.lock:
                waiting = bool(self.missed)
            if waiting and (self.wake.is_set() or time.monotonic() >= next_retry):
                self.wake.clear()
                self._flush()
                next_retry = time.monotonic() + RETRY_SECONDS
            self.wake.clear()
            if text is None:
                self._maybe_heartbeat()

    def _maybe_heartbeat(self):
        hours = _hours(self.ctx.get_config("heartbeat_hours", DEFAULT_HEARTBEAT_HOURS), DEFAULT_HEARTBEAT_HOURS)
        ln = self.listener
        if hours <= 0 or ln is None or time.monotonic() - self.heartbeat_at < hours * 3600:
            return
        self.heartbeat_at = time.monotonic()
        st = ln.status()
        cutoff = time.monotonic() - 24 * 3600
        n = sum(1 for t in self.alert_times if t >= cutoff)
        if st["state"] == "listening":
            self.outbox.put("Signal Dog is still listening. Alerts in the last day: %d." % n)
        else:
            self.outbox.put("Signal Dog is NOT listening (%s). %s" % (st["state"], st["error"]))

    def on_event(self, ev):
        if ev.get("kind") == "knock" and not self.knocks_on:
            return                             # paused by the owner; the numbers log still has it
        if ev.get("kind") != "down":
            self.alert_times.append(time.monotonic())
            self.last_alert = time.strftime("%H:%M:%S ") + ev["kind"]
        self.outbox.put(caption(ev))

    # ---- who may give commands ----------------------------------------------------------------------------
    def _authorise(self, cmd, platform, source):
        """(allowed, user id text, refusal text). On a person's own machine anyone at it is the owner. In a
        messaging chat the sender must be known; trust on first use only in a direct message."""
        if platform in LOCAL_SURFACES:
            return True, "", ""
        if source is None:
            return False, "", "Signal Dog cannot tell who sent this command here, so it will not act on it."
        uid = str(getattr(source, "user_id", "") or "")
        if not uid:
            return False, "", "Signal Dog cannot tell who sent this command here, so it will not act on it."
        owners = _ids(self.ctx.get_config("owner_ids", []))
        with self.lock:
            pinned = self.pin
        if owners:
            if uid in owners:
                return True, uid, ""
            return False, uid, "Only the owner can use /signaldog here."
        if cmd == "on":
            # With no owner_ids, switching on is only ever done from a direct message, and only by whoever it is
            # already pinned to (or by the first person, if it is not on yet). Being the pinned owner is not enough
            # to move the alerts into a group chat, where other people could read them.
            is_dm = str(getattr(source, "chat_type", "") or "").lower() in DM_TYPES
            if is_dm and (pinned is None or pinned == (platform, uid)):
                return True, uid, ""
            if pinned is not None and pinned != (platform, uid):
                return False, uid, "Only the owner can use /signaldog here."
            return False, uid, ("Signal Dog will only be switched on from a direct message, or by someone listed in "
                                "its owner_ids setting.")
        if pinned is not None and pinned == (platform, uid):
            return True, uid, ""
        return False, uid, "Only the owner can use /signaldog here."

    # ---- commands -----------------------------------------------------------------------------------------
    def handle(self, raw_args, *, platform="", session_key="", source=None, **kw):
        words = (raw_args or "").split()
        cmd = words[0].lower() if words else "status"
        if cmd not in COMMANDS:
            return USAGE
        allowed, uid, refusal = self._authorise(cmd, platform, source)
        if not allowed:
            return refusal
        if cmd == "on":
            return self._on(words[1] if len(words) > 1 else "", platform, uid, session_key)
        if cmd == "off":
            return self._off()
        with self.lock:
            same_chat = self.pin is not None and (platform in LOCAL_SURFACES or session_key == self.pin_key)
            if same_chat:
                self.bound = contextvars.copy_context()     # the owner's own chat: a fresh, live route
        if same_chat:
            self.wake.set()
        if cmd in ("wrong", "right"):
            return self._mark(cmd, " ".join(words[1:]))
        if cmd == "demo":
            return self._demo()
        if cmd == "knocks":
            return self._knocks(words[1].lower() if len(words) > 1 else "")
        if cmd == "status":
            return self._status()
        ok, err = self._post("Signal Dog test: if you can read this, alerts will reach you here.")
        return "Test message sent." if ok else "Test message failed: %s" % err

    def _on(self, source, platform, uid, session_key):
        self._stop_demo()
        with self.lock:
            self.bound = contextvars.copy_context()
            self.latest = None
            self.pin = ("local", "") if platform in LOCAL_SURFACES else (platform, uid)
            self.pin_key = session_key or ""
        source = source or str(self.ctx.get_config("source", "default") or "default")
        if self.listener is not None:
            self.listener.stop()
        minutes = _hours(self.ctx.get_config("recycle_minutes", DEFAULT_RECYCLE_MINUTES), DEFAULT_RECYCLE_MINUTES)
        self.listener = Listener(source, self.on_event, recycle_s=minutes * 60.0)
        self.listener.start()
        self.heartbeat_at = time.monotonic()
        if self.worker is None or not self.worker.is_alive():
            self.stopping.clear()
            self.worker = threading.Thread(target=self._work, name="signal-dog-sender", daemon=True)
            self.worker.start()
        self.outbox.put("Signal Dog is listening. Alerts will arrive here.")
        return ("Signal Dog is listening (%s). It will post here when it hears a knock, a repeating beep or a long "
                "steady tone. It is not a certified smoke alarm. /signaldog status checks on it, /signaldog off stops it." % source)

    def _knocks(self, arg):
        if arg == "off":
            self.knocks_on = False
            return ("Knock messages paused. Beeps and alarms still come through. They stay paused until /signaldog "
                    "knocks on, or until Hermes restarts.")
        if arg == "on":
            self.knocks_on = True
            return "Knock messages are on."
        return "Knock messages are %s. /signaldog knocks on or /signaldog knocks off changes it." % (
            "on" if self.knocks_on else "paused")

    def _off(self):
        demo_stopped = self._stop_demo()
        if self.listener is None:
            return "The demo was stopped." if demo_stopped else "Signal Dog was not listening."
        self.listener.stop()
        with self.lock:
            self.missed.clear()
            self.lost = 0
            self.bound = self.latest = self.pin = None
            self.pin_key = ""
        return "Signal Dog stopped listening."

    def _status(self):
        ln = self.listener
        t = self.demo_thread
        extra = ["A demo is running. /signaldog off stops it."] if t is not None and t.is_alive() else []
        if not self.knocks_on:
            extra.append("Knock messages are paused (/signaldog knocks on turns them back on).")
        extra += [m for m in (self._marks_line(),) if m]
        if ln is None:
            return "\n".join(["Signal Dog is not listening. /signaldog on starts it."] + extra)
        st = ln.status()
        mins = st["uptime_s"] // 60
        lines = ["Signal Dog: %s (source %s, up %dh %02dm)" % (st["state"], st["source"], mins // 60, mins % 60)]
        if st["error"]:
            lines.append("Problem: " + st["error"])
        age = st["last_frame_age_s"]
        lines.append("Sound analysis: last numbers %s, %d restarts" % ("never" if age is None else "%.1f s ago" % age, st["restarts"]))
        lines.append("Alerts so far: %d%s" % (st["events"], " (last %s)" % self.last_alert if self.last_alert else ""))
        with self.lock:
            waiting, oldest, lost = len(self.missed), (self.missed[0][0] if self.missed else ""), self.lost
            ok_at, fail = self.last_ok, self.last_fail
            sent, failed = self.sent, self.failed
        lines.append("Delivery: %d sent, %d could not be sent at first; last worked %s; last problem %s" % (
            sent, failed, ok_at or "never", fail or "none"))
        if waiting or lost:
            lines.append("Waiting to be delivered: %d%s%s" % (
                waiting, " (oldest %s)" % oldest if oldest else "", "; %d older ones lost because the queue was full" % lost if lost else ""))
        return "\n".join(lines + extra)

    # ---- right and wrong marks ----------------------------------------------------------------------------
    def _marks_line(self):
        snap = history.read(default_log_path())
        if snap.status == "missing":
            return None
        if snap.status == "unreadable":
            return "Your marks: the numbers log cannot be read (%s)" % snap.error
        c = snap.counts()
        unmarked = sum(1 for a in snap.alerts if a["mark"] is None)
        return "Your marks: %d right, %d wrong; %d alerts in the numbers log are not marked" % (c["right"], c["wrong"], unmarked)

    def _mark(self, verdict, note):
        """Write the owner's verdict on the latest alert into the numbers log. Nothing else changes: the rules are not
        tuned by it."""
        if self.demo_done_at is not None and time.time() - self.demo_done_at < DEMO_MARK_PAUSE_S:
            return ("The last alerts you saw came from /signaldog demo, which are not real and are never logged, so there is "
                    "nothing to mark. Marking works on real alerts, and starts working again about %d minutes after a demo." % (DEMO_MARK_PAUSE_S // 60))
        path = default_log_path()
        snap = history.read(path)
        if snap.status == "unreadable":
            return "Signal Dog cannot read its numbers log (%s), so it cannot tell which alert to mark." % snap.error
        latest = snap.alerts[-1] if snap.alerts else None
        if latest is None or latest["ts"] < time.time() - MARK_HOURS * 3600:
            return ("There is no alert to mark: Signal Dog has not raised one in the last %d hours that is still in its "
                    "numbers log." % MARK_HOURS)
        fields = "verdict=%s kind=%s%s alert_at=%s" % (
            verdict, latest["kind"], " hz=%d" % latest["hz"] if latest["hz"] else "", latest["at"])
        note = history.clean_note(note)
        if note:
            fields += " note=" + note
        if not NumbersLog(path).line("mark " + fields):
            return "The mark could not be saved: Signal Dog cannot write to its numbers log."
        what = "%s at %s%s" % (latest["kind"], latest["at"][11:19], " (about %.1f kHz)" % (latest["hz"] / 1000.0) if latest["hz"] else "")
        before = latest["mark"]
        return "Marked the %s as %s%s. It is only noted in the numbers log; it does not change what Signal Dog detects." % (
            what, verdict, " (it was marked %s before)" % before if before and before != verdict else "")

    # ---- the agent's tool ---------------------------------------------------------------------------------
    def recent_alerts(self, args, **kw):
        """The tool handler: always a JSON string, never an exception."""
        try:
            return json.dumps(self._recent(args if isinstance(args, dict) else {}))
        except Exception as e:
            log.exception("signal-dog: the alerts tool failed")
            return json.dumps({"error": "Signal Dog could not read its alerts (%s)." % type(e).__name__})

    def _listener_facts(self):
        ln = self.listener
        if ln is None:
            return {"listening": False, "state": "off",
                    "detail": "Signal Dog is switched off here, or Hermes was restarted since it was switched on (a restart "
                              "does not turn it back on)."}
        st = ln.status()
        age = st["last_frame_age_s"]
        live = st["state"] == "listening" and age is not None and age < 30
        facts = {"listening": live, "state": st["state"], "restarts": st["restarts"],
                 "listening_since": time.time() - st["uptime_s"]}
        if st["error"]:
            facts["problem"] = st["error"]
        if st["state"] == "listening" and not live:
            facts["detail"] = "No sound numbers have arrived for %s s." % ("a long time" if age is None else "%.0f" % age)
        return facts

    def _recent(self, args):
        hours = _bounded(args.get("hours"), TOOL_HOURS)
        limit = int(_bounded(args.get("limit"), TOOL_LIMIT))
        now = time.time()
        since = now - hours * 3600
        facts = self._listener_facts()
        began = facts.pop("listening_since", None)
        if began is not None:
            facts["listening_since"] = history.clock(began)
        notes = []
        out = {"now": history.clock(now), "window_hours": hours, "listener": facts, "notes": notes}
        snap = history.read(default_log_path())
        if snap.status != "ok":
            out["log"] = {"status": snap.status}
            out["alerts"] = []
            if snap.status == "missing":
                notes.append("Signal Dog has no numbers log yet, so nothing has been recorded: it has never run on this "
                             "profile, or its log was deleted. This says nothing about what the house sounded like.")
            else:
                notes.append("Signal Dog cannot read its numbers log (%s), so it cannot say what it heard." % snap.error)
        else:
            inside = [a for a in snap.alerts if a["ts"] >= since]
            shown = inside[-limit:][::-1]
            out["alerts"] = [self._alert_row(a, now) for a in shown]
            out["alerts_in_window"] = len(inside)
            out["marked_in_window"] = {"right": sum(1 for a in inside if a["mark"] == "right"),
                                       "wrong": sum(1 for a in inside if a["mark"] == "wrong")}
            out["log"] = {"status": "ok", "oldest_entry": history.clock(snap.oldest) if snap.oldest else None,
                          "last_entry": history.clock(snap.last_line) if snap.last_line else None,
                          "note": "The log is size limited and rotates: only its newest part is kept."}
            if len(inside) > len(shown):
                notes.append("Only the newest %d of %d alerts in the window are listed." % (len(shown), len(inside)))
            if snap.oldest is not None and snap.oldest > since:
                notes.append("The log starts at %s, so alerts before then are gone, not absent." % history.clock(snap.oldest))
            for at, ts, reason in snap.downs[-3:]:
                if ts >= since:
                    notes.append("The listener gave up at %s: %s" % (at, reason))
        if not facts["listening"]:
            last = " The log was last written at %s." % history.clock(snap.last_line) if snap.status == "ok" and snap.last_line else ""
            notes.append("Signal Dog is NOT listening right now. While it is off it hears nothing, so an empty or short "
                         "list does not mean the house was quiet.%s" % last)
        elif began is not None and began > since:
            notes.append("Live listening began at %s, later than the start of the window. Time before that, and any time "
                         "it was switched off, is not marked in the log, so quiet there proves nothing." % facts["listening_since"])
        notes.append("Signal Dog can miss sounds and can raise false alerts. Marks are the owner's own verdict on past alerts.")
        return out

    @staticmethod
    def _alert_row(a, now):
        row = {"time": a["at"], "ago": history.ago(now - a["ts"]), "kind": a["kind"], "mark": a["mark"] or "not marked"}
        if a["kind"] == "beep":
            row["pitch_hz"] = a["hz"]
            row["pattern"] = "repeating beeps" if a["pulses"] else "steady tone for %s s" % a["seconds"]
            if a["busy"]:
                row["noisy_room"] = True
        return row

    # ---- demo ---------------------------------------------------------------------------------------------
    def _demo(self):
        """Play a recording made by code through the real ffmpeg graph, detector and delivery, every alert labelled."""
        with self.lock:
            if self.demo_thread is not None and self.demo_thread.is_alive():
                return "A demo is already running. /signaldog off stops it."
            ln = self.listener
            if ln is not None and ln.status()["state"] in ("starting", "listening"):
                return ("Signal Dog is listening for real, so it will not run a demo now. Run /signaldog off first, then "
                        "/signaldog demo.")
            if shutil.which("ffmpeg") is None:
                return "The demo needs ffmpeg and it was not found. Install ffmpeg, then run /signaldog demo again. No microphone is needed."
            self.demo_stop.clear()
            self.demo_result = None
            self.demo_thread = threading.Thread(target=self._run_demo, args=(contextvars.copy_context(),),
                                                name="signal-dog-demo", daemon=True)
            self.demo_thread.start()
        return ("Signal Dog demo started. A recording made by code (a knock, then beeps) is played through the real "
                "listener for about %d seconds, with no microphone. Every alert is labelled DEMO. /signaldog off stops it."
                % demo_mod.DEMO_SECONDS)

    def _say_demo(self, cv, text):
        for attempt in range(DELIVERY_TRIES):
            ok, _err = self._post_in(cv, text)
            if ok:
                return True
            if self.stopping.is_set() or self.demo_stop.wait(RETRY_WAITS[attempt]):
                break
        return False

    def _run_demo(self, cv):
        events = queue.Queue()
        alerts, posted, failed = [], 0, 0
        tmp, ln = None, None
        try:
            tmp = tempfile.mkdtemp(prefix="signal-dog-demo-")          # private to this user
            path = os.path.join(tmp, "demo.wav")
            seconds = demo_mod.write_demo(path)
            ln = Listener("file:" + path, events.put, log_path="", recycle_s=0.0)      # "" : the demo writes no log
            ln.start()
            deadline = time.monotonic() + seconds + DEMO_GRACE_S
            while not self.demo_stop.is_set() and time.monotonic() < deadline:
                over = ln.state in ("finished", "error")
                try:
                    ev = events.get(timeout=0.3)
                except queue.Empty:
                    if over:
                        break
                    continue
                if ev.get("kind") == "down":
                    text = "DEMO stopped: %s" % ev.get("text", "unknown reason")
                else:
                    alerts.append(ev["kind"])
                    text = DEMO_LABEL + caption(ev)
                if self._say_demo(cv, text):
                    posted += 1
                else:
                    failed += 1
            if not self.demo_stop.is_set():
                if alerts:
                    summary = ("Signal Dog demo finished: %d demo alerts. None of it was a real sound: the recording was "
                               "made by code and has been deleted." % len(alerts))
                else:
                    summary = "Signal Dog demo finished with no alerts, which is a fault. %s" % (ln.error or "The recording raised nothing.")
                if self._say_demo(cv, summary):
                    posted += 1
                else:
                    failed += 1
        except Exception:
            log.exception("signal-dog: the demo failed")
            failed += 1
        finally:
            if ln is not None:
                ln.stop()
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
            self.demo_result = {"alerts": alerts, "posted": posted, "failed": failed}
            self.demo_done_at = time.time()
            if failed:
                log.warning("signal-dog: %d demo messages could not be delivered", failed)

    def _stop_demo(self):
        """Stop a demo that is running. Returns whether there was one."""
        t = self.demo_thread
        if t is None or not t.is_alive():
            return False
        self.demo_stop.set()
        t.join(10.0)
        return True

    # ---- hooks --------------------------------------------------------------------------------------------
    def refresh(self, **kw):
        """Called before each turn. On a person's own machine, remember this conversation as a spare route and
        wake the sender. Never in a messaging chat (its key is stable and it is not ours to guess)."""
        try:
            surface = self.ctx.session_surface()
        except Exception:
            return None
        with self.lock:
            local = self.pin is not None and self.pin[0] == "local"
            if local and surface in LOCAL_SURFACES:
                self.latest = contextvars.copy_context()
        if local and surface in LOCAL_SURFACES:
            self.wake.set()
        return None

    def shutdown(self):
        self.stopping.set()
        self._stop_demo()
        if self.listener is not None:
            self.listener.stop()


def register(ctx):
    plugin = _Plugin(ctx)
    ctx.register_command("signaldog", plugin.handle, description="Signal Dog: listen for knocks, beeps and alarms",
                         args_hint="on [source] | off | status | test | wrong [note] | right [note] | demo")
    ctx.register_tool(name=TOOL_NAME, toolset="signal_dog", schema=TOOL_SCHEMA, handler=plugin.recent_alerts,
                      description=TOOL_DESCRIPTION)
    ctx.register_hook("pre_llm_call", plugin.refresh)
    ctx.on_unload(plugin.shutdown)
    return plugin
