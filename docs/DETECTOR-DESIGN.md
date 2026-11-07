# Detector design

Decided and built 6 November 2026. Tuned by replaying 66 saved three-second recordings
from two listening tests in a real room with fan noise, then checked end to end on a
synthetic recording through the real plugin.

## No Python packages

`ffmpeg` does all the audio work. Python only ever sees numbers, never sound, and uses the
standard library alone. The official Hermes image has no numpy, and a plugin with no
dependencies installs anywhere Hermes and `ffmpeg` do.

`graph.py` builds one `ffmpeg` command with 64 channels, the most one process can join:

- channels 0 to 8: knock bands at 70, 95, 128, 172, 232, 312, 420, 565 and 760 Hz
- channel 9: a high band, 1.6 to 6.4 kHz, to tell a dull knock from a sharp click
- channels 10 to 63: 54 beep bands 64 Hz apart, 1000 to 4448 Hz

Each band is two band-pass filters in a row, squared, resampled to 100 values a second.
The join is a 64-channel stream of 32-bit floats on a pipe: 25.6 kilobytes a second.

Measured cost of the ffmpeg part: about 5 percent of one core for the full 73-band
prototype, so about 5 percent here; Python's share is small (all rules run on 64 numbers
per frame, the beep rules at 25 steps a second). To be confirmed on a live microphone.

## The rules (`detect.py`)

**Learning the room.** Each knock band is divided by its own usual level. The first half
second is used to learn it, then it adapts over about 30 seconds, ignoring loud frames.
Steady tones such as fan whine become part of the usual level and are ignored.

**Knock.** A thud is a smoothed peak at least 10 dB above the room across the low bands,
with the high band at least 8 dB below the low band. It is *sharp* if the high band at any
loud moment is within 8 dB of the low band, or if a burst of 10 dB or more sits in the high
band within 150 ms before it or 80 ms after; a thud within 600 ms after a sharp one is its
rebound. Two routes, and one alert per knocking session (it ends after 5 seconds without
either route firing):

- *Route A, any room:* three thuds, sharp or not, 180 to 400 ms apart, the two gaps within
  100 ms of each other, within 8 dB of each other in strength, the strongest at least 20 dB
  over the room, and at least two of the three under 14 dB in the high band. No sharpness
  test: a television throws sharp transients over knocks, but it does not keep time like a
  hand on a door. *In a thumping room* (six or more thuds in the 30 s before the set, not
  counting its own last second) a set alerts only if another accepted set ended 0.5 to 20 s
  before it: people at a door knock again, and a person typing, cooking or walking about
  throws up an even three by chance. Such an alert carries `busy: true` and its message says
  the room was busy.
- *Route B, quiet room only:* two dull (not sharp) thuds 150 to 450 ms apart. A room is busy
  when three or more thuds came in the 30 s before (not counting the last 450 ms), or while the
  beep side says the room is busy (see *A busy room* below), and then only Route A applies. A
  thud closer than 150 ms to the last is a bounce and is ignored.

**Beep.** A band is a candidate when it stands out by at least 8 dB from the median of the
bands around it and from its own usual level. A candidate that holds for 160 ms is a pulse.
An alert needs three pulses of **one band**, evenly spaced, within eight seconds: the gaps
between the starts of the three differ by at most 2 steps (80 ms; there is no percentage
allowance), and any two earlier pulses of the band may be the first two (so a stray pulse in
between does not spoil it, and any pattern that repeats has three evenly spaced pulses: the
same place in three cycles). Or one tone that holds for three seconds. A loud tone leaks into
the bands either side, so the strongest band within three of it is reported. Repeating beeps
and a long steady tone are separate warnings with a one-minute quiet time each, so a fridge
that has beeped in threes and then goes continuous still raises the second warning. After a
repeat alert, no other repeat alert at any pitch for ten seconds, so a chord is one message.

*A busy room* is one where 15 or more pulses came in pitches more than three bands away in
the last 30 s; it stays busy for 120 s after the last such moment. In a busy room three
evenly spaced pulses are common by chance, so an alert needs the rhythm to be quick (period
at most 2.4 s) and one of: a run of five pulses, each one period after the last; or a separate
second set (sharing no pulse) at the same pitch and period, within 120 s. In either case at most
one beep in the evidence (in each set of three, or the run of five) may be shorter than 6 steps
(240 ms): a machine's beeps stay over the threshold for a while, a television's chance rhythms
are mostly blips of 4 or 5 steps. An alert that came through the busy-room route carries
`busy: true` and its message adds "The room is noisy right now, so this may be the television or
other sound. Worth a look."

