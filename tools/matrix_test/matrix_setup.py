"""Creates the three test accounts on the throwaway Matrix server and writes the bot's login where Hermes reads it.
Run inside the Hermes test container, which can reach the server by name on the private network:

  python matrix_setup.py accounts     # register the accounts, write the secrets file and the test driver's logins

Passwords are random and never stored or shown. Access tokens go straight into two files in the container (mode
600) and are never printed. Only the account names and 'ok' lines are printed."""
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

SERVER = "http://signaldog-matrix-synapse:8008"
NAME = "signaldog-test.invalid"
USERS = ("sdbot", "sdowner", "sdguest")
HOME = os.environ.get("HERMES_HOME", "/opt/data")


def call(method, path, body=None, token=None):
    req = urllib.request.Request(SERVER + path, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


def accounts():
    for _ in range(30):
        try:
            if call("GET", "/_matrix/client/versions")[0] == 200:
                break
        except OSError:
            pass
        time.sleep(2)
    tokens = {}
    for u in USERS:
        pw = secrets.token_urlsafe(24)
        code, r = call("POST", "/_matrix/client/v3/register", {"username": u, "password": pw, "auth": {"type": "m.login.dummy"},
                                                                 "initial_device_display_name": "signaldog-test"})
        if code != 200:
            print("register %s failed: %s %s" % (u, code, r.get("errcode")))
            sys.exit(1)
        tokens[u] = r["access_token"]
        print("registered @%s:%s" % (u, NAME))
    bot = "@sdbot:" + NAME
    allowed = ",".join("@%s:%s" % (u, NAME) for u in ("sdowner", "sdguest"))
    env = "\n".join([
        "MATRIX_HOMESERVER=" + SERVER,
        "MATRIX_USER_ID=" + bot,
        "MATRIX_ACCESS_TOKEN=" + tokens["sdbot"],
        "MATRIX_ALLOWED_USERS=" + allowed,
        "MATRIX_REQUIRE_MENTION=false",
        "MATRIX_AUTO_THREAD=false",
        "MATRIX_DM_AUTO_THREAD=false",
        "MATRIX_SESSION_SCOPE=room",
        "MATRIX_REACTIONS=false",
        "MATRIX_E2EE_MODE=off",
        ""])
    write_private(os.path.join(HOME, ".env"), env)
    write_private(os.path.join(HOME, "test-clients.json"), json.dumps({u: tokens[u] for u in ("sdowner", "sdguest")}))
    print("wrote the bot's login to the secrets file and the test clients' logins (not displayed)")


if __name__ == "__main__":
    {"accounts": accounts}[sys.argv[1]]()
