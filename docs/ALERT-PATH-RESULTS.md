# Alert path results

How a message gets from a background listener into the chat. Tested on 6 November 2026
with a stub plugin (`/signaldog test ...`) inside a sealed container built from the
official Hermes image, version 0.23.0, with networking switched off.

## Checks on the stub

- `hermes plugins doctor`: passed (manifest, import, registration).
- `hermes plugins validate --install-deps`: passed every check. Security scan: safe.
  No core override. Declared tools, hooks and middleware match what is registered.
  The slash command did not need declaring. One note: `post_message` runs in the
  gateway process only, so the plugin cannot run isolated in a separate host.

## The three routes

| Route | Result | Notes |
|---|---|---|
| `post_message` straight from the command handler | Worked. Gateway log: `plugin_message plugin=signal-dog op=post ... ok=True` | Sanity check only |
| `post_message` from a background thread that carries the conversation (`contextvars.copy_context()` taken inside the command) | Worked. Posted 5 seconds after the command, `ok=True` | This is the route for alerts. It uses no model and needs no permission |
| `inject_message` with the session key | Worked, after granting `allow_gateway_injection` to the plugin | The text arrives as a user message, so the agent runs a model turn to answer it. It needs a permission the user must grant. Slower and costlier |

## Decision

Alerts use `post_message` from a thread that carries the conversation. The conversation
is captured when the user switches the listener on, and refreshed whenever Hermes calls
the plugin for that conversation (any hook or command). If no conversation is bound, the
plugin cannot deliver: it writes a log line and the status command says so, instead of
failing silently.

`inject_message` stays as a possible fallback, off by default.

## Not yet proven

- Idle for two hours: a thread test was started at 09:36 UTC and is due at about 11:36 UTC.
  This one was started before a `/new` replaced the conversation, so it also shows what
  happens to an alert whose conversation has been replaced. Result to be added below.
- After a gateway restart: a restart ends the process, so the binding must be made
  again by the next command or hook call. To be tested on a live gateway.
- Messaging platforms: this test ran on the local chat surface. Matrix has since been
  tested on a real gateway (section "Real messaging app: Matrix" below).

## The real plugin, end to end (6 November 2026)

The finished plugin, enabled in a second sealed container on the same image, with a 24
second synthetic recording standing in for the microphone (a knock at 5 s, three beeps at
12 s, a sharp desk thump at 19 s), played in real time with `/signaldog on file:...`:

- Two alerts were delivered, `ok=True` both times, and nothing for the thump:
  - 10:22:24 knock at the door
  - 10:22:31 beeping around 3.5 kHz
- `/signaldog status` showed 2 sent, 0 failed, last numbers 0.0 s ago.
- The plugin wrote only its numbers log (start, events, finish). No audio file was
  written anywhere.
- The plugin's tests (18) pass on the official image's Python 3.14 and ffmpeg 9.0.
- `hermes plugins doctor` and `validate --install-deps` pass on the real plugin, with the
  security scan "safe" even though the plugin starts `ffmpeg`.

Delivery uses the conversation `/signaldog on` was run in. A hook refreshes a second,
spare route on every turn; if the first route fails, the spare is tried, and if it works
it becomes the main one. Three attempts are made, 5 and 10 seconds apart. If all fail, the
failure is counted and shown by `/signaldog status`.

## Two-hour idle test (6 November 2026)

A thread started at 09:36 UTC and due 11:36 UTC posted with `post_message`. Between the
start and the due time the conversation had been replaced with `/new`.

Result: refused, `no_conversation`, and the stub's status showed `ok=False`.

Two further tests in the new conversation, with no `/new` in between, separated the two
causes. A thread started 20 minutes earlier and one started 60 minutes earlier both
posted successfully (`ok=True`, 11:56:59 and 12:37:02 UTC). **Idle time is not the
problem. Replacing the conversation is**: the old session is no longer live, so a post
carrying its context is refused.

Consequence for the design either way: an alert can be lost if the conversation it was
bound to is replaced before the next turn. Planned: keep alerts that could not be
delivered, deliver them as soon as a new turn rebinds the plugin (the `pre_llm_call` hook
already refreshes the spare route), and show them in `/signaldog status` until then.

## Missed-alert queue on the official image (7 November 2026)