The run was four pulses until the evening of 7 November. Replayed over the labelled television
windows (1.9 hours of saved logs), five took false beep alerts from 10 to 6, and the fridge, the phone
timer and the smoke-alarm stand-in were still found. The settings were chosen on those same
logs, so this is a direction, not a result; and the smoke-alarm stand-in now alerts at a pitch three bands
from the one listed, the edge of the check's tolerance. The knock side reads
the same busy flag: while the room is busy, Route B stays silent (Route A is not affected).

**Debug numbers.** With `debug` on, the numbers-only log gets a few more numbers, one set per event
and never frames, at the end of each line so older logs and tools still read. Per thud: `attack`
(frames from 10 dB under the peak up to the peak), `fall` (frames from the peak to 10 dB under it,
`None` if not reached by the time the thud is decided) and the share of the energy over the room's
usual level in 70 to 172 Hz, 232 to 420 Hz and 565 to 760 Hz at the peak frame. Per beep pulse:
`rise_far` and `rise_low`, how much the mean energy of the tone bands more than three bands away,
and of the nine low bands, rose when the pulse began, against the half second before. With
`debug` off none of this is kept.

Changed 7 November 2026.
Before, any three pulses in bands within two of each other counted, so one sound leaking across
three neighbouring bands counted as three beeps, and then a busy room produced about 100 false
alerts an hour. Costs: a real alert comes later (about 2 seconds in a quiet room, about 2
minutes for a fridge door alarm with the television on), and a soft knock with the television
on is missed.

**Replacing the reader.** Every `recycle_minutes` (default 10) a second `ffmpeg` and detector
start and listen for 30 s, muted, so they have their own room level, busy count and thud
memory. At the swap the new detector takes over only what happened before it started
(`Detector.adopt`): the one-minute quiet times, the ten-second hold, a busy room, earlier sets
and a knocking session, each moved onto its own clock and each taking the later of the two
ends. Nothing newer is carried, so nothing is counted twice. The new one's tuning lines for
its muted 30 s are discarded.

**Bug found and fixed on the way.** After any loud click, the resampler rings and reports
tiny negative energies. The learned usual level of a band was being dragged to zero, which
made that band look 40 dB loud for the next half minute. Energies are now clamped at zero
and the usual level has a floor.

## What the replay showed

| Sound | Result |
|---|---|
| Door knocks, first test (3 recordings) | 3 of 3 found |
| Door knocks, second test (4 sets of knocks) | 3 of 4 found; the 4th, softer set was 7.5 dB over the room and is below the floor |
| Television, speech and music (10 recordings) | 0 of 10 called a knock; no beep alerts |
| Fridge door alarm (8 recordings, 3 short beeps each) | 7 of 8 alerted at 3624 Hz; the 8th was cut off mid-set and the continuous alarm raises the long-tone warning |
| Phone timer (2 recordings) | 2 of 2 found, at 2344 Hz and 1960 and 2408 Hz |
| Desk thumps and a mug put down hard (6 recordings) | 5 of 6 rejected as sharp; 1 still called a knock |
| Other low thuds of unknown source (about 25 recordings) | 6 called a knock; the source (footsteps, a chair, someone else) is unknown, so they are not counted as errors |

Beep rule of 7 November, replayed through the real code (`tools/replay_pulses.py` for the
log, `tools/replay.py` for clips):

| Sound | Today's rule | New rule |
|---|---|---|
| The evening's 34.7 minutes of pulses (washing up, talking, no TV) | 35 alerts, 60 an hour | 7 alerts, 12 an hour |
| Tone clips (fridge 03-10, phone timer 01-02, unlabelled 11-14) | 01-12 alert | 01, 03-08, 12 alert |
| The 52 thud, knock and television clips | 3 beep alerts | the same 3 (they are fridge and timer pitches: real beeps) |

The settings were chosen on the same 34.7 minutes, so this is a direction, not a result. The
television period has no pulse lines and has never been measured with the new rule. Clips 02
(timer, two real repeats in its 3 seconds), 09 (cut off), 10 (continuous alarm) and 11 stopped
alerting and must be confirmed live.

