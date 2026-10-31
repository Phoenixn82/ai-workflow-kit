# tg-admin: create Telegram groups and bots as the user (2026-10-12)
The user: "make a skill or something for telegram where you create bots and groups for me so that you remember you can."
`tg-admin` drives the user's own Telegram account (Telethon) on odin. `tg` (README.md) sends; tg-admin creates.
Skill: `skill/SKILL.md`, linked as `~/.claude/skills/telegram` and `~/.codex/skills/telegram`.

## Prerequisite (read before using it anywhere else)
tg-admin works only on a Linux machine with **Telegram Desktop installed and logged in** to the account that should
own the groups and bots: it imports that desktop session once (`~/.local/share/TelegramDesktop/tdata`, see below)
under Telegram Desktop's public app id, and never asks for a phone number, code, `api_id` or `api_hash`. No logged-in
Telegram Desktop on Linux (macOS, Windows, a headless server, a phone-only account) = no tg-admin. Everyone else
sets Telegram up by hand, once: create a bot with @BotFather (`/newbot`) and save its token as
`~/telegram-bot.token` (0600) on the machine that runs `tg`; run `tg --claim` there and send the one-time code it
prints to the bot from your own account (that makes you the owner); create one group per topic (README.md: Heat,
Fleet, Lane Intel, Weekly Review, Misc), add the bot to each, then run `tg --discover` there and post the
`/start@<bot>` line it prints in each group. Telegram stays optional: without a token `tg` says "telegram not
configured" and exits 0.
**Fresh setup with tg-admin** (a box with no sender yet; the user's heimdall has one): the same two first steps, only
the bot and the groups come from tg-admin. On the box that sends: `tg-admin bots create "My Alerts" my_alerts_bot`;
`(umask 077; install -m 600 ~/.config/telegram/bots/my_alerts_bot.token ~/telegram-bot.token)` (tg sends only with
that file; skip it if the file exists, never replace a working token); `tg --claim` and tap Start on the link it
prints (tg-admin does not claim for you, and `tg --discover` maps only groups created by the claiming account);
`tg --discover 240 </dev/null & sleep 15; tg-admin groups ensure-topics --bot @my_alerts_bot; wait`; then
`tg --show; tg --test fleet`. The kit's `profiles/telegram/README.md` has the same sequence, plus another sending box.
`groups ensure-topics` adds the bot named by `TG_ADMIN_BOT` (environment or `~/.config/ai-workflow-kit/hosts.env`;
The user's file names heimdall's sender) or `--bot @name`; with neither it stops and asks for one.

## How it signs in (once, no phone number, no login code)
First run reads Telegram Desktop's `~/.local/share/TelegramDesktop/tdata` with opentele and authorizes a new,
separate device named **tg-admin** (QR-login handoff from the desktop session; Telegram sends the usual new-login
notice). Cached at `~/.config/telegram/user.session` (0600, odin only). With no local passcode the import needs no input;
if one is set, the import asks for it once on a terminal.
- **Cloud 2FA on:** the new device needs the cloud password once. tg-admin asks for it on a terminal (run
  `tg-admin whoami` there; never type it into a chat); with no terminal it stops and says why. It never falls back
  to sharing Telegram Desktop's login on its own (audit 2026-10-13 S11; before that it silently did).
- **Shared mode, only when asked for:** `tg-admin --share-desktop-session whoami` (first import only; ignored once
  `user.session` exists) uses Telegram Desktop's own authorization. There is no separate device then: the session
  IS Telegram Desktop's login, one entry in Settings > Devices named after whichever app connected last (so after a
  tg-admin run it can read "tg-admin"; terminating it logs Telegram Desktop out).
