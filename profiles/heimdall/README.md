# heimdall: the always-on watchman

One small, boring, always-on box that does the scheduled and watching work for all the others: checks every
machine is up (and knows when one sleeps on purpose), tells your phone when something breaks, wakes a box that
went down, runs nightly backups and scheduled jobs, serves the vault-search embeddings, and is where your own
projects get deployed. Optionally it blocks ads for the whole house.

Named after the maintainer's always-on box. It is the **heimdall** profile ([profile.json](profile.json)) and the
`heimdall` lane of the server scripts.

**Rule:** anything scheduled (timers, cron, recurring scrapes) and every small always-on service goes on
heimdall, not on your laptop (odin) or the GPU box (thor). Your laptop reboots and sleeps; thor sleeps when idle
and gets maintenance. Jobs on heimdall keep running through both. A job runs elsewhere only when it needs that
box (thor's own sleep check runs on thor).

## The hard rules

The checklist for every server and every project on it. The kit's own pieces follow it; keep yours to it too.

- [ ] **One job per box.** heimdall watches and schedules; heavy work goes to thor, media to loki.
- [ ] **Every job is registered**, with what it does, where it runs and its kill switch: one row in
      `~/AI_Projects/_system/automations/REGISTRY.md` (the installer writes rows for the kit's jobs).
- [ ] **Every job has a kill switch**: a `HALT` file it checks first, or at least a `systemctl disable` (each
      one is in [What runs](#what-runs)). The **global HALT** (`~/AI_Projects/_system/automations/HALT`) stops
      at once every job that runs as you and checks it (fleet-wake, the probes, yours); the system jobs
      (wake-watch, backup-push) keep their own switches.
- [ ] **Watchdogs ignore HALT.** Stopping every job must never stop the heat guard (`host-heat`) or the box's
      own firewall.
- [ ] **Nothing listens on the LAN unless it must.** Services bind to `127.0.0.1` and are published with
      `tailscale serve`; the host firewall drops the LAN (DNS for Pi-hole and Caddy's 80/443 are the only
      exceptions, each opened only when you set that piece up).
- [ ] **Secrets live in files with mode 600** (`.env`, keys, tokens), never in a compose file, a unit or git.
- [ ] **Every service has a health check and an alert**: an Uptime Kuma monitor (or a push URL for a job), with
      a notification to your phone.
- [ ] **Every service can come back**: its folder is backed up (backup-push to thor, restic for projects) and
      the restore is written down and tested.
- [ ] **Updates are deliberate**: images pinned to a version, upgraded by you after reading the release notes.

## Install

From the kit's root folder (your clone; every command on this page runs from there unless it says `cd`):

```sh
install/install.sh --profile heimdall --dry-run     # the plan
install/install.sh --profile heimdall               # asks once, then installs
```

That runs the server setup (`sudo servers/linux/setup.sh heimdall`), pulls and starts Uptime Kuma and ntfy
(Pi-hole and the embed server only if you turn them on), and installs fleet-wake and freellmapi-probe with their
timers left off. Tools in the profile: git, python3, jq, Tailscale, the `hosts.env` address book, Docker,
restic, Uptime Kuma, ntfy, Pi-hole and Unbound (optional), the server setup, fleet-wake and freellmapi-probe.
Fill in `~/.config/ai-workflow-kit/hosts.env` (template: [examples/hosts.env.example](../../examples/hosts.env.example)):
`THOR_HOST` and `THOR_MAC` for fleet-wake, `HEIMDALL_HOST` for the embed server, `FREELLMAPI_URL` for the probe.

Optional: install Hermes for an always-on Telegram gateway, and `thor-wake-proxy` if its router/GPU box sleeps. The gateway needs separate pairing and credentials; neither service is enabled by this profile. See [Hermes](../../docs/hermes.md).

## What runs

| Service | Where | Port (tailnet) | Job | Kill switch |
|---|---|---|---|---|
| uptime-kuma 2.5.6 | Docker | 127.0.0.1:3001 (tailscale serve) | dashboard + checks of every box and service, alerts | `docker compose stop uptime-kuma` |
| ntfy | Docker | 127.0.0.1:8090 (tailscale serve) | your own push-notification server (phone app) | `docker compose stop ntfy` |
| Pi-hole (optional) | Docker, profile `pihole` | 53 (LAN + tailnet), admin 8053 | ad and tracker blocking DNS for the house | `docker compose stop pihole` |
| Unbound (optional) | host | 127.0.0.1:5335 | Pi-hole's upstream, DNS-over-TLS | `systemctl stop unbound` |
| ollama-embed (optional) | Docker | 11434, tailnet address only | CPU-only embeddings for vault search from odin | `docker compose stop` in `~/services/ollama-embed` |
| fleet-wake | user timer, 2 min | | Wake-on-LAN for thor and odin, honours their holds | `touch ~/.local/state/fleet-wake/HALT` |
| freellmapi-probe | user timer, 30 min | | one real completion through the free-model router | `touch ~/.local/state/freellmapi-probe/HALT` |
| wake-watch | systemd timer, 2 min | | Wake-on-LAN recovery for your other boxes | `sudo touch /var/lib/wake-watch/HALT` |
| backup-push | systemd timer, 04:15 | | nightly add-only copy to thor (wakes it first) | `sudo systemctl disable --now backup-push.timer` |
| host-heat | systemd timer, 1 min | | heat watch (every lane); ignores HALT on purpose | `/etc/host-heat/env` |
| Cockpit | host | 9090 | web admin: logs, updates, services, terminal | `sudo systemctl disable --now cockpit.socket` |
| your projects | Docker + `app@<name>` user units | 127.0.0.1 only | [Deploy a project](#deploy-a-project) | `systemctl --user disable --now app@<name>` |
| Beszel, Dozzle, Homepage, Diun (optional) | Docker, one profile each | 127.0.0.1 (tailscale serve) | history, container logs, start page, update notes: [Monitoring and security](#monitoring-and-security-optional) | `docker compose stop <name>` |
| Wazuh 4.14.9 (optional) | Docker, `~/services/wazuh` | 127.0.0.1 (tailscale serve) | security events from every box's agent | `docker compose down` in `~/services/wazuh/single-node` |
| wazuh-tg (optional) | systemd service | | Wazuh level 12+ to your phone | `sudo touch /var/lib/wazuh-tg/HALT` |
| Wazuh MCP (optional) | Docker, profile `wazuh-mcp` | 127.0.0.1:8770 (tailscale serve) | an AI agent reads alerts, read-only | `docker compose stop wazuh-mcp` |
| fleet-scan (optional) | systemd timer, Sun 05:00 | | weekly triage + ClamAV of every box, one message | `sudo touch /var/lib/fleet-scan/HALT` |

## Hardware

Anything 64-bit with 4 GB RAM; 8 GB if you add more jobs or projects. Low idle power matters more than speed (a
U-series laptop idles at about 5 W). Wired Ethernet if possible: Wake-on-LAN packets for other boxes are sent on
the LAN, and DNS for the house should not depend on Wi-Fi. A box without a battery charge limit: enable "fan
always on" in the BIOS and check the battery monthly ([servers/common.md](../../servers/common.md#8-battery-look-at-it-monthly)).

## Setup

### Linux (recommended)

From the kit's root folder (or let `install/install.sh --profile heimdall` run it):

```sh
sudo servers/linux/setup.sh heimdall            # preview first: servers/linux/setup.sh heimdall --dry-run
```

Installs Docker, copies `servers/compose/heimdall` to `/opt/homeserver/heimdall`, starts uptime-kuma and ntfy,
installs wake-watch and backup-push with their timers, the `heimdall-alert` shim, Cockpit, and a backup key.
Like every lane it also gets the base: lid closed keeps running, sleep masked, SSH keys only, the firewall,
updates, heat watch, alerts ([servers/common.md](../../servers/common.md)). uptime-kuma and ntfy listen on
127.0.0.1 only, so no other machine can claim the uptime-kuma admin before you do. Share them with your tailnet
first:

```bash
sudo tailscale serve --bg --http=3001 http://127.0.0.1:3001
sudo tailscale serve --bg --http=8090 http://127.0.0.1:8090
```

Then from odin (on the tailnet): `http://heimdall:3001` (create the uptime-kuma admin),
`https://heimdall:9090` (Cockpit, your Linux login).

Pi-hole uses host networking, so the host firewall applies to it: DNS is reachable from the LAN and the
tailnet, its admin page over the tailnet only.

### macOS

From the kit's root folder:

```sh
servers/macos/setup.sh heimdall
```

colima + Docker run uptime-kuma and ntfy (ports on 127.0.0.1; share them with
`/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg --http=3001 http://127.0.0.1:3001` and the
same for 8090). wake-watch runs every 2 minutes from a LaunchDaemon as you, not root (macOS's /usr/bin/python3;
state in /var/lib/wake-watch, log in /var/log/wake-watch), no login needed. colima and Tailscale start at login:
turn on automatic login.
backup-push, host-heat, fleet-wake, the probes, the embed server and the `app@` units are Linux-only.

### Windows

From the kit's root folder, in an admin PowerShell:

```powershell
.\servers\windows\setup.ps1 -Lane heimdall
```

Docker Desktop runs uptime-kuma and ntfy (needs a signed-in user: automatic sign-in plus "Start Docker
Desktop when you sign in"). wake-watch runs as SYSTEM from Task Scheduler every 2 minutes and at startup,
via `C:\homeserver\wake-watch.cmd`; alerts through `C:\homeserver\alert.cmd`. Both folders are locked: only
administrators may change what SYSTEM runs or reads (`C:\homeserver`, `C:\ProgramData\wake-watch`, so edit
`wake-watch.conf` in an elevated editor and run `wake-watch.cmd status` from an elevated prompt). `alert.cmd` and
`C:\homeserver\heimdall\.env` can be read only by SYSTEM and administrators (plus you, for `.env`), so edit them
from an elevated editor. A `C:\homeserver` or `C:\ProgramData\wake-watch` that was already there is refused if
another account may change it (an owner or permission entry other than SYSTEM, Administrators or TrustedInstaller;
your own account counts too, since anything you run without elevation runs as it). Move such a folder away rather than only removing the access: removing it does not undo what that
account may have changed. `wake-watch.cmd` and `tools\wake-watch` are rewritten from the kit on every run, Python
runs with `-I -S` (no site initialization, so no `.pth` file or `sitecustomize` runs; the wrapper also clears
PYTHONHOME, PYTHONPATH, PYTHONEXECUTABLE and `__PYVENV_LAUNCHER__`), and every file in the machine-wide Python folder
is checked the same way (this can take a minute). Python must be 3.13.15 or later: before that, a
`C:\Modules\Setup.local`, which any account may create, makes it load its standard library from `C:\Lib`
(CVE-2026-12003); upgrade it from an elevated prompt with `winget upgrade --id Python.Python.3.13 --exact --scope
machine`. A Python with a `._pth` file beside python.exe, a `pyvenv.cfg` beside it or in the folder above, a
`pybuilddir.txt` beside it, no `Lib\os.py` (nor `os.pyc` or `python313.zip`), or a folder above it that other accounts may
add files to is refused too. setup.ps1 finds ProgramData and Program Files through Windows' known-folder API, not your
environment variables (anything you run without elevation can change those), and only on Windows' own drive, refuses a path under a folder that is a
junction or symlink or that another account owns or may delete, rename or re-permission, and refuses rather than writes
into `wake-watch.cmd` a path holding `% ! " ^ & | < >`. A Python your account owns or may change is refused, never repaired. Uninstall it, delete what is left of
`C:\Program Files\Python313`, reinstall it for all users from an elevated prompt (`winget install --id
Python.Python.3.13 --exact --scope machine`), and install packages into it only from an elevated prompt. Until a
run passes, `wake-watch.cmd` is replaced by one that runs nothing, so SYSTEM never runs a refused Python. An `alert.cmd` that another account could have changed is set aside as `alert.cmd.unsafe-<time>` (never
run) and replaced by a fresh one. Windows is the weakest choice
for this lane: it reboots for updates on its own schedule. Use it only if it is what you have.

## uptime-kuma: what to watch, and sleep on purpose

Add monitors (type, target):

- **TCP port** `loki:22`, `odin:22`: is the box up. (TCP, not ping; see [servers/lessons.md](../../servers/lessons.md).)
- **HTTP** `http://loki:8096/health`: Jellyfin. (Needs `tcp:8096` added to the server-to-quarantine grant in
  the Tailscale policy.)
- **DNS** `example.com` against `127.0.0.1`: Pi-hole.
- **HTTP** `http://127.0.0.1:<port><health path>` for every [deployed project](#deploy-a-project) (Kuma uses host
  networking, so it sees heimdall's loopback ports; on macOS use `host.docker.internal`).
- **Push** monitors for jobs: the job calls the push URL when it succeeds; silence = alert. For backup-push:
  `sudo systemctl edit backup-push.service`, add `[Service]` and
  `ExecStartPost=-/usr/bin/curl -fsS -m 10 -o /dev/null <push URL>` (a oneshot runs it only after success).

**thor sleeps on purpose** (an hour without real work, see [profiles/thor](../thor/README.md)), so its monitors
must not page you every night. Give thor's monitors (`thor:22`, `http://thor:11434`) **no notification**: they
stay on the dashboard as history, red while thor sleeps. The alert that matters comes from fleet-wake, which
knows the difference: a deliberate sleep leaves a hold and nothing happens; an unexpected outage gets one
Wake-on-LAN packet and one alert. freellmapi-probe also skips while thor holds a sleep.

Notifications: Settings > Notifications > ntfy, server `http://127.0.0.1:8090` (uptime-kuma shares heimdall's
network, so this works without `tailscale serve`; or ntfy.sh), your topic; or
Telegram with the bot of the [telegram add-on](../telegram/README.md) (keep "Send Silently" on).

**Who watches the watchman?** heimdall cannot report its own outage. Add one outside check: a free external
uptime service pinging a push URL, or a small timer on thor that alerts when `heimdall:22` stops answering
for 15 minutes.

## Alerts on your phone

**ntfy:**

1. Install the ntfy app. Add a subscription: server `http://heimdall:8090` (phone on the tailnet), topic = a
   long random name.
2. On every box: `NTFY_URL=http://heimdall:8090/<topic>` in `/etc/homeserver/alert.conf`, test with
   `sudo alert hello`. (loki cannot reach heimdall by design: give it `https://ntfy.sh/<topic>` or no
   alerts.)
3. iPhone: instant delivery needs the `NTFY_UPSTREAM_BASE_URL` relay line in `compose.yaml` (on by default; the
   relay sees only a hashed topic poll, not your message).

**Telegram:** the [telegram add-on](../telegram/README.md) gives you `tg <topic>`, one group per topic, and only
heat alerts make a sound. fleet-wake and freellmapi-probe alert through `/usr/local/bin/heimdall-alert`:
setup.sh installs a shim there that posts to the `fleet` topic when `tg` and your bot token are set up, and
otherwise hands the message to `alert` (ntfy or Telegram from `/etc/homeserver/alert.conf`, which is readable
by your group so jobs running as you can use it). With nothing configured an alert is one log line, never a
failure.

## Waking boxes: fleet-wake and wake-watch

Two wakers, for different boxes. **Never list one box in both**, or the second one wakes what the first was told
to leave asleep.

- **fleet-wake** (profile automation, user timer every 2 min, off until you enable it) watches thor and odin from
  `hosts.env` (`THOR_HOST` + `THOR_MAC`, `ODIN_HOST` + `ODIN_MAC`). After three missed checks in a row from a box
  that was up it sends one Wake-on-LAN packet and an alert; a **hold** (set by thor right before every suspend,
  or by a deliberate shutdown) means "off on purpose", and nothing happens. It is the one to use for thor, because
  thor-sleep talks to it. Commands: `fleet-wake status`, `fleet-wake wake thor`, `fleet-wake hold thor`.
  Turn it on: `systemctl --user enable --now fleet-wake.timer`. With `<NAME>_UNLOCK_HOST` set and a key from
  [luks-unlock](../../servers/tools/luks-unlock/README.md) in `~/.config/fleet-wake/<name>.luks`, fleet-wake also
  unlocks an encrypted odin or thor that sits at its disk password after a reboot; without the key file it only alerts.
- **wake-watch** (server lane, systemd timer every 2 min) watches the boxes in `/etc/wake-watch.conf`, which
  starts empty. Use it for any other box with Wake-on-LAN.

thor's hold reaches heimdall over SSH port 2222 with a key that can run exactly one command. setup.sh thor makes
the key and prints the line; it goes in **your** `~/.ssh/authorized_keys` on heimdall:

```
command="/usr/local/bin/fleet-wake hold thor",restrict ssh-ed25519 AAAA... fleet-hold@thor
```

Test from thor: `sudo /lib/systemd/system-sleep/fleet-hold pre`, then here `fleet-wake status` says
`thor: hold`. (`servers/linux/test_setup.sh` runs exactly this between two containers, and shows that a held thor
that goes quiet is left alone while an unheld one is woken.)

**wake-watch details.** Every 2 minutes it opens TCP port 22 on each box in `/etc/wake-watch.conf`:

```
# name      address    wired-MAC (on that box: ip -br link)
mybox       mybox      XX:XX:XX:XX:XX:XX
BROADCAST=255.255.255.255
```

After 3 misses in a row from a box that **was** up, it sends one Wake-on-LAN packet and an alert. It never
repeats and never wakes a box that was already off when watching started. Commands: `wake-watch status`,
`wake-watch wake mybox`, `wake-watch hold mybox`. Pause: `sudo touch /var/lib/wake-watch/HALT`.
Deliberate shutdowns: install `servers/tools/systemd/wake-watch-hold.service` on the watched box (steps are in
the file). At a clean shutdown it tells heimdall "hold", so the box is not woken straight back up. A power cut
cannot send that, which is exactly the case these wakers are for.

Limits (both): the target needs Wake-on-LAN on in its BIOS and its OS (`ethtool <port> | grep Wake-on` shows `g`;
setup.sh arms it at every boot), must be wired to the same LAN as heimdall, and must have had standby power. They
cannot revive a box that is on but hung, nor one that lost power completely unless its BIOS has "power on after
AC loss". Test once on purpose: shut the box down, `fleet-wake wake thor` (or `wake-watch wake <name>`), wait 2
minutes.

## Scheduled jobs: the pattern, the registry, HALT

Each job is a systemd service + timer pair, so it gets logs, a failure alert and a clean off switch:

```ini
# /etc/systemd/system/myjob.service
[Unit]
Description=My nightly job
OnFailure=alert@%n.service
[Service]
Type=oneshot
User=you
ExecStart=/opt/jobs/myjob.sh

# /etc/systemd/system/myjob.timer
[Timer]
OnCalendar=*-*-* 03:00
Persistent=true
RandomizedDelaySec=5m
[Install]
WantedBy=timers.target
```

`sudo systemctl enable --now myjob.timer`; list all with `systemctl list-timers`. Then:

- **Register it**: one row in `~/AI_Projects/_system/automations/REGISTRY.md` (what, when, where, how to stop
  it). The installer adds the rows for the kit's jobs; you add yours.
- **Kill switch**: the job checks a `HALT` file of its own first (`~/.local/state/<job>/HALT`), as fleet-wake and
  the probes do, and the global `~/AI_Projects/_system/automations/HALT` (create the folder on heimdall with
  `mkdir -p ~/AI_Projects/_system/automations`; then `touch .../HALT` stops fleet-wake, the probes and every job
  of yours that checks it; wake-watch and backup-push have their own switches in [What runs](#what-runs)).
- **Watchdogs ignore HALT**: host-heat keeps running whatever you stop.

**Remote actions from a chat bot or agent:** do not give it a shell. Give it a fixed menu: a small program
with a list of named operations (`disk_free`, `service_status <name from a list>`, `restart_service <name>`),
each with typed arguments that become exact command words, never shell text; read-only by default, and
anything that changes something asks for a one-time confirmation code. Log every run.

## Vault search embeddings (optional)

odin's vault search (`vault-recall`, injected on every prompt) needs an embedding server that is always up. thor
sleeps, so the profile runs a small CPU-only Ollama on heimdall with `nomic-embed-text` (about 300 MB, 3 CPUs at
most, 1.5 GB memory cap), bound to heimdall's **tailnet address** only (`HEIMDALL_HOST` in `hosts.env`; Compose
refuses to start without it). Template: [framework/_tools/vault-rag/heimdall/compose.yaml](../../framework/_tools/vault-rag/heimdall/compose.yaml).
The installer copies it to `~/services/ollama-embed` once and starts it; then load the model:

```sh
docker exec ollama-embed ollama pull nomic-embed-text:v1.5
docker cp framework/_tools/vault-rag/heimdall/nomic-embed-text.Modelfile ollama-embed:/tmp/m
docker exec ollama-embed ollama create nomic-embed-text:latest -f /tmp/m
```

On odin, set `EMBED_URL=http://heimdall:11434` in `hosts.env` (then turn on the reindex timer, see
[profiles/odin](../odin/README.md)). Stop it: `docker stop ollama-embed`
(search falls back to keywords; reindex refuses to run, the index stays as it was).

## Backups

**heimdall to thor (backup-push).** heimdall pushes `/opt/homeserver` (service configs plus SQLite snapshots and
the Pi-hole export) to thor every night at 04:15. The key heimdall holds can only write into one folder there.
thor sleeps when idle: set `WAKE_MAC` and backup-push sends it a Wake-on-LAN packet and waits up to 3 minutes for
it first (thor-sleep counts the rsync as work, so thor stays up until it is done).

1. `setup.sh heimdall` made the key and printed a line like this. On **thor**, append it to
   `~/.ssh/authorized_keys`:
   `command="rrsync -wo -no-del -no-overwrite /srv/backups/heimdall",restrict ssh-ed25519 AAAA... backup-push@heimdall`
   (`setup.sh thor` made `/srv/backups`.)
2. On heimdall: set `DEST=you@thor` and `WAKE_MAC=<thor's wired MAC>` in `/etc/homeserver/backup.conf`, then pin
   the host key and test:

```sh
sudo sh -c 'ssh-keyscan -p 2222 thor > /etc/homeserver/backup/known_hosts'
sudo systemctl start backup-push && journalctl -u backup-push -n 5    # "backup-push ok"
sudo systemctl enable --now backup-push.timer
```

`-wo` = write only, `-no-del` = deletes refused, `-no-overwrite` = existing files are never replaced, and
each night lands in its own dated folder (`2026-10-09/opt_homeserver/...`). So a wiped or hijacked heimdall
can add files but cannot erase or alter what is already on thor. `-no-overwrite` needs rsync 3.5+ on thor
(Debian 13+; Debian 12 and Ubuntu 24.04 ship 3.2.7 without it, and the backup then fails with an alert rather
than run unprotected). Every night is a full copy, so delete old dated folders on thor now and then (heimdall
cannot). The copy includes each stack's `.env` (passwords): fine on your own thor, but keep that folder private.

**Projects (restic).** `deploy/backup.sh <name>` keeps encrypted, deduplicated snapshots of each deployed project
in a restic repository. Config template: [deploy/restic.env.example](deploy/restic.env.example), copied to
`~/.config/restic/heimdall.env` (mode 600) with the password in its own 600 file. The simplest repository is a
second disk on heimdall; a repository on thor works too (thor sleeps: set the `WAKE_*` lines and backup.sh wakes
it first). Nothing deletes old snapshots for you: `restic forget --keep-daily 7 --keep-weekly 4 --prune`, by
hand, when the repository grows.

## Deploy a project

The out-of-the-box way to put any project you have, now or later, on heimdall, the same way every time. The
kit is [deploy/](deploy/):

| File | What it is |
|---|---|
| [deploy.sh](deploy/deploy.sh) | `deploy.sh NAME --image IMAGE --port PORT [--internal-port N] [--health PATH]`: makes `~/apps/NAME/`, starts it under systemd, waits until healthy |
| [project/compose.yaml](deploy/project/compose.yaml), [project/env.example](deploy/project/env.example) | the project template: one `app` service, port on 127.0.0.1 only, data in `./data`, logs capped, secrets in `.env` (600) |
| [app@.service](deploy/app@.service) | one systemd user unit for every project (`app@NAME`): `docker compose up` in the foreground, `Restart=on-failure`, starts at boot |
| [healthcheck.sh](deploy/healthcheck.sh) | `healthcheck.sh URL`: waits for a 2xx/3xx answer; deploy and restore use it, Uptime Kuma watches the same URL afterwards |
| [tailscale-serve.sh](deploy/tailscale-serve.sh) | `tailscale-serve.sh NAME`: HTTPS on your tailnet only, `https://heimdall.<tailnet>.ts.net:<port>` (the default) |
| [caddy/compose.yaml](deploy/caddy/compose.yaml), [caddy/Caddyfile](deploy/caddy/Caddyfile) | one Caddy for the projects that must be public: real certificates, one site block per domain |
| [backup.sh](deploy/backup.sh), [restore.sh](deploy/restore.sh), [restic.env.example](deploy/restic.env.example) | restic snapshot of a project (stopped for a consistent copy), and the way back |
| [test_deploy.sh](deploy/test_deploy.sh) | the whole path with a throwaway project in containers: deploy, health, backup, wipe, restore |

Deploy, from the kit's root folder on heimdall:

```sh
profiles/heimdall/deploy/deploy.sh myapp --image ghcr.io/you/myapp:1.2.0 --port 8081 --internal-port 3000 --health /health
profiles/heimdall/deploy/tailscale-serve.sh myapp          # https://heimdall.<tailnet>.ts.net:8081, tailnet only
```

1. The image: any container image (your registry, Docker Hub), or your own code: put it in `~/apps/myapp/src`
   and replace `image:` with `build: ./src` in `~/apps/myapp/compose.yaml`. That file is yours after the first
   deploy; deploy.sh never overwrites it (or `.env`). A database or a worker is one more service in it.
2. Settings and secrets: `~/apps/myapp/.env` (600). The app's data goes in `./data` (mounted at `/data`).
3. Health: deploy.sh waits for `--health` to answer. Add an Uptime Kuma HTTP monitor for
   `http://127.0.0.1:8081/health` with your phone notification: that is the project's alert.
4. Backups: `profiles/heimdall/deploy/backup.sh myapp` nightly (a timer, as in [the pattern](#scheduled-jobs-the-pattern-the-registry-halt),
   with `User=you`, since the unit and the project are yours), restore with `restore.sh myapp` (it moves the
   current folder aside, never deletes it). Test the restore once.
5. Register it: one row in the registry; its kill switch is `systemctl --user disable --now app@myapp`.
6. Updates: change the tag in `.env`, `systemctl --user restart app@myapp`. Logs: `journalctl --user -u app@myapp`.

User units need lingering, or they stop when you log out; deploy.sh turns it on once with
`sudo loginctl enable-linger $USER` (and says why first).

**Public on the internet (only when it must be).** `cp -r profiles/heimdall/deploy/caddy ~/apps/caddy`, put your
domain and the project's port in `~/apps/caddy/Caddyfile`, `systemctl --user enable --now app@caddy`, then
`sudo servers/linux/setup.sh heimdall --only=firewall` (with `~/apps/caddy/Caddyfile` present the firewall opens
TCP 80 and 443; nothing else), then, from a NEW ssh session: `sudo servers/linux/setup.sh --confirm-firewall`. Point the domain's DNS at your home address and forward 80 and 443 on the router
to heimdall. Caddy gets the certificate by itself. Everything else stays tailnet-only.

## Pi-hole (optional)

1. Unbound as the private upstream:
   `sudo apt install unbound && sudo cp /opt/homeserver/heimdall/unbound-pihole.conf /etc/unbound/unbound.conf.d/pihole.conf && sudo systemctl restart unbound`
   Test: `dig @127.0.0.1 -p 5335 example.com` returns an address.
2. Ubuntu only: port 53 is held by systemd-resolved. Set `DNSStubListener=no` in `/etc/systemd/resolved.conf`,
   `sudo systemctl restart systemd-resolved`.
3. Set `PIHOLE_PASSWORD` in `/opt/homeserver/heimdall/.env`, or leave it empty and read the random one Pi-hole
   makes with `docker logs pihole | grep password`, then
   `cd /opt/homeserver/heimdall && docker compose --profile pihole up -d`
4. Admin page: `http://heimdall:8053/admin`.
5. Router: set the DHCP DNS server to heimdall's LAN address (give heimdall a DHCP reservation first). Keep a
   second DNS server in the router (a second Pi-hole, or the router itself) so the house still resolves names
   when heimdall reboots.
6. Optional: Tailscale admin console > DNS > add heimdall as a nameserver for the tailnet.

Never run Pi-hole on loki: if that box is ever compromised, the whole house's DNS goes with it.

## Monitoring and security (optional)

A second layer on top of uptime-kuma: history for every box, container logs, one start page, update notes,
and security events with a weekly malware check. Every piece is optional, Linux only, and **report only**: none
of them blocks, deletes, quarantines or restarts anything, so a false alarm costs you a message, never your work.
Each one listens on 127.0.0.1 and reaches your other devices through `tailscale serve`, like uptime-kuma.

| Piece | What it is for | Runs | Tailnet address | Memory | Disk |
|---|---|---|---|---|---|
| Beszel 0.21.1 | CPU, memory, disk, temperature history for every box | hub: profile `beszel`; agents on each box | `https://heimdall.<tailnet>.ts.net:8490` | hub 25 MB, agent 10 MB | 60 MB images, history a few MB |
| Dozzle v11.3.5 | live container logs in the browser (heimdall, plus agents on other boxes) | profile `dozzle` | `:8481` | 20 MB | 100 MB image |
| Homepage v2.7.0 | one phone-friendly start page, a tile per service | profile `homepage` | `:3010` | 90 MB | 380 MB image |
| Diun 4.33.1 | a weekly note when an image you run has an update | profile `diun` | none | 15 MB | 90 MB image |
| Wazuh 4.14.9 | security events from every box: logins, changed system files, rootkit checks, vulnerable packages, container shells | `~/services/wazuh` (upstream clone) | dashboard `:5601`, agents `:1514` `:1515` | about 3 GB (the box needs 8 GB) | 7.5 GB images, plus alert indexes |
| wazuh-tg | Wazuh alerts of level 12 and up to your phone, one per rule and box per hour | systemd service | none | 15 MB | none |
| Wazuh MCP 5.0.0 | an AI agent can read alerts and agents; read-only | profile `wazuh-mcp` | `:8770` | 40 MB | 160 MB image |
| fleet-scan | weekly read-only compromise check plus ClamAV over every box's Downloads, one message | systemd timer, Sun 05:00 | none | ClamAV about 1 GB while it runs | 300 MB signatures |

The compose services are at the end of [servers/compose/heimdall/compose.yaml](../../servers/compose/heimdall/compose.yaml)
(setup.sh copied it to `/opt/homeserver/heimdall`). Without Wazuh the rest fits a 4 GB heimdall.

**Share them with your tailnet.** Turn on HTTPS certificates once (Tailscale admin console > DNS), then, for the
pieces you start:

```sh
sudo tailscale serve --bg --https=8490 http://127.0.0.1:8490              # Beszel
sudo tailscale serve --bg --https=8481 http://127.0.0.1:8481              # Dozzle
sudo tailscale serve --bg --https=3010 http://127.0.0.1:3010              # Homepage
sudo tailscale serve --bg --https=5601 https+insecure://127.0.0.1:5601    # Wazuh dashboard (its own certificate)
sudo tailscale serve --bg --tcp=1514 tcp://127.0.0.1:1514                 # Wazuh agents
sudo tailscale serve --bg --tcp=1515 tcp://127.0.0.1:1515                 # Wazuh enrollment
sudo tailscale serve --bg --https=8770 http://127.0.0.1:8770              # Wazuh MCP
tailscale serve status                                                    # every line says (tailnet only)
```

Undo one: the same line with `off` at the end (`sudo tailscale serve --https=8490 off`). Never use `funnel` for
these: that puts them on the internet.

### Beszel: history for every box

1. In `/opt/homeserver/heimdall/.env` set `BESZEL_APP_URL=https://heimdall.<tailnet>.ts.net:8490`, then
   `cd /opt/homeserver/heimdall && docker compose --profile beszel up -d` and the `tailscale serve` line.
2. Open the address right away and create the admin account (the first visitor does).
3. **heimdall's own agent:** Add system, name `heimdall`. Put the KEY and TOKEN it shows in
   `/opt/homeserver/heimdall/beszel-agent.env` as `KEY=...` and `TOKEN=...` (chmod 600), then
   `docker compose --profile beszel-agent up -d`.
4. **Other boxes:** agents dial out to the hub over WebSocket and listen on nothing (`DISABLE_SSH=true`). With
   Docker (thor), one compose file there, with that box's own KEY and TOKEN in `agent.env` (chmod 600):

   ```yaml
   services:
     beszel-agent:
       image: henrygd/beszel-agent:0.21.1
       container_name: beszel-agent
       network_mode: host
       env_file: ./agent.env
       environment:
         HUB_URL: https://heimdall.<tailnet>.ts.net:8490
         DISABLE_SSH: "true"
         DOCKER_IMAGE_CHECK: "false"
         SKIP_GPU: "true"        # polling keeps GPUs awake (heat, and thor never idles)
         # SENSORS: "-name*"     # hide a sensor input that reads nonsense
       volumes:
         - /var/run/docker.sock:/var/run/docker.sock:ro
         - ./agent_data:/var/lib/beszel-agent
       mem_limit: 128m
       cpus: 0.25
       restart: unless-stopped
   ```

   On a laptop without Docker (odin, raven), install the agent from the hub's Add system dialog (binary) or your
   distribution's package, at the hub's version, and say no to automatic updates. Then keep it from ever slowing
   the laptop: `sudo systemctl edit beszel-agent`, add the lines below, `sudo systemctl restart beszel-agent`.

   ```ini
   [Service]
   Environment=DISABLE_SSH=true
   Environment=SKIP_GPU=true
   Environment=PACKAGE_UPDATES_INTERVAL=0
   Environment=DOCKER_IMAGE_CHECK=false
   Nice=19
   CPUSchedulingPolicy=idle
   IOSchedulingClass=idle
   CPUWeight=1
   CPUQuota=5%
   MemoryMax=64M
   ```

Check: every box shows up green in the hub (a box that sleeps on purpose shows down while it sleeps);
`curl -fsS http://127.0.0.1:8490/api/health`. No alerts are set in Beszel on purpose: heat alerts come from
host-heat, outages from uptime-kuma and fleet-wake. Undo: `docker compose stop beszel beszel-agent`, and on each
box stop its agent. Backups: backup-push copies the hub's database while it runs; it is history only, so a torn
copy costs nothing that matters (add `beszel` to `EXCLUDES` in `/etc/homeserver/backup.conf` to skip it).

### Dozzle: container logs

`docker compose --profile dozzle up -d` and the `tailscale serve` line. Check: `https://heimdall.<tailnet>.ts.net:8481`
lists heimdall's containers; `curl -fsS http://127.0.0.1:8481/healthcheck`. It has no login and no actions or
shell; container logs can hold secrets, so keep the port tailnet-only. Undo: `docker compose stop dozzle`.

Another box's containers: run a Dozzle agent there, bound to that box's tailnet address (host networking: if
Tailscale is not up yet at boot, the agent exits and the restart policy retries):

```yaml
services:
  dozzle-agent:
    image: amir20/dozzle:v11.3.5
    container_name: dozzle-agent
    command: agent
    network_mode: host
    environment:
      DOZZLE_AGENT_ADDR: <that box's tailnet IP>:7007
      DOZZLE_HOSTNAME: thor
      DOZZLE_NO_ANALYTICS: "true"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    mem_limit: 128m
    cpus: 0.25
    restart: unless-stopped
```

then add `DOZZLE_REMOTE_AGENT: thor:7007|thor` to the `dozzle` service and `docker compose --profile dozzle up -d`.
Agents use Dozzle's built-in certificate, so any Dozzle on your tailnet could read that box's logs: let only
heimdall reach port 7007 (a Tailscale policy grant from heimdall to `tcp:7007` on that box and nothing else, or
an nftables rule on the box that accepts 7007 from heimdall's tailnet address and drops the rest).

### Homepage: one start page

1. In `.env` set `HOMEPAGE_ALLOWED_HOSTS=heimdall.<tailnet>.ts.net:3010,127.0.0.1:3010` (Homepage refuses other
   host names), then `docker compose --profile homepage up -d` and the `tailscale serve` line.
2. The first start writes example files into `/opt/homeserver/heimdall/homepage/`. Replace `services.yaml` with
   your tiles, and set `disableUpdateCheck: true` in `settings.yaml`. A tile links to the tailnet address and
   watches the container, for example:

   ```yaml
   - Monitoring:
       - Uptime Kuma:
           href: http://heimdall:3001
           server: heimdall
           container: uptime-kuma
       - Beszel:
           href: https://heimdall.<tailnet>.ts.net:8490
           server: heimdall
           container: beszel
   ```

   with `heimdall: {socket: /var/run/docker.sock}` in `docker.yaml`. Widgets that need a password or API key
   read it from a file: mount the file read-only and name it in an environment variable that starts with
   `HOMEPAGE_FILE_` (then `{{HOMEPAGE_FILE_NAME}}` in `services.yaml`), never the value itself. Use a key that can
   only read (Pi-hole: an app password; Immich: a key with stats only). A tile for a box that sleeps should call
   it directly, never through something that wakes it, or every page load wakes it.

Check: the page loads on your phone; `curl -fsS http://127.0.0.1:3010/api/healthcheck`. Undo: `docker compose stop homepage`.

### Diun: update notes

Diun looks at the images of your running containers every Sunday at 09:00 and sends one silent Telegram note per
image with an update. It never pulls or restarts anything: [updates stay deliberate](#the-hard-rules). Settings:
`/opt/homeserver/heimdall/diun.yml`. With pinned tags it reports a re-published image for the tag you run; for
new version tags of an image, give that container the labels in the comment at the top of `diun.yml`.

```sh
cd /opt/homeserver/heimdall
install -m 600 ~/telegram-bot.token diun-telegram.token        # or: (umask 077; cat > diun-telegram.token), paste, Ctrl-D
(umask 077; echo '[123456789]' > diun-telegram-chatids.json)   # the chat it posts in (the telegram add-on's "misc" group)
docker compose --profile diun up -d && docker logs diun 2>&1 | tail -5
```

Check: the log shows the next run time and no error; the first sighting of each image is silent on purpose. Undo:
`docker compose stop diun`.

### Wazuh: security events

Wazuh is three containers (manager, indexer, dashboard) from upstream's own compose files. The kit does not copy
them or their certificates; you clone upstream at the pinned tag and make five changes. It needs about 3 GB of
memory: put it on a heimdall with 8 GB or more, or skip it. Agents on the other boxes send their events to the
manager; active response (Wazuh blocking things by itself) stays off.

1. Prepare and generate the certificates (they stay on heimdall, never in git):

   ```sh
   echo vm.max_map_count=262144 | sudo tee /etc/sysctl.d/90-wazuh.conf && sudo sysctl --system
   git clone --depth 1 --branch v4.14.9 https://github.com/wazuh/wazuh-docker.git ~/services/wazuh
   cd ~/services/wazuh/single-node
   docker compose -f generate-indexer-certs.yml run --rm generator
   ```

2. Edit `docker-compose.yml`:
   - **Ports on 127.0.0.1 only.** Manager: `"127.0.0.1:1514:1514"`, `"127.0.0.1:1515:1515"`,
     `"127.0.0.1:55000:55000"`, and delete `514:514/udp` (syslog intake). Indexer: `"127.0.0.1:9200:9200"`.
     Dashboard: `"127.0.0.1:5601:5601"` instead of `443:5601`. Docker publishes ports past the host firewall, so the
     bind address is what keeps them off the LAN; the agents reach 1514 and 1515 through `tailscale serve --tcp`.
   - **No passwords in the file.** Replace each `INDEXER_PASSWORD`, `API_PASSWORD` and `DASHBOARD_PASSWORD` value
     with `${INDEXER_PASSWORD}`, `${API_PASSWORD}`, `${DASHBOARD_PASSWORD}` and put new values in
     `single-node/.env` (chmod 600; upper and lower case, a digit and a symbol, no `$` or quotes). Then set the
     same passwords inside Wazuh with upstream's "Change the password of Wazuh users" steps (a hash in
     `config/wazuh_indexer/internal_users.yml` for each indexer user, then `securityadmin.sh`):
     [documentation.wazuh.com](https://documentation.wazuh.com/4.14/deployment-options/docker/wazuh-container.html).
     The defaults are public; never start it on a network with them.
   - Delete the dashboard's `./config/wazuh_dashboard/wazuh.yml` volume line: that file carries the default API
     password, and without it the dashboard writes its API settings from the environment.

3. **Join password**, so only boxes you set up can join: in `config/wazuh_cluster/wazuh_manager.conf`, under
   `<auth>`, set `<use_password>yes</use_password>`. Make the password once, in a 600 file:
   `(umask 077; openssl rand -hex 16 > ~/.config/wazuh-join.pass)`.

4. Start it and hand the manager the password:

   ```sh
   docker compose up -d                                   # the first start takes a minute or two
   docker exec -i single-node-wazuh.manager-1 sh -c 'umask 027; cat > /var/ossec/etc/authd.pass; chown root:wazuh /var/ossec/etc/authd.pass' < ~/.config/wazuh-join.pass
   docker compose restart wazuh.manager
   ```

   Then the dashboard and agent lines of the `tailscale serve` block. Log in at `https://heimdall.<tailnet>.ts.net:5601`
   as `admin` with your new password.

5. **Retention**, or the alert indexes fill the disk: Indexer management > Index Management > Index policies >
   Create policy, JSON editor:

   ```json
   {"policy": {"description": "delete Wazuh alerts after 60 days", "default_state": "hot",
     "states": [
       {"name": "hot", "actions": [], "transitions": [{"state_name": "delete", "conditions": {"min_index_age": "60d"}}]},
       {"name": "delete", "actions": [{"delete": {}}], "transitions": []}],
     "ism_template": [{"index_patterns": ["wazuh-alerts-*"], "priority": 1}]}}
   ```

   It applies to new daily indexes; attach the existing ones once under Indexes > Apply policy.

**Agents.** On each box (not loki: it cannot reach heimdall by design, see
[servers/tailscale.md](../../servers/tailscale.md)), the agent at the manager's exact version, then the join
password, then start. Debian/Ubuntu:

```sh
curl -fsS https://packages.wazuh.com/key/GPG-KEY-WAZUH | sudo gpg --dearmor -o /usr/share/keyrings/wazuh.gpg
echo "deb [signed-by=/usr/share/keyrings/wazuh.gpg] https://packages.wazuh.com/4.x/apt/ stable main" | sudo tee /etc/apt/sources.list.d/wazuh.list
sudo apt update && sudo WAZUH_MANAGER=heimdall WAZUH_AGENT_NAME="$(hostname -s)" apt install wazuh-agent=4.14.9-1
sudo apt-mark hold wazuh-agent
(umask 077; ssh heimdall cat .config/wazuh-join.pass > wazuh-join.pass)   # copied, never printed
sudo install -m 640 -g wazuh wazuh-join.pass /var/ossec/etc/authd.pass && rm wazuh-join.pass
sudo systemctl enable --now wazuh-agent
```

Arch: the AUR `wazuh-agent` package compiles for a long while: build it once on a box that copes with the heat and
install the same package file on the others. Hold it at the manager's version (an agent newer than the manager is
refused). heimdall gets an agent too: it watches itself.

Optional, per agent, in its `/var/ossec/etc/ossec.conf` (then `sudo systemctl restart wazuh-agent`):

- **Containers:** `sudo apt install python3-docker` (Debian's Python is newer than Wazuh's own docker listener
  expects; the distribution package covers it) and add
  `<wodle name="docker-listener"><interval>10m</interval><attempts>5</attempts><run_on_start>yes</run_on_start><disabled>no</disabled></wodle>`.
  Health checks then log a "command launched in container" event many times a minute; mute that one rule on the
  manager (dashboard: Server management > Rules > Custom rules, `local_rules.xml`) and shells opened in a
  container still alert:

  ```xml
  <group name="docker,">
    <rule id="87907" level="0" overwrite="yes">
      <if_sid>87900</if_sid>
      <field name="docker.Action">^exec_start: </field>
      <description>Docker: Command launched in container $(docker.Actor.Attributes.name). Action: "$(docker.Action)"</description>
    </rule>
  </group>
  ```

- **Downloads:** watch the folder (inside `<syscheck>`:
  `<directories realtime="yes">/home/you/Downloads</directories>` and
  `<ignore type="sregex">/Downloads/.*\.(part|crdownload|tmp)$</ignore>`). Wazuh's VirusTotal integration can
  then look up the hash of every new file (free API key: 4 lookups a minute, 500 a day; only the hash leaves your
  network); a known-bad file becomes a level 12 alert. The key goes in `wazuh_manager.conf`; chmod 600 that file.
  Log only: nothing is moved or deleted.

Check: Agents in the dashboard lists every box as Active;
`docker exec single-node-wazuh.manager-1 /var/ossec/bin/agent_control -l`; an agent started without the
password is refused. Undo: `sudo systemctl disable --now wazuh-agent` on a box; on heimdall
`cd ~/services/wazuh/single-node && docker compose down` (add `-v` only if you also want the alerts and
settings gone). Updates: read the release notes, then move every agent and the manager to the same new tag.

### wazuh-tg: serious alerts to your phone

[servers/tools/wazuh-tg/wazuh-tg](../../servers/tools/wazuh-tg/wazuh-tg) follows the manager's `alerts.json` and
sends level 12 and up (7 to 11 stay in the dashboard), one message per rule and box per hour. It reads the file
only and runs as root because the Docker volume is root's.

```sh
sudo install -m 755 servers/tools/wazuh-tg/wazuh-tg /usr/local/bin/wazuh-tg
sudo install -d -m 700 /etc/wazuh-tg && sudo install -m 600 servers/tools/wazuh-tg/env.example /etc/wazuh-tg/env
sudoedit /etc/wazuh-tg/env              # NOTIFY_USER=you for the telegram add-on; WAZUH_ALERTS if your volume differs
sudo install -m 644 servers/tools/wazuh-tg/wazuh-tg.service /etc/systemd/system/
wazuh-tg --self-test && sudo systemctl daemon-reload && sudo systemctl enable --now wazuh-tg
```

Check: `journalctl -u wazuh-tg -n 3` says `following ...alerts.json`; the volume path is right when
`sudo ls /var/lib/docker/volumes/ | grep wazuh_logs` matches it. Undo: `sudo systemctl disable --now wazuh-tg`
(pause: `sudo touch /var/lib/wazuh-tg/HALT`).

### Wazuh MCP: ask an AI about alerts (optional)

[gensecaihq/Wazuh-MCP-Server](https://github.com/gensecaihq/Wazuh-MCP-Server) (MIT) lets Claude Code or another MCP
client read alerts, agents and vulnerabilities. Read-only on three levels: its API key carries only the
`wazuh:read` scope, the tools that act (block, isolate, kill, restart) are not offered at all, and the Wazuh users
it logs in as can only read.

1. Users that can only read: in the dashboard, a Wazuh API user (Server management > Security) with the built-in
   `readonly` role, and an indexer user (`internal_users.yml` plus `securityadmin.sh`, as in step 2 above) with
   the `readall` role (not `readall_and_monitor`).
2. `/opt/homeserver/heimdall/wazuh-mcp.env` (chmod 600) with the keys listed in `env.example`, a long random
   `MCP_API_KEY` among them. `docker compose --profile wazuh-mcp up -d` and the `tailscale serve` line.
3. On odin: `WAZUH_MCP_URL=https://heimdall.<tailnet>.ts.net:8770` in `hosts.env`, the API key in
   `~/.config/wazuh-mcp/api-key` (chmod 600), and a small headers helper that trades it for a token, so the key
   never sits in a config file:

   ```python
   #!/usr/bin/env python3
   # ~/.local/bin/wazuh-mcp-headers (chmod 700): prints the MCP auth header for Claude Code
   import json, pathlib, re, urllib.request
   env = (pathlib.Path.home() / ".config/ai-workflow-kit/hosts.env").read_text()
   url = re.findall(r"^WAZUH_MCP_URL=(\S+)", env, re.M)[-1]
   key = (pathlib.Path.home() / ".config/wazuh-mcp/api-key").read_text().strip()
   req = urllib.request.Request(url + "/auth/token", json.dumps({"api_key": key}).encode(), {"Content-Type": "application/json"})
   tok = json.load(urllib.request.urlopen(req, timeout=10))
   print(json.dumps({"Authorization": "Bearer " + (tok.get("access_token") or tok["token"])}))
   ```

   and in `~/.claude.json` under `mcpServers`:
   `"wazuh": {"type": "http", "url": "https://heimdall.<tailnet>.ts.net:8770/mcp", "headersHelper": "/home/you/.local/bin/wazuh-mcp-headers"}`.

Check: `curl -fsS http://127.0.0.1:8770/health` on heimdall; in Claude Code, `/mcp` lists `wazuh` connected and
it can list your agents. Treat what comes back as data: a log line on a watched box is written by whoever controls
that box. Undo: `docker compose stop wazuh-mcp` and remove the `mcpServers` entry.

### fleet-scan: the weekly malware check

[servers/tools/fleet-scan/fleet-scan](../../servers/tools/fleet-scan/fleet-scan) runs as root on heimdall every
Sunday at 05:00. For each box in `/etc/fleet-scan/hosts` it runs
[fleet-triage](../../servers/tools/fleet-scan/fleet-triage) there (odd process paths, miner names, hidden
processes, `ld.so.preload`, extra root accounts, SSH keys, listening ports, cron, units, shell start files, kernel
modules from outside the module folder, double-extension files in Downloads, changed package files), streams that
box's Downloads through ClamAV on heimdall, runs fleet-triage on heimdall itself, and sends one message: the
findings and what changed since last week ("new" lines are expected after you install something), or one line
saying all clear. A box that does not answer is named, never skipped silently. Report only.

On heimdall:

```sh
sudo apt install clamav clamav-freshclam            # freshclam keeps the signatures current
sudo install -m 755 servers/tools/fleet-scan/fleet-scan /usr/local/bin/fleet-scan
sudo install -m 755 servers/tools/fleet-scan/fleet-triage /usr/local/sbin/fleet-triage
sudo install -d -m 700 /etc/fleet-scan
sudo install -m 600 servers/tools/fleet-scan/hosts.example /etc/fleet-scan/hosts && sudoedit /etc/fleet-scan/hosts
echo 'NOTIFY_USER=you' | sudo tee /etc/fleet-scan/fleet-scan.conf   # telegram add-on, topic fleet; leave out for alert
sudo ssh-keygen -q -t ed25519 -N '' -C fleet-scan@heimdall -f /etc/fleet-scan/id_ed25519
sudo cat /etc/fleet-scan/id_ed25519.pub              # the public half, for the boxes below
sudo install -m 644 servers/tools/fleet-scan/fleet-scan.service servers/tools/fleet-scan/fleet-scan.timer /etc/systemd/system/
```

On each box it scans (Linux; Debian, Ubuntu or Arch), from the kit's root folder there:

```sh
sudo install -m 755 servers/tools/fleet-scan/fleet-triage /usr/local/sbin/fleet-triage
echo "$USER ALL=(root) NOPASSWD: /usr/local/sbin/fleet-triage" | sudo tee /etc/sudoers.d/fleet-triage
sudo chmod 440 /etc/sudoers.d/fleet-triage && sudo visudo -cf /etc/sudoers.d/fleet-triage
```

and ONE line in that user's `~/.ssh/authorized_keys`, so the key can run fleet-triage and nothing else:

```
command="sudo -n /usr/local/sbin/fleet-triage \"$SSH_ORIGINAL_COMMAND\"",restrict ssh-ed25519 AAAA... fleet-scan@heimdall
```

It connects to OpenSSH on port 2222 (the kit's port for machine jobs: Tailscale SSH answers 22 and ignores
`command=`). A box without that port gets `port=22` in the hosts file, only when Tailscale SSH is off there. Your
Tailscale policy must let heimdall reach that port. A box that sleeps on purpose gets `wake=<its MAC>`.

```sh
fleet-scan --self-test                               # the report logic, offline
sudo fleet-scan --dry-run                            # what it would contact, nothing sent
sudo systemctl start fleet-scan && journalctl -u fleet-scan -n 20   # a real run (the first one pins host keys)
sudo systemctl daemon-reload && sudo systemctl enable --now fleet-scan.timer
```

Check: the message arrives; `sudo cat /var/lib/fleet-scan/last-report.txt` holds the same text. A run takes
minutes per box (the package check on Arch is the slow part). Undo: `sudo systemctl disable --now fleet-scan.timer`,
and remove the `fleet-scan@heimdall` line from each box's `authorized_keys` and `/etc/sudoers.d/fleet-triage`.
Pause: `sudo touch /var/lib/fleet-scan/HALT`; the global HALT under `NOTIFY_USER`'s home stops it too.

**Monitor the monitors:** uptime-kuma HTTP monitors for `http://127.0.0.1:8490/api/health`,
`http://127.0.0.1:8481/healthcheck`, `http://127.0.0.1:3010/api/healthcheck`, `http://127.0.0.1:8770/health` and
`https://127.0.0.1:5601` (ignore TLS errors), each with your phone notification. fleet-scan and wazuh-tg alert
through `alert@` when they fail.

## Verify

```sh
systemctl list-timers | grep -E 'wake-watch|backup-push|host-heat'
systemctl --user list-timers | grep -E 'fleet-wake|freellmapi-probe'
fleet-wake status                                                   # thor: up / hold, odin: up
wake-watch status
docker ps --format '{{.Names}}: {{.Status}}'
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3001      # 200 or 302
ss -Hltn '( sport = :3001 or sport = :8090 )'                       # 127.0.0.1 only
sudo alert "heimdall test"                                          # arrives on your phone
heimdall-alert "heimdall-alert test"                                # the same, from your user (fleet topic with tg)
systemctl --user list-units 'app@*'                                 # your projects, all "running"
# from odin (tailnet): http://heimdall:3001 opens; from a LAN-only device it must not
# deploy kit, anywhere with Docker: profiles/heimdall/deploy/test_deploy.sh   -> PASS
```

## Maintenance

- `cd /opt/homeserver/heimdall && docker compose pull && docker compose up -d` monthly. The images are
  pinned to exact versions in `compose.yaml`: bump the tags there first, or `pull` changes nothing.
- Reboot after kernel updates, then check `fleet-wake status`, `wake-watch status` and your uptime-kuma page.
- Battery check monthly.

## Gotchas

- Booted with a dead clock battery: time is wrong until NTP syncs, which can upset TLS for a minute. Replace
  the coin cell if the case is open anyway.
- Interactive logins on a headless box (Tailscale, anything that waits for you to approve) belong in a
  background unit (`systemd-run`), not your SSH session.
- `docker compose` reads `.env` from the compose file's folder, not your current folder.
- A user unit that is "active" right after login but dead after logout: lingering is off
  (`loginctl show-user $USER -p Linger`).
- fleet-wake and freellmapi-probe say nothing when `/usr/local/bin/heimdall-alert` is missing; `setup.sh
  heimdall` puts the shim there.