Sealed container, official image, version 0.23.0, local chat, synthetic recordings played in
real time.

- **Recovery by a command.** Knock delivered; `/new` replaced the conversation; a knock at 40 s
  was refused three times (21:16:22, :24, :29) and queued; beeps at 55 s went straight to the
  queue because the route was known dead. `/signaldog status` showed `Waiting to be delivered:
  2` and refreshed the route; the summary was delivered at 21:17:19, `ok=True`. A second status
  showed nothing waiting.
- **Recovery by an ordinary message.** Same sequence, with a dead model address so a turn
  could start. On typing "hello", the hook fired before the model call failed, and at 21:19:34
  the old route was refused and the spare route delivered, `ok=True`.
- 34 tests pass on the image's Python 3.14 and ffmpeg 9.0. `doctor` and `validate
  --install-deps` pass, security scan "safe". A first scan raised a `caution` on README wording
  ("this conversation"), a false match; the wording was changed.
- Four rules were broken on purpose (ignore `delivered`, spare route on messaging, trust a
  group sender, a flush that never empties); each break made a test fail.

Not proven on real Hermes:
- `delivered=False` (a local chat with no viewer) is covered by a unit test only.
- Messaging chats: since tested on Matrix (section below); the line was true when written.
- The summary's text as seen by a person: the classic CLI shows notices at the end of its
  next turn, and the gateway log records `ok`, not the words.

## Real messaging app: Matrix, on a throwaway server (7 November 2026)

Setup: the official Hermes image 0.23.0 in its own container, a throwaway Synapse server in a
second container, both on a Docker network made with `--internal` (no route out), no published
ports, no host mounts, two new volumes. Server name `signaldog-test.invalid`. Three new
accounts (a bot, an owner, a guest), a direct room and a group room, unencrypted. No model was
called; only slash commands, which the gateway answers itself. The bot's login sat in
`/opt/data/.env` inside the test container (mode 600, never displayed), and was deleted with
its volume. Times below are the container's UTC.

- **Alerts in the direct room.** `/signaldog on file:...` from the owner; "Knock at the door."
  and the beeping message arrived in the room, every post `ok=True`.
- **Delivery survives `/new` on a messaging chat.** `/new` needs approval on Matrix (text
  fallback `!approve`). After the approved `/new`, the next recorded knock was still delivered
  to the same room. This was read from the source before and is now observed.
- **Outage.** The Hermes container was cut off from the network at 02:12:04 and reconnected at
  02:12:41. A knock at 02:12:18 failed three times (02:12:18, :20, :25, `send_failed`) and was
  queued. At 02:13:25, 44 seconds after reconnecting, one message arrived: "Missed while this
  chat was disconnected: 02:12 Knock at the door." `/signaldog status` afterwards: 5 sent, 1
  could not be sent at first, nothing waiting. (The recording ended at 70 s, so the planned
  second knock was never played; that is test material, not a missed alert.)
- **Refusals in the group room.** The guest's `/signaldog on`, `status` and `off` were refused
  with "Only the owner can use /signaldog here."
- **A bug the real test found, now fixed.** The owner, once pinned from the direct room, could
  type `/signaldog on` in the group room and the alerts moved to the group (the unit tests had
  only tried a group before any owner was pinned). Fix: without `owner_ids`, `on` is accepted
  only from a direct message. Four unit tests added; the real test was repeated and the group
  `on` was refused with "Signal Dog will only be switched on from a direct message, or by
  someone listed in its owner_ids setting."
- **Stop.** `/signaldog off` in the direct room: "Signal Dog stopped listening."
- **Separation.** No bind mounts, no published ports, the only network was the internal one,
  and the live install paths do not exist inside the container. No live agent, profile,
  gateway, plugins folder or `~/.hermes` was touched.
- **Removal.** Both containers, the network and both volumes were removed by exact name.
  Docker's container, volume and network lists are identical to the snapshot taken before.

Not proven: encrypted rooms (E2EE was off), other messaging apps (Telegram, Discord, Slack),
and how the message looks in a real Matrix client rather than through the server's API.

## Results to add

- Two-hour idle thread test: see above (refused because `/new` replaced the conversation;
  idle time itself is fine).
- Gateway restart: a restart ends the process and empties the queue (stated in the README);
  not separately measured.
- Messaging platform: see the Matrix section above.
