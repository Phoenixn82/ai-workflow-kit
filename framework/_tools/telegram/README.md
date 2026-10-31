# tg: one Telegram group per topic (2026-10-12)
The user: "I don't want theo stuff and ping stuff and whatever else happening in my same chat window."
`tg <topic> "<msg>"` posts to that topic's group; only `heat` buzzes, everything else is silent. Until a topic
has its own group it falls back to the old chat (your DM with the bot), so nothing changed until you do the steps.
Canonical: heimdall `/usr/local/bin/tg` + `~/.config/telegram/topics.json`. odin `~/.local/bin/tg` forwards over ssh.
thor cannot run commands on heimdall (its key is forced to `fleet-wake hold thor`), so thor-heat and heimdall-watch
keep sending directly from thor and read a copy of topics.json that odin's `tg --discover` pushes there.

Telegram is optional. With no bot token (`~/telegram-bot.token` of the owner, or `$TG_HOME/telegram-bot.token`)
`tg <topic> <msg>`, `tg --test` and `tg --show` print one line, `tg: telegram not configured ...`, log it, send
nothing and exit 0; `tg --discover` and `tg --claim` exit 1 (they have nothing to listen with). A configured sender
whose send fails still exits non-zero. Senders call the `tg` CLI, never `ssh heimdall /usr/local/bin/tg`: lane-intel `run.sh` and
`common.py`, weekly-review `run.sh`, handoff-v2 (on odin `tg` is the ssh forwarder, so nothing changed for the user).

## Fresh install: claim the bot first
A new box with only a bot token has no owner yet (no topics.json, no old chat file): `tg --discover` refuses and a
send exits non-zero with "run tg --claim". `tg --claim` prints a one-time code as a link
(`https://t.me/<bot>?start=<code>`) and as a `/start <code>` message; open the link from your own Telegram account
and tap Start (or send the message to the bot in a private chat) within 120 s. That private chat becomes the owner:
`default_chat_id`, the fallback for topics without a group, and the account `--discover` checks group ownership
against. Only that code counts, so a stranger who messages the bot first is never trusted; with an owner already
set (heimdall: the old DM) it refuses. Then do the steps below.

## Do once in Telegram (only you can create groups)
1. Create groups named **Heat, Fleet, Lane Intel, Weekly Review, Misc** (spacing and case don't matter).
   Jobs and Mail are optional: nothing sends to Mail today, and Example App is two-way (see Jobs below).
2. Add the bot to each group.
3. On odin run `tg --discover` (listens 120 s, pauses example_app-inbox meanwhile); while it listens, send the
   `/start@<bot>` line it prints in each group. It maps groups by title (only groups you created; a topic that
   already has a live group is never repointed), prints which topics still fall back,
   and copies the map to thor. Then check: `tg --show`, `tg --test fleet` (`tg --test heat` buzzes).

| topic | senders | host | sound |
|---|---|---|---|
| heat | fleet-heat alerts/tests (odin, heimdall, raven); thor-heat (thor, direct) | heimdall, thor | BUZZ |
| fleet | heimdall-alert shim (fleet-wake, freellmapi-probe, heimdall-backup OnFailure), fleet-heat 6-hourly summary, heimdall-watch (thor), uptime-kuma (after the setting below) | heimdall, thor | silent |
| lane-intel | lane-intel run.sh / common.py (Theo, X, slopalytics) | odin -> heimdall | silent |
| weekly-review | weekly-review run.sh | odin -> heimdall | silent |
| jobs | Example App (stays in the DM unless you move it) | heimdall | silent |
| mail | nothing yet | - | silent |
| misc | any unknown topic, `tg --test misc` | - | silent |
Already separate, untouched: example-tracker (own group), web handoff (own group), another app (own bot, DM only).
disk-probe (thor) is installed but not scheduled; when enabled, put the fleet chat id in `~/.config/disk-probe/chat-id`.

## Uptime Kuma (not changed yet)
Kuma UI on heimdall: Settings -> Notifications -> "Telegram" -> Edit -> **Chat ID** = the fleet id, keep "Send
Silently" on, Save (make it the default notification for every monitor). The id, on heimdall:
`python3 -c 'import json,pathlib;print(json.load(open(pathlib.Path.home()/".config/telegram/topics.json"))["topics"]["fleet"]["chat_id"])'`

## Jobs
Example App sends and reads replies in the chat named by `chat_id` in heimdall `~/.config/example_app/config.json`.
Simplest: leave it in the DM; once the rest has moved out, the DM is the jobs window. For a Jobs group instead:
make the bot an admin there (so it sees plain replies), `tg --discover`, set that `chat_id` to the jobs id
(command above with "jobs"), `systemctl --user restart example_app-inbox`.

## Registry row
| tg: Telegram topic router, one group per topic (added 2026-10-12, the user: "I don't want theo stuff and ping stuff ... in my same chat window") | on demand, no timer; called by fleet-heat, heimdall-alert, lane-intel, weekly-review | zero-token Python, no LLM | heimdall /usr/local/bin/tg + ~/.config/telegram/topics.json; odin ~/.local/bin/tg (ssh); thor-heat + heimdall-watch read a copy on thor; only heat buzzes | nothing to stop; rollback below, or `rm ~/.config/telegram/topics.json` on heimdall = everything back to the old chat |

## Rollback
install.sh stages its files in a private `mktemp -d` dir on heimdall and, before replacing `tg`, `heimdall-alert`,
`fleet-heat` (heimdall) or `~/.local/bin/tg` (odin), keeps any existing copy that differs as
`<file>.bak-<UTC timestamp>` next to it (`.1`, `.2` ... if taken; an earlier backup is never overwritten).
Undo the last deploy: `sudo cp -p <file>.bak-<timestamp> <file>` (odin: no sudo). Back to before topics (originals
from the first deploy, *.bak-20261012):
heimdall: `cd /usr/local/bin && sudo cp -p heimdall-alert.bak-20261012 heimdall-alert && sudo cp -p fleet-heat.bak-20261012 fleet-heat && sudo rm tg`
thor: `cd /usr/local/bin && sudo cp -p thor-heat.bak-20261012 thor-heat && sudo cp -p heimdall-watch.bak-20261012 heimdall-watch`
odin: `cd ~/AI_Projects/_tools && for f in lane-intel/run.sh lane-intel/common.py weekly-review/run.sh host-heat/fleet-heat; do cp -p $f.bak-20261012 $f; done; rm ~/.local/bin/tg`
Redeploy: `install.sh --dry-run` (says which files differ and would be backed up; changes no installed file), then
`install.sh` (idempotent). Self-test: `tg --self-test`.
