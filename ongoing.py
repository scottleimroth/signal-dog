"""Group detected beeps using analysed-audio time, never wall-clock silence or detector cooldowns."""
from __future__ import annotations

import uuid

from .graph import TONE_BANDS


class Alarms:
    def __init__(self, reminder_s=180.0, clear_s=15.0, spread=3):
        self.reminder_s = reminder_s
        self.clear_s = clear_s
        self.spread = spread
        self.active = []
        self.serial = 0
        self.prefix = uuid.uuid4().hex[:12]

    def feed(self, seconds, events, sounding):
        """Return starts immediately; update only while fresh matching spectral evidence exists.

        `sounding` contains bands with a narrow tone held for at least the detector's minimum pulse length.
        It deliberately ignores the adaptive room baseline: a long alarm must not become 'stopped' just
        because that baseline has learned it. Different pitches and repeat-to-steady changes start anew.
        """
        out = []
        for a in self.active:
            if any(abs(k - a['band']) <= self.spread for k in sounding):
                a['last'] = seconds
        for ev in events:
            if ev['kind'] != 'beep':
                out.append(ev)
                continue
            band = min(range(len(TONE_BANDS)), key=lambda k: abs(TONE_BANDS[k] - ev['hz']))
            steady = not bool(ev.get('pulses'))
            found = next((a for a in self.active if abs(a['band'] - band) <= self.spread
                          and a['steady'] == steady and seconds - a['last'] < self.clear_s), None)
            if found is not None:
                found['last'] = seconds
                continue
            self.serial += 1
            a = dict(band=band, steady=steady, hz=ev['hz'], start=seconds, last=seconds,
                     reminded=seconds, incident='%s-%d' % (self.prefix, self.serial))
            self.active.append(a)
            out.append(dict(ev, incident=a['incident']))
        keep = []
        for a in self.active:
            absent = seconds - a['last']
            base = dict(incident=a['incident'], hz=a['hz'], elapsed_s=round(seconds - a['start'], 1))
            if absent >= self.clear_s:
                out.append(dict(base, kind='beep_stopped', absent_s=round(absent, 1)))
            else:
                keep.append(a)
                # No reminder during a quiet tail: absence is not evidence of 'still beeping'.
                if seconds - a['reminded'] >= self.reminder_s and absent < 1.0:
                    a['reminded'] = seconds
                    out.append(dict(base, kind='beep_ongoing'))
        self.active = keep
        return out

    def interrupted(self):
        """Lost input is unknown, not a cleared alarm. A new detection must alert immediately again."""
        out = [dict(kind='beep_unknown', incident=a['incident'], hz=a['hz']) for a in self.active]
        self.active = []
        return out