A synthetic recording (room noise, fan hum, one knock, three beeps, one desk thump) run
through the real plugin on the official Hermes image gave exactly two alerts, a knock and
the beeps, and nothing for the thump.

## Public recordings, 7 November (knock rule change)

`tools/public_clips.py` mixes ESC-50 clips into 60 s of steady pink room noise as audio, scales each clip so its
loudest moment is a set number of dB over the room, and runs the real graph and rules. A clip counts once. The
change tested is the knock evenness (80 to 100 ms) and the dull limit (10 to 14 dB in the high band). The kept
numbers below are the change applied; the old rule's 25 dB row is the one before it.

| Loudness over the room | Knocks found (of 40) | Other sounds with a knock alert (of 240) | Alarm clocks with a beep alert (of 40) |
|---|---|---|---|
| 20 dB | 28 | 39 | 21 |
| 25 dB | 39 (was 38) | 50 (was 48) | 26 |
| 30 dB | 35 (was 30) | 68 (was 66) | 32 |

Reading it: the change found one more public knock at 25 dB and five more at 30 dB, and raised two more false alerts at each; on the house logs
(tuned on) it moved the knock sets alerted from 7 of 12 to 10 of 12 and the knock false alerts from 17 to 24 in 1.9
hours. It was kept because it finds more knocks than it adds false alerts on both, but the gain is small and the
house gain is on the data the rule was tuned on. Mouse clicks (19 of 40), typing (10), a creaking door (8) and
clock ticks (7) all raise knock alerts when as loud as a knock; ticks also raise beep alerts (14 of 40). A knock
alert means three loud, even, dull thuds, not that someone is at the door.

## Knock alerts while someone is home, 8 November

Logs from the final build with the developer at home and no visitors, so every knock alert was
false: one evening 82 knock alerts in 133 minutes while up and about (80 of them Route A, a median
of 35 s apart), 2 in an hour with the television on and the developer sitting, 7 in 8 quiet
overnight hours (all Route B, 10 to 17 dB), and 13 in 21 minutes in the morning. The false ones were as loud as the real house knocks (14 to 30 dB against 16 to 33)
and as dull, so no loudness or sharpness limit separates them. Public snoring recordings
(ESC-50) set off Route B: 23 of 40 at 15 dB over the room.

Tried on the saved logs and public clips:

| Change | Knock visits alerted (of 3) | Knock sets alerted (of 12) | False knock alerts on the tuning logs | Public knocks with a Route A set, 25 dB |
|---|---|---|---|---|
| None | 3 | 10 | 24 | 32 |
| The set must stand apart (no other thud in the second before; two variants) | not measured | 7 to 10 of the 11 that had a three | 10 to 19 | 28 to 31 |
| Drop Route B | 3 | 10 | 23 | 32 (but the three real door recordings, which only Route B catches, are lost) |
| In a thumping room, wait for a second set within 20 s (kept) | 3 | 9 | 16 | unchanged (steady room noise is never thumping) |

The kept change never lost a visit; where it lost a set, the visit's next set alerted a few
seconds later. Route B stays, with snoring named as a known cause of night alerts.

## Long runs on a live microphone

Overnight and into the morning (8.7 hours, 523 one-minute readings, the reader swapped every 10 minutes,
49 swaps): the listener and its `ffmpeg` used a median of 8.8 percent of one processor core, under 10
percent in 95 percent of the minutes, 12 percent in the busiest, and never crashed or stopped listening. A one-minute resource line (memory and processor of the
listener and its `ffmpeg`) is in the numbers log; `tools/live_run.py` makes such a run.
Saved television sittings (7, 1.3 hours in all) under the final rules: 5 false beep alerts and 1
false knock alert.

## What the numbers do not show

- These are short clips cut around events, not hours of ordinary life. False alerts per
  day have not been measured yet; a numbers-only logging run is planned.
- One house, one microphone, one set of fans.
- The knock floor of 10 dB over the room misses soft knocks in a noisy room. Lowering it
  made the rules worse on the saved recordings: more clips were thrown out as sharp.
- The smoke-alarm pattern has not been tested against a real alarm's test button.

## Still to measure

- Live microphone input (the replay used files), with Python's processor share, over days.
- False alerts per day, with a television on, and footsteps and a door closing in particular.
- The smoke alarm.
