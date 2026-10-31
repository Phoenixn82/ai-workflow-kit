# telegram: phone alerts, one chat per topic (add-on)

An add-on for any other profile: your boxes' alerts arrive in Telegram, each kind in its own group, so heat
warnings, fleet pings, the weekly review and the model-advice job never pile up in one chat. Only heat alerts make
a sound; everything else arrives silently. Without it nothing breaks: every alert becomes one log line.

It is the **telegram** profile ([profile.json](profile.json)): install it next to another one,
`install/install.sh --profile heimdall,telegram`. Telegram is optional everywhere; ntfy
([profiles/heimdall](../heimdall/README.md#alerts-on-your-phone)) is the other option.

## What you get

| Piece | One line |
|---|---|
| `tg <topic> "<msg>"` | the one sender: posts to that topic's group; silent unless the topic is `heat`; an unknown topic goes to `misc` |
| `tg-admin` (optional, Linux) | creates the groups and bots for you, as your own Telegram account |
| the telegram skill | tells your agents how to use both, so they send to the right topic and never paste a token |

Topics: `heat` (the only audible one), `fleet` (fleet-wake, the probes, backups, Uptime Kuma), `lane-intel`,
`weekly-review`, `jobs`, `mail`, `misc`. A topic without its own group falls back to your private chat with the
bot, so nothing is lost before you finish the setup.

**No Telegram set up = a quiet no-op.** With no bot token, `tg <topic> <msg>`, `tg --test` and `tg --show` print
one line, `tg: telegram not configured ...`, send nothing and exit 0, so every job that alerts keeps working.
A configured sender whose send fails still exits non-zero, so a real failure is never hidden.

## Install

From the kit's root folder (your clone; every command on this page runs from there):

```sh
install/install.sh --profile telegram --dry-run      # alone, or with another profile: --profile heimdall,telegram
install/install.sh --profile telegram
```

It links `tg` into `~/.local/bin` (and the skill into `~/.claude/skills` with `--harness`), and on Linux makes
`tg-admin`'s own Python environment (`~/.local/telegram/venv`, Telethon and opentele). Install it on the box that
sends: heimdall for the fleet alerts, odin for the agent jobs.

## Set it up

There are two ways, and the result is the same: a bot, one group per topic with the bot in it, and `tg` knowing
which group is which.

**By hand (everyone):**

1. In Telegram, talk to **@BotFather**: `/newbot`, pick a name. Save the token it gives you as
   `~/telegram-bot.token` on the sending box, mode 600, without printing it anywhere:
   `(umask 077; cat > ~/telegram-bot.token)`, paste, Enter, Ctrl-D. Never paste a token into a chat with an AI.
2. Claim the bot: run `tg --claim`. It prints a link, `https://t.me/<your bot>?start=<one-time code>`, and the
   same code as a `/start <code>` message. Within two minutes, open the link from your own Telegram account and
   tap Start (or send the message to the bot in a private chat). Your private chat with the bot becomes its owner:
   the fallback for topics without a group, and the account whose groups `tg --discover` accepts. Only your
   terminal shows the code, so someone else who messages your bot is never trusted; once an owner is set,
   `--claim` refuses.
3. Create one group per topic you want: **Heat, Fleet, Lane Intel, Weekly Review, Misc** (Jobs and Mail if you
   use them; spacing and case do not matter). Add the bot to each.
4. Run `tg --discover`. While it listens (two minutes), post the `/start@<your bot>` line it prints in each group.
   It maps the groups to topics by title, only groups you created, never repoints a topic that already has a live
   group, and prints which topics still fall back to your private chat.
5. Check: `tg --show` (the table, no ids printed), `tg --test fleet` (arrives silently), `tg --test heat`
   (buzzes).

**With `tg-admin` (Linux with Telegram Desktop):** `tg-admin` creates the groups and bots from your own account.
Its real prerequisite: **Telegram Desktop installed on this Linux machine and logged in** to the account that
should own them. It imports that desktop session once (from `~/.local/share/TelegramDesktop/tdata`, as a new
device named "tg-admin") and never asks for a phone number, a login code, an `api_id` or an `api_hash`. If your
account has a cloud password (2FA), the new device needs it once: tg-admin asks on a terminal (never type it into
a chat with an AI) and stops when there is no terminal. It never switches to sharing Telegram Desktop's own login
by itself; `tg-admin --share-desktop-session whoami` does that on purpose (see "To cut its access"). No logged-in
Telegram Desktop on Linux (macOS, Windows, a headless server, a phone-only account) means no tg-admin: use the
by-hand path above. With it, on the box that sends (installing tg-admin installs `tg` there too):

```sh
tg-admin whoami                                   # your name: the import worked
tg-admin session                                  # own "tg-admin" device or shared, and how to revoke it
tg-admin bots create "My Alerts" my_alerts_bot    # a new bot via @BotFather; token saved 0600, never printed
(umask 077; install -m 600 ~/.config/telegram/bots/my_alerts_bot.token ~/telegram-bot.token)   # tg sends with it
tg --claim                                        # open the link it prints, tap Start: your account owns the bot
tg-admin groups ensure-topics --bot @my_alerts_bot --dry-run    # see what it would create
tg --discover 240 </dev/null & sleep 15; tg-admin groups ensure-topics --bot @my_alerts_bot; wait
tg --show; tg --test fleet                        # every topic has its own group; the test arrives silently
```

The `install` and `tg --claim` lines are steps 1 and 2 of the by-hand path; tg-admin does neither for you: `tg`
sends only with `~/telegram-bot.token`, and `tg --discover` maps only groups created by the account that claimed
the bot. Already have a bot? Instead of `bots create` and `install`, save its token as in step 1 (never replace a
working `~/telegram-bot.token`), run `tg --claim` if `tg --show` says there is no owner yet, and pass that bot to
`--bot`.
Sending from another box: put the token there instead of the `install` line,
`T=$(cat ~/.config/telegram/bots/my_alerts_bot.token) && printf '%s\n' "$T" | ssh <box> 'umask 077; set -C; cat > ~/telegram-bot.token'`
(`set -C` refuses to replace an existing token; the secret firewall lets an agent run this form, not a `< token`
redirect), run `ssh -t <box> tg --claim`, then
`ssh <box> tg --discover 240 </dev/null & sleep 15; tg-admin groups ensure-topics --bot @my_alerts_bot; wait`.

Every changing command takes `--dry-run`; `ensure` only creates a group that does not exist yet and never touches
a group with other people in it. Set `TG_ADMIN_BOT=@my_alerts_bot` in `~/.config/ai-workflow-kit/hosts.env` to
skip `--bot`.

To cut its access, run `tg-admin session` first: it says which kind of session you have (the import records it
in `~/.config/telegram/tg-admin.mode`), names the entry in Settings > Devices, and prints these steps.

- **Own device (the default):** Telegram > Settings > Devices > "tg-admin" > Terminate, then
  `rm ~/.config/telegram/user.session`.
- **Shared (imported with `--share-desktop-session`):** there is no separate device; the session is Telegram
  Desktop's own login (one entry in Settings > Devices, named after whichever app connected last, so it can read
  "tg-admin"), and deleting `user.session` does not revoke a copy of it. Terminate the entry that
  `tg-admin session` names (this logs Telegram Desktop out too), then delete the file.

Either way, while Telegram Desktop is logged in on that machine a later tg-admin run can import again; to stop
that, remove tg-admin (`rm ~/.local/bin/tg-admin`) or keep Telegram Desktop logged out.

## Who sends what

- heimdall's jobs (fleet-wake, freellmapi-probe) call `/usr/local/bin/heimdall-alert`; the shim `setup.sh
  heimdall` installs posts to `fleet` once `tg` and the bot token are on heimdall.
- Uptime Kuma: Settings > Notifications > Telegram, the same bot token and the `fleet` group's chat id, "Send
  Silently" on. The id, on the box where you ran `tg --discover`:
  `python3 -c 'import json,os; print(json.load(open(os.path.expanduser("~/.config/telegram/topics.json")))["topics"]["fleet"]["chat_id"])'`
- The odin jobs (weekly review, lane-intel) call `tg weekly-review ...` and `tg lane-intel ...`.
- Heat alerts (host-heat) use `/etc/homeserver/alert.conf`; set `TELEGRAM_TOKEN` and the `heat` group's chat id
  there to have them in Telegram, the one chat that buzzes.

## Test

```sh
tg --self-test                     # offline
tg --test fleet                    # set up: arrives silently; not set up: "telegram not configured", exit 0
tg-admin --self-test               # offline (Linux, after install)
```

## Security

- The bot token lives in one file, mode 600 (`~/telegram-bot.token`); tg reads it in-process, never in a command
  line, a log or the output. Bot tokens tg-admin makes go to `~/.config/telegram/bots/<name>.token` (600).
- tg-admin's session file (`~/.config/telegram/user.session`, 600) is a login to your account: it stays on the
  machine that made it. Never copy it, and never give an AI a token, a chat id or a phone number. The kit's
  secret firewall blocks agents from reading it (with its SQLite sidecars) and Telegram Desktop's `tdata`.
- Revoking access depends on how tg-admin signed in (own device or shared with Telegram Desktop): see "To cut its
  access" above, or run `tg-admin session`.
- tg-admin only posts in groups it created and to @BotFather.
