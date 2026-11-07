"""Drives the test: acts as the owner or the guest on the throwaway Matrix server, and reads what the bot says.
Run inside the Hermes test container.

  matrix_driver.py rooms                         create the direct room and the group room; wait for the bot to join
  matrix_driver.py send <dm|group> <owner|guest> <text...>
  matrix_driver.py read <dm|group> [count]       print the latest messages, oldest first (time, sender, text)
  matrix_driver.py wait <dm|group> <substring> <seconds> [after-count]   wait for a bot message containing the text

Logins are read from the container's test file and never printed."""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

SERVER = "http://signaldog-matrix-synapse:8008"
NAME = "signaldog-test.invalid"
HOME = os.environ.get("HERMES_HOME", "/opt/data")
BOT = "@sdbot:" + NAME
ROOMS_FILE = os.path.join(HOME, "test-rooms.json")


def tokens():
    return json.load(open(os.path.join(HOME, "test-clients.json")))


def call(method, path, body=None, token=None, timeout=30):
    req = urllib.request.Request(SERVER + path, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def tok(who):
    return tokens()["sd" + who]


def rooms_ids():
    return json.load(open(ROOMS_FILE))


def make_rooms():
    owner, guest = tok("owner"), tok("guest")
    code, dm = call("POST", "/_matrix/client/v3/createRoom", {"is_direct": True, "invite": [BOT], "preset": "trusted_private_chat",
                                                               "name": "signaldog-test-direct"}, owner)
    assert code == 200, dm
    code, grp = call("POST", "/_matrix/client/v3/createRoom", {"invite": [BOT, "@sdguest:" + NAME], "preset": "private_chat",
                                                                "name": "signaldog-test-group"}, owner)
    assert code == 200, grp
    code, r = call("POST", "/_matrix/client/v3/join/" + urllib.parse.quote(grp["room_id"]), {}, guest)
    assert code == 200, r
    ids = {"dm": dm["room_id"], "group": grp["room_id"]}
    json.dump(ids, open(ROOMS_FILE, "w"))
    for name, rid in ids.items():
        want = 2 if name == "dm" else 3
        for _ in range(40):
            code, r = call("GET", "/_matrix/client/v3/rooms/%s/joined_members" % urllib.parse.quote(rid), None, owner)
            if code == 200 and len(r.get("joined", {})) >= want:
                break
            time.sleep(1)
        print("%-5s room: %d joined (%s)" % (name, len(r.get("joined", {})), ", ".join(sorted(k.split(":")[0] for k in r.get("joined", {})))))


def messages(room, count=30):
    rid = rooms_ids()[room]
    code, r = call("GET", "/_matrix/client/v3/rooms/%s/messages?dir=b&limit=%d" % (urllib.parse.quote(rid), count), None, tok("owner"))
    out = []
    for ev in reversed(r.get("chunk", [])):
        if ev.get("type") == "m.room.message":
            out.append((ev["origin_server_ts"] / 1000.0, ev["sender"].split(":")[0], (ev.get("content") or {}).get("body", "")))
    return out


def send(room, who, text):
    rid = rooms_ids()[room]
    code, r = call("PUT", "/_matrix/client/v3/rooms/%s/send/m.room.message/%s" % (urllib.parse.quote(rid), uuid.uuid4().hex),
                   {"msgtype": "m.text", "body": text}, tok(who))
    print("sent as %s in %s: %s %s" % (who, room, code, "ok" if code == 200 else r.get("errcode")))


def show(msgs, base=None):
    for ts, sender, body in msgs:
        print("%s  %-8s %s" % (time.strftime("%H:%M:%S", time.gmtime(ts)), sender, body.replace("\n", " | ")[:230]))


def wait(room, needle, seconds, after=0):
    end = time.time() + seconds
    while time.time() < end:
        got = [m for m in messages(room) if m[1] == "@sdbot".lstrip("@") or m[1].lstrip("@") == "sdbot"]
        hits = [m for m in got if needle.lower() in m[2].lower()]
        if len(hits) > after:
            print("FOUND after %.0f s: %s" % (seconds - (end - time.time()), hits[-1][2][:200]))
            return
        time.sleep(1)
    print("NOT FOUND within %d s: %r" % (seconds, needle))


def last_bot_event(room, needle):
    rid = rooms_ids()[room]
    code, r = call("GET", "/_matrix/client/v3/rooms/%s/messages?dir=b&limit=30" % urllib.parse.quote(rid), None, tok("owner"))
    for ev in r.get("chunk", []):
        if ev.get("type") == "m.room.message" and ev["sender"].startswith("@sdbot") and needle.lower() in (ev.get("content") or {}).get("body", "").lower():
            return ev
    return None


def react(room, who, needle, emoji):
    ev = last_bot_event(room, needle)
    if ev is None:
        print("no bot message containing %r" % needle)
        return
    rid = rooms_ids()[room]
    code, r = call("PUT", "/_matrix/client/v3/rooms/%s/send/m.reaction/%s" % (urllib.parse.quote(rid), uuid.uuid4().hex),
                   {"m.relates_to": {"rel_type": "m.annotation", "event_id": ev["event_id"], "key": emoji}}, tok(who))
    print("reacted %s as %s: %s" % (emoji, who, code))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "full":
        ev = last_bot_event(sys.argv[2], sys.argv[3])
        print(json.dumps((ev or {}).get("content", {}), ensure_ascii=False)[:1500])
    elif cmd == "react":
        react(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
    elif cmd == "rooms":
        make_rooms()
    elif cmd == "send":
        send(sys.argv[2], sys.argv[3], " ".join(sys.argv[4:]))
    elif cmd == "read":
        show(messages(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 15))
    elif cmd == "wait":
        wait(sys.argv[2], sys.argv[3], float(sys.argv[4]), int(sys.argv[5]) if len(sys.argv) > 5 else 0)