- **Which one, and how to revoke it:** the import records its mode in `~/.config/telegram/tg-admin.mode`;
  `tg-admin session` prints it, how Settings > Devices lists the session right now, and the steps. Own device:
  Settings > Devices > that entry ("tg-admin") > Terminate, then `rm ~/.config/telegram/user.session`. Shared:
  deleting `user.session` does not revoke a copy of it; terminate the entry it names (that logs Telegram Desktop
  out too), then delete the file. A session imported before 2026-10-13 has no record: it gets the strict (shared)
  steps. In both modes a logged-in Telegram Desktop lets a later tg-admin run import again; to stop that, remove
  tg-admin (`rm ~/.local/bin/tg-admin`) or keep Telegram Desktop logged out.
- The secret firewall blocks agents from reading `user.session` (and its `-journal`/`-wal`/`-shm` sidecars) and `tdata`.
- opentele 1.15.1 needs two workarounds, both in tg-admin: Python 3.12 venv (3.13 adds class attributes its
  metaclass rejects) and a `readMapWith` patch (TDesktop 7.x map has key types it cannot parse; the auth key is in
  the mtp file, which still reads fine).
- Libraries (checked with `web gh`, 2026-10-12): LonamiWebs/Telethon 12.1k stars, MIT, archived on GitHub
  (last push 2026-02) but still released on PyPI (1.45.0); thedemons/opentele 473 stars, MIT, last push 2024-07.
  Rejected: DedInc/opentele2 (42 stars) for a credential-handling dependency.
- Env: `uv venv --python 3.12 ~/.local/telegram/venv && uv pip install --python ~/.local/telegram/venv/bin/python
  telethon opentele` (telethon 1.45.0, opentele 1.15.1, pyqt5 5.15.11, tgcrypto 1.2.5). tg-admin re-execs itself
  in that venv; linked as `~/.local/bin/tg-admin`, so `tg-admin ...` works from any shell.

## Commands
See `tg-admin --help` or the skill. Groups are private supergroups (stable ids, no migration). `ensure` matches
titles the way `tg --discover` does (case, spacing, punctuation ignored), never touches a group with other people
in it, and posts a text only if the user has not already posted that exact text there. Bots: tokens only in
`~/.config/telegram/bots/<name_bot>.token` (0600); the `secret` courier CLI does not exist on odin, so no copy is
made there (tg-admin says so). `bots list` parses BotFather /mybots (first page), local token files as fallback.
`bots token-rotate` refuses a bot with no local token file (its token lives elsewhere, e.g. the `TG_ADMIN_BOT`
sender on heimdall) unless `--force`. `--admin` grants explicit rights (not anonymous, no post/edit, no add_admins).

## Which bot sends what
- **The sender:** the bot whose token is `~/telegram-bot.token` on the alert host (heimdall); `tg` and Example App post
  with it, and it is in every topic group. `TG_ADMIN_BOT` (environment, else `~/.config/ai-workflow-kit/hosts.env`)
  names it, so `groups ensure-topics` adds it without `--bot`.
- **Bots tg-admin creates:** each token in `~/.config/telegram/bots/<name_bot>.token` (0600, odin); `tg-admin bots
  list` shows the account's bots. A bot with no topic group and no token file here is not used by `tg`.

## Registry row (`_system/automations/REGISTRY.md`)
| tg-admin: create Telegram groups/bots as the user's account (added 2026-10-12, the user: "make a skill or something for telegram where you create bots and groups for me") | on demand, no timer | zero-token Python (Telethon + opentele in `~/.local/telegram/venv`) | odin only: `~/AI_Projects/_tools/telegram/tg-admin`; session `~/.config/telegram/user.session` (0600; device "tg-admin", or Telegram Desktop's own login if imported with `--share-desktop-session`); bot tokens `~/.config/telegram/bots/*.token` (0600); messages only its own groups and @BotFather | nothing runs; to cut access run `tg-admin session` and follow its steps (own device: terminate "tg-admin" in Telegram Settings > Devices; shared: terminate the Telegram Desktop session it names), then `rm ~/.config/telegram/user.session` |

## SKILLS_INDEX lines (`SKILLS_INDEX.md`)
- "Skill tool" list: add `telegram`.
- "Tools with their own skill" list: add `telegram` (`_tools/telegram/skill/SKILL.md`: `tg` sender + `tg-admin`).
