"""Reads Signal Dog's numbers log back: the alerts it raised, the owner's right/wrong marks and when the listener
last wrote. Read only. It never touches audio and makes no network call. The log rotates (see listener.LOG_LIMIT):
only the newest part is here, and `oldest` says where it starts.
"""

from __future__ import annotations

import datetime
import os
import time

READ_LIMIT = 1024 * 1024        # bytes read from the end of each log file; the log itself stays far under this
NOTE_LIMIT = 80                 # characters of the owner's note kept in a mark line


def parse_stamp(text: str):
    """Seconds since the epoch for a log time such as 2026-11-07T17:02:11+1100, or None."""
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


def clean_note(text: str) -> str:
    """The owner's note as it is kept in the log: printable characters only, one line, no '=' (so it can never look
    like a log field), cut to NOTE_LIMIT."""
    text = "".join(ch if ch.isprintable() else " " for ch in (text or "")).replace("=", " ")
    return " ".join(text.split())[:NOTE_LIMIT].strip()


def _fields(rest: str) -> dict:
    return dict(tok.split("=", 1) for tok in rest.split() if "=" in tok)


def _read_tail(path: str) -> list:
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if size > READ_LIMIT:
            fh.seek(size - READ_LIMIT)
        data = fh.read()
    return data.decode("utf-8", "replace").splitlines()      # a line cut by the seek has no time in front and is skipped


class Snapshot:
    """What the log holds. `status` is "ok", "missing" (no log yet) or "unreadable" (`error` says why)."""

    def __init__(self):
        self.status = "ok"
        self.error = ""
        self.alerts = []          # oldest first: {"at", "ts", "kind", "hz", "pulses", "seconds", "busy", "mark"}
        self.marks = {}           # (alert time text, kind) -> "right" or "wrong"; the latest mark wins
        self.downs = []           # (time text, ts, reason): times the listener gave up
        self.oldest = None        # epoch seconds of the first line kept
        self.last_line = None     # epoch seconds of the last line written (the listener writes one a minute)

    def counts(self) -> dict:
        right = sum(1 for v in self.marks.values() if v == "right")
        return {"right": right, "wrong": len(self.marks) - right}


def read(path: str) -> Snapshot:
    snap = Snapshot()
    lines, found = [], False
    for p in (path + ".1", path):
        try:
            lines += _read_tail(p)
            found = True
        except FileNotFoundError:
            continue
        except OSError as e:
            snap.status, snap.error = "unreadable", e.strerror or type(e).__name__
            return snap
    if not found:
        snap.status = "missing"
        return snap
    for line in lines:
        parts = line.split(" ", 2)
        if len(parts) < 2:
            continue
        ts = parse_stamp(parts[0])
        if ts is None:
            continue
        if snap.oldest is None:
            snap.oldest = ts
        word, rest = parts[1], (parts[2] if len(parts) > 2 else "")
        if word != "mark":                 # a mark is the owner's, not a sign the listener was running
            snap.last_line = ts
        if word == "event":
            f = _fields(rest)
            if f.get("kind") in ("knock", "beep"):
                snap.alerts.append({"at": parts[0], "ts": ts, "kind": f["kind"], "hz": _int(f.get("hz")),
                                    "pulses": _int(f.get("pulses")), "seconds": f.get("seconds"),
                                    "busy": f.get("busy") == "True", "mark": None})
        elif word == "mark":
            f = _fields(rest.split(" note=", 1)[0])
            if f.get("verdict") in ("right", "wrong") and f.get("alert_at") and f.get("kind"):
                snap.marks[(f["alert_at"], f["kind"])] = f["verdict"]
        elif word == "down":
            snap.downs.append((parts[0], ts, rest.replace("reason=", "", 1)[:200]))
    for a in snap.alerts:
        a["mark"] = snap.marks.get((a["at"], a["kind"]))
    return snap


def _int(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "%d s ago" % seconds
    if seconds < 5400:
        return "%d min ago" % round(seconds / 60)
    if seconds < 172800:
        return "%.1f h ago" % (seconds / 3600.0)
    return "%d days ago" % (seconds // 86400)


def clock(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
