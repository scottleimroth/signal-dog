"""Observable alarm lifecycle and delivery, including cooldown, interruption and non-editing routes."""
import unittest
from unittest import mock

import _pkg
_pkg.load()
import signal_dog
from signal_dog import detect, graph, listener
from signal_dog.ongoing import Alarms
from test_signal_dog import frame
from test_delivery import Ctx, route


def beep(hz=None, pulses=3):
    return dict(kind='beep', hz=hz or graph.TONE_BANDS[30], pulses=pulses, seconds=3)


class AlarmTests(unittest.TestCase):
    def test_first_alert_is_immediate_then_one_update_per_interval_and_one_ending(self):
        a = Alarms(reminder_s=60, clear_s=15)
        start = a.feed(0, [beep()], {30})
        self.assertEqual([e['kind'] for e in start], ['beep'])
        out = []
        for t in range(1, 131):
            out += a.feed(t, [beep()] if t % 10 == 0 else [], {30})
        self.assertEqual([e['kind'] for e in out], ['beep_ongoing', 'beep_ongoing'])
        self.assertTrue(all(e['incident'] == start[0]['incident'] for e in out))
        self.assertEqual(a.feed(144, [], set()), [])
        end = a.feed(145, [], set())
        self.assertEqual([e['kind'] for e in end], ['beep_stopped'])
        self.assertEqual(a.feed(200, [], set()), [])
        again = a.feed(201, [beep()], {30})
        self.assertNotEqual(start[0]['incident'], again[0]['incident'])

    def test_pause_in_alarm_does_not_clear_and_quiet_tail_does_not_send_reminder(self):
        a = Alarms(reminder_s=60, clear_s=15)
        a.feed(0, [beep()], {30})
        for t in range(1, 61):
            self.assertEqual(a.feed(t, [], {30} if t % 5 == 0 and t < 55 else set()), [])
        out = a.feed(61, [], {30})
        self.assertEqual(out[0]['kind'], 'beep_ongoing')

    def test_other_pitch_and_steady_escalation_are_new_alerts_knocks_unchanged(self):
        a = Alarms()
        a.feed(0, [beep()], {30})
        out = a.feed(1, [beep(pulses=0), beep(graph.TONE_BANDS[5]), {'kind': 'knock'}], {30, 5})
        self.assertEqual([e['kind'] for e in out], ['beep', 'beep', 'knock'])

    def test_input_failure_is_unknown_not_stopped_and_next_detection_alerts(self):
        a = Alarms()
        a.feed(0, [beep()], {30})
        self.assertEqual(a.interrupted()[0]['kind'], 'beep_unknown')
        self.assertEqual(a.interrupted(), [])
        self.assertEqual(a.feed(1, [beep()], {30})[0]['kind'], 'beep')

    def test_listener_failure_reports_unknown_once_then_down_not_a_clear(self):
        out = []
        ln = listener.Listener('file:synthetic.wav', out.append, log_path='')
        ln.alarms.feed(0, [beep()], {30})
        ln._fail('test input failure')
        self.assertEqual([ev['kind'] for ev in out], ['beep_unknown', 'down'])

    def test_eof_and_user_stop_do_not_invent_a_quiet_tail(self):
        out = []
        ln = listener.Listener('file:synthetic.wav', out.append, log_path='')
        ln.alarms.feed(0, [beep()], {30})
        ln.stop()
        self.assertEqual(out, [])

    def test_spectral_evidence_survives_room_learning_and_rearms_after_quiet(self):
        d = detect.Detector(detect.Config(own_alpha=0.005))
        a = Alarms(reminder_s=60)
        out = []
        # A sustained tone eventually becomes part of the adaptive room baseline; it has not stopped.
        for i in range(8500):
            f = frame(tone={30: 100.0}) if 100 <= i < 6800 or i >= 8400 else frame()
            for ev in a.feed(i / 100, d.feed(f), d.beep.sounding):
                out.append(ev)
                if ev['kind'] == 'beep_stopped':
                    d.rearm_beep(ev['hz'])
        self.assertEqual([e['kind'] for e in out], ['beep', 'beep_ongoing', 'beep_stopped'])
        self.assertGreater(out[-1]['elapsed_s'], 75)

    def test_reader_handover_preserves_rearm_after_an_alarm_has_stopped(self):
        d, shadow = detect.Detector(), detect.Detector()
        a, out = Alarms(), []
        for i in range(3200):
            on = (100 <= i < 300 or 2300 <= i < 2500) and i % 32 < 20
            f = frame(tone={30: 100.0}) if on else frame()
            if i < 2100:
                shadow.feed(f)   # warmed reader detects too, but its events are muted
            if i == 2100:
                shadow.adopt(d)
                d = shadow
            for ev in a.feed(i / 100, d.feed(f), d.beep.sounding):
                out.append(ev)
                if ev['kind'] == 'beep_stopped':
                    d.rearm_beep(ev['hz'])
        self.assertEqual([e['kind'] for e in out], ['beep', 'beep_stopped', 'beep'])

    def test_same_pitch_new_alarm_within_old_minute_cooldown_alerts(self):
        d = detect.Detector()
        a = Alarms()
        out = []
        for i in range(3200):
            on = (100 <= i < 300 or 2300 <= i < 2500) and i % 32 < 20
            events = d.feed(frame(tone={30: 100.0}) if on else frame())
            for ev in a.feed(i / 100, events, d.beep.sounding):
                out.append(ev)
                if ev['kind'] == 'beep_stopped':
                    d.rearm_beep(ev['hz'])
        self.assertEqual([e['kind'] for e in out], ['beep', 'beep_stopped', 'beep'])


