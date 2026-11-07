"""Delivery, queue, owner and spare-route rules. A fake chat stands in for Hermes; no ffmpeg is needed."""
import contextvars
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

import _pkg

_pkg.load()
import signal_dog  # noqa: E402

ROUTE = contextvars.ContextVar("route", default="none")
_SAVED = {}


def setUpModule():
    """/signaldog status reads the numbers log: point it at an empty folder, never at a real profile."""
    _SAVED["home"] = os.environ.get("HERMES_HOME")
    _SAVED["dir"] = tempfile.mkdtemp()
    os.environ["HERMES_HOME"] = _SAVED["dir"]


def tearDownModule():
    shutil.rmtree(_SAVED["dir"], ignore_errors=True)
    if _SAVED["home"] is None:
        os.environ.pop("HERMES_HOME", None)
    else:
        os.environ["HERMES_HOME"] = _SAVED["home"]


def route(name):
    cv = contextvars.Context()
    cv.run(ROUTE.set, name)
    return cv


class Handle:
    def __init__(self, ok, error=None, result=None):
        self.ok, self.error = ok, error
        self.result = result if result is not None else {"ok": ok}


class Ctx:
    """post_message succeeds only in routes named 'live'; 'quiet' answers ok but delivered False."""

    def __init__(self):
        self.posted = []
        self.cfg = {}
        self.surface = "cli"
        self.commands, self.hooks, self.unload, self.tools = {}, {}, [], {}

    def register_command(self, name, fn, **kw):
        self.commands[name] = fn

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
        where = ROUTE.get()
        if where == "live":
            self.posted.append(text)
            return Handle(True)
        if where == "quiet":
            return Handle(True, result={"ok": True, "delivered": False})
        return Handle(False, "no_conversation")


class StubListener:
    def __init__(self, source, on_event, **kw):
        self.source = source

    def start(self):
        pass

    def stop(self):
        pass

    def status(self):
        return {"state": "listening", "error": "", "source": self.source, "uptime_s": 125, "frames": 1,
                "last_frame_age_s": 0.1, "restarts": 0, "events": 0, "recent": []}


def make(pin=("local", ""), bound="live", latest=None):
    ctx = Ctx()
    p = signal_dog.register(ctx)
    p.stopping.set()                       # no waiting between retries in tests
    p.pin, p.bound, p.latest = pin, route(bound) if bound else None, route(latest) if latest else None
    return ctx, p


def src(user, chat="dm"):
    return types.SimpleNamespace(user_id=user, chat_id="c1", chat_type=chat)


class DeliveryTests(unittest.TestCase):
    def test_ok_but_not_delivered_is_a_failure_and_is_queued(self):
        ctx, p = make(bound="quiet")
        self.assertFalse(p._deliver("Knock at the door."))
        self.assertEqual(len(p.missed), 1)
        self.assertEqual(p.failed, 1)
        self.assertIn("nobody is watching", p.last_fail)

    def test_flush_sends_one_summary_in_order_and_empties_the_queue(self):
        ctx, p = make(bound="live")
        for t, text in (("03:12", "Knock at the door."), ("03:40", "Beeping: a tone."), ("04:01", "Knock at the door.")):
            p.missed.append((t, text))
        self.assertTrue(p._flush())
        self.assertEqual(len(ctx.posted), 1)
        msg = ctx.posted[0]
        self.assertLess(msg.index("03:12"), msg.index("03:40"))
        self.assertLess(msg.index("03:40"), msg.index("04:01"))
        self.assertTrue(msg.startswith("Missed while this chat was disconnected"))
        self.assertEqual(len(p.missed), 0)

    def test_failed_flush_keeps_everything_in_order(self):
        ctx, p = make(bound="dead")
        p.missed.extend([("03:12", "first"), ("03:13", "second")])
        self.assertFalse(p._flush())
        self.assertEqual([t for _, t in p.missed], ["first", "second"])

    def test_new_alert_queues_behind_waiting_ones_when_the_route_is_dead(self):
        ctx, p = make(bound="dead")
        p.missed.append(("03:12", "old"))
        self.assertFalse(p._deliver("new"))
        self.assertEqual([t for _, t in p.missed], ["old", "new"])

    def test_overflow_keeps_newest_twenty_and_counts_the_lost(self):
        ctx, p = make(bound="dead")
        for i in range(25):
            p._queue("alert %d" % i)
        self.assertEqual(len(p.missed), 20)
        self.assertEqual(p.lost, 5)
        self.assertEqual(p.missed[0][1], "alert 5")
        p.bound = route("live")
        self.assertTrue(p._flush())
        self.assertIn("5 older ones lost", ctx.posted[0])
        self.assertIn("earlier ones not listed", ctx.posted[0])
        self.assertEqual(p.lost, 0)

    def test_spare_route_rescues_a_dead_bound_route_on_a_local_surface(self):
        ctx, p = make(pin=("local", ""), bound="dead", latest="live")
        self.assertTrue(p._deliver("Knock at the door."))
        self.assertEqual(ctx.posted, ["Knock at the door."])

    def test_spare_route_is_never_used_on_a_messaging_platform(self):
        ctx, p = make(pin=("telegram", "42"), bound="dead", latest="live")
        self.assertFalse(p._deliver("Knock at the door."))
        self.assertEqual(ctx.posted, [])
        self.assertEqual(len(p.missed), 1)

    def test_hook_adopts_spare_only_for_a_local_owner_on_a_local_surface(self):
        ctx, p = make(pin=("local", ""), bound="live")
        ctx.surface = "telegram"
        p.refresh()
        self.assertIsNone(p.latest)
        ctx.surface = "tui"
        p.refresh()
        self.assertIsNotNone(p.latest)
        ctx2, p2 = make(pin=("telegram", "42"), bound="live")
        ctx2.surface = "cli"
        p2.refresh()
        self.assertIsNone(p2.latest)

    def test_status_is_honest_about_waiting_alerts(self):
        ctx, p = make(bound="dead")
        p.listener = StubListener("default", p.on_event)
        p._queue("Knock at the door.")
        text = p._status()
        self.assertIn("Waiting to be delivered: 1", text)
        self.assertNotIn("bound", text)


class OwnerTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(signal_dog, "Listener", StubListener)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cmd(self, p, cmd, platform, source=None, key="k1"):
        return contextvars.copy_context().run(p.handle, cmd, platform=platform, session_key=key, source=source)

    def fresh(self):
        ctx = Ctx()
        p = signal_dog.register(ctx)
        self.addCleanup(p.shutdown)
        return ctx, p

    def test_own_machine_needs_no_owner(self):
        ctx, p = self.fresh()
        self.assertIn("listening", self.run_cmd(p, "on", "cli"))
        self.assertEqual(p.pin, ("local", ""))

    def test_messaging_without_a_known_sender_is_refused(self):
        ctx, p = self.fresh()
        self.assertIn("cannot tell who sent", self.run_cmd(p, "on", "telegram", None))
        self.assertIsNone(p.listener)

    def test_first_direct_message_owner_is_trusted_and_pinned(self):
        ctx, p = self.fresh()
        self.assertIn("listening", self.run_cmd(p, "on", "telegram", src(123, "dm")))
        self.assertEqual(p.pin, ("telegram", "123"))
        self.assertIn("Only the owner", self.run_cmd(p, "status", "telegram", src(999, "dm")))
        self.assertIn("Signal Dog:", self.run_cmd(p, "status", "telegram", src(123, "dm")))

    def test_the_pinned_owner_cannot_move_the_alerts_into_a_group_chat(self):
        # found by the real Matrix test: being the pinned owner used to be enough to switch on from a group
        ctx, p = self.fresh()
        self.run_cmd(p, "on", "telegram", src(123, "dm"), key="dm-key")
        before_pin, before_bound = p.pin, p.bound
        self.assertIn("direct message", self.run_cmd(p, "on", "telegram", src(123, "group"), key="group-key"))
        self.assertEqual(p.pin, before_pin)
        self.assertIs(p.bound, before_bound)
        self.assertEqual(p.pin_key, "dm-key")

    def test_another_user_cannot_switch_on_while_it_is_pinned_to_someone_else(self):
        ctx, p = self.fresh()
        self.run_cmd(p, "on", "telegram", src(123, "dm"), key="dm-key")
        self.assertIn("Only the owner", self.run_cmd(p, "on", "telegram", src(999, "dm"), key="other-key"))
        self.assertEqual(p.pin, ("telegram", "123"))

    def test_the_pinned_owner_can_switch_on_again_from_their_direct_message(self):
        ctx, p = self.fresh()
        self.run_cmd(p, "on", "telegram", src(123, "dm"), key="dm-key")
        self.assertIn("listening", self.run_cmd(p, "on", "telegram", src(123, "dm"), key="dm-key"))

    def test_a_listed_owner_may_switch_on_in_a_group_on_purpose(self):
        ctx, p = self.fresh()
        ctx.cfg["owner_ids"] = ["123"]
        self.assertIn("listening", self.run_cmd(p, "on", "telegram", src(123, "group"), key="group-key"))

    def test_group_chat_is_refused_without_owner_ids(self):
        ctx, p = self.fresh()
        self.assertIn("direct message", self.run_cmd(p, "on", "telegram", src(123, "group")))
        self.assertIsNone(p.listener)

    def test_owner_ids_compare_as_text_so_integers_in_config_work(self):
        ctx, p = self.fresh()
        ctx.cfg["owner_ids"] = [123, 456]
        self.assertIn("listening", self.run_cmd(p, "on", "telegram", src("123", "group")))
        self.assertIn("Only the owner", self.run_cmd(p, "on", "telegram", src("789", "group")))

    def test_off_clears_the_queue_and_the_pin(self):
        ctx, p = self.fresh()
        self.run_cmd(p, "on", "cli")
        p._queue("Knock at the door.")
        self.assertIn("stopped", self.run_cmd(p, "off", "cli"))
        self.assertEqual(len(p.missed), 0)
        self.assertIsNone(p.pin)
        self.assertIsNone(p.bound)

    def test_status_from_another_chat_does_not_rebind_alerts_there(self):
        ctx, p = self.fresh()
        self.run_cmd(p, "on", "telegram", src(123, "dm"), key="dm-key")
        before = p.bound
        self.run_cmd(p, "status", "telegram", src(123, "dm"), key="group-key")
        self.assertIs(p.bound, before)


if __name__ == "__main__":
    unittest.main()
