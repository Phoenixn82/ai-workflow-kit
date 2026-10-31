---
name: telegram
description: >
  Telegram for the user: send a notification to the right chat (`tg <topic>`), and create Telegram groups or bots
  yourself as their account (`tg-admin`, odin). Triggers: telegram, notify me, ping me, telegram bot, BotFather,
  new bot, bot token, telegram group, make a group, chat id, topic chat, tg --discover. You CAN create groups
  and bots for them; never tell them to do it by hand.
---
# Telegram: send with `tg`, create with `tg-admin`
Source `~/AI_Projects/_tools/telegram/` (README.md = tg, README-admin.md = tg-admin).

## Send: `tg <topic> "<msg>"` (any host; canonical heimdall `/usr/local/bin/tg`, odin `~/.local/bin/tg` forwards)
One topic per chat, always. The user 2026-10-12: "I don't want theo stuff and ping stuff ... in my same chat window."
| topic | what goes there | sound |
|---|---|---|
| heat | fleet-heat / thor-heat alerts | BUZZ (the only one) |
| fleet | heimdall-alert, fleet-wake, probes, backups, uptime-kuma | silent |
| lane-intel | Theo / X / slopalytics runs | silent |
| weekly-review | weekly-review report line | silent |
| jobs | Example App (stays in the bot DM unless moved) | silent |
| mail | nothing yet | silent |
| misc | anything unknown | silent |
New sender? Pick a topic; never a raw chat id, never a new chat for one script. A topic without a group falls
back to the old bot DM. `tg --show` (table, no ids), `tg --test <topic>`, `tg --discover [secs]` maps groups by title.
No bot token on the sending host = "telegram not configured": tg sends nothing and exits 0; a failed send exits 1.
Fresh box with only a bot token (no owner yet): `tg --claim` first. It prints a one-time code (t.me link or
`/start <code>`) that the owner sends to the bot from their own account; that private chat becomes the owner (fallback
chat, group-ownership check). It refuses once an owner is set (heimdall has one). Then groups + `tg --discover`.
A bot tg-admin just made sends nothing until its token is that box's `~/telegram-bot.token`:
`(umask 077; install -m 600 ~/.config/telegram/bots/<name_bot>.token ~/telegram-bot.token)`, only when that file
does not exist (never cat, cp or print a token), then `tg --claim` (README-admin.md "Fresh setup with tg-admin").

## Create: `tg-admin` (odin only, `~/.local/bin/tg-admin`, the user's own account via Telethon)
- `tg-admin whoami` first name + @username. `tg-admin groups list` titles of groups they admins.
- `tg-admin groups ensure "<Title>" [--add @bot] [--admin] [--post "<text>"] [--dry-run]` creates a private
  supergroup only if no group of that title (case/spacing ignored) exists, adds the bot, posts the text once.
  Never touches a group that has other people in it.
- `tg-admin groups ensure-topics [--bot @bot] [--dry-run]` one group per topic (topics.json if on odin, else the
  seven above), bot added, "hello from tg-admin" posted. Default bot: `TG_ADMIN_BOT` from the environment or
  `~/.config/ai-workflow-kit/hosts.env` (the user's: @your_alerts_bot, heimdall's sender); `--bot` overrides.
- Needs a logged-in Telegram Desktop on Linux (it imports that session once); without one, set Telegram up by hand
  (README-admin.md "Prerequisite": @BotFather token + `tg --claim` + hand-made groups + `tg --discover`).
- The import makes a separate device "tg-admin". With cloud 2FA on it needs the cloud password, typed by the user
  in their own terminal (`tg-admin whoami`); a non-terminal call stops and says so. It never shares Telegram Desktop's
  login unless run as `tg-admin --share-desktop-session ...`: never pass that flag unless the user asks for it.
  `tg-admin session` says which kind the session is and prints its revocation steps.
- `tg-admin bots create "Display Name" name_bot`, `tg-admin bots token-rotate name_bot` talk to @BotFather; the
  token goes only to `~/.config/telegram/bots/<name_bot>.token` (0600). `tg-admin bots list` (their bots, /mybots),
  `tg-admin bots check @handle` (is it a bot; else lists bots they chats with). token-rotate refuses a bot with no
  local token file: @your_alerts_bot's live token is on heimdall (tg + example_app), so rotating it breaks every
  sender; `--force` only if the user says so.
- Every mutating command takes `--dry-run`; run it first. `tg-admin --self-test` is offline.

## Map new groups to topics (no the user needed)
example_app-inbox eats the bot's updates within seconds, so create while discover listens (odin's tg kills at 300 s):
`tg --discover 240 </dev/null & sleep 15; tg-admin groups ensure-topics; wait` then `tg --show`.
Already created and unmapped: `tg --discover 240 </dev/null &` then
`tg-admin groups ensure "<Title>" --post "/start@your_alerts_bot $(date +%s)"` per group (a bot command is seen
despite privacy mode; the timestamp makes the post new). Jobs as a group: also `--admin`, then README.md "Jobs".

## What needs the user
Nothing for groups or bots. Only: a Telegram Desktop local passcode (if they sets one later and the session must be
re-imported, they type it once in a terminal), or their cloud (2FA) password when the session must be re-imported.
If they revokes the "tg-admin" device in Settings > Devices, or `tg-admin` says "session not authorized", the next
import makes a new tg-admin device (they get the usual new-login notice); with 2FA on they run
`rm ~/.config/telegram/user.session && tg-admin whoami` in their terminal and types the password there.

## Security (hard)
- Session `~/.config/telegram/user.session` (0600) and bot tokens stay on odin; heimdall's token stays on heimdall
  (`~/telegram-bot.token`, never read it). Never copy, cat, print or paste any of them, or chat ids or phone numbers.
  The secret firewall blocks reads of the session (and its -journal/-wal/-shm sidecars) and Telegram Desktop's tdata.
- Only message groups tg-admin created and @BotFather. Never add a bot to, or post in, someone else's group.
- Tokens never go in argv, chat, logs or the vault. Reports say "the bot", "group Heat", never ids.