class EditableCtx(Ctx):
    def __init__(self):
        super().__init__()
        self.edited = []
        self.edit_ok = True
        self.delivered = True

    def post_message(self, text):
        handle = super().post_message(text)
        def edit(new):
            if self.edit_ok:
                self.edited.append(new)
            return {'ok': self.edit_ok, 'delivered': self.delivered}
        handle.edit = edit
        return handle


class DeliveryTests(unittest.TestCase):
    def plugin(self, editable=True):
        ctx = EditableCtx() if editable else Ctx()
        p = signal_dog.register(ctx)
        p.bound = route('live')
        p.pin = ('local', '')
        self.addCleanup(p.shutdown)
        return p, ctx

    def deliver_event(self, p, ev):
        p.on_event(ev)
        return p._deliver(p.outbox.get_nowait())

    def test_start_then_updates_edit_the_same_message_and_release_handle(self):
        p, ctx = self.plugin()
        self.assertTrue(self.deliver_event(p, dict(beep(), incident='alarm')))
        self.assertTrue(self.deliver_event(p, dict(kind='beep_ongoing', hz=3000, elapsed_s=180, incident='alarm')))
        self.assertTrue(self.deliver_event(p, dict(kind='beep_stopped', hz=3000, elapsed_s=200, absent_s=15, incident='alarm')))
        self.assertEqual(len(ctx.posted), 1)
        self.assertEqual(len(ctx.edited), 2)
        self.assertIn('still beeping', ctx.edited[0])
        self.assertIn('no longer detected', ctx.edited[1])
        self.assertIn('not mean it is safe', ctx.edited[1])
        self.assertEqual(p.alarm_handles, {})

    def test_non_editing_handle_falls_back_to_messages(self):
        p, ctx = self.plugin(editable=False)
        self.deliver_event(p, dict(beep(), incident='alarm'))
        self.deliver_event(p, dict(kind='beep_ongoing', hz=3000, elapsed_s=180, incident='alarm'))
        self.assertEqual(len(ctx.posted), 2)

    def test_failed_edit_or_no_viewer_uses_normal_delivery_and_queues_when_disconnected(self):
        for no_viewer in (False, True):
            p, ctx = self.plugin()
            self.deliver_event(p, dict(beep(), incident='alarm'))
            ctx.edit_ok = no_viewer
            ctx.delivered = not no_viewer
            p.bound = route('dead')
            with mock.patch.object(signal_dog, 'RETRY_WAITS', (0, 0, 0)):
                self.assertFalse(self.deliver_event(p, dict(kind='beep_stopped', hz=3000, elapsed_s=200,
                                                          absent_s=15, incident='alarm')))
            self.assertEqual(len(p.missed), 1)
            self.assertEqual(p.alarm_handles, {})
            p.bound = route('live')
            self.assertTrue(p._flush())
            self.assertIn('no longer detected', ctx.posted[-1])

    def test_reset_discards_old_handles_without_reporting_stopped(self):
        p, ctx = self.plugin()
        self.deliver_event(p, dict(beep(), incident='alarm'))
        p._deliver({'reset': True})
        self.assertEqual(p.alarm_handles, {})
        self.assertEqual(ctx.edited, [])
