# thor: the heavy local AI box

The one machine with a real GPU (or an Apple silicon Mac with lots of memory). It runs local models with Ollama
for every other device on the tailnet, transcribes with Whisper on the GPU, takes big batch jobs, optionally
runs a free-model router, and is where backups land. It **sleeps when idle**: anything that needs it wakes it
first, and heimdall knows the difference between a nap and an outage.

Named after the maintainer's GPU box. It is the **thor** profile ([profile.json](profile.json)) and the `thor`
lane of the server scripts (`workhorse` is the old name and still works).

## Install

From the kit's root folder (your clone; every command on this page runs from there unless it says `cd`):

```sh
install/install.sh --profile thor --dry-run     # the plan, after the hardware check below
install/install.sh --profile thor               # asks once, then installs
```

**Hardware check first.** The profile stops before installing anything unless the machine has 16 GB RAM and
either an **NVIDIA GPU with 8 GB+ VRAM** (`nvidia-smi` must see it: install the driver first) or **Apple silicon
with 16 GB+ unified memory**. A machine that matches neither gets the reason and nothing half-installed. AMD GPUs
work with Ollama on Linux (ROCm-supported cards), but the profile's checks and thor-sleep read the GPU with
`nvidia-smi`; set it up from the steps below by hand.

The installer runs the server setup (`sudo servers/linux/setup.sh thor`, or `servers/macos/setup.sh thor`), then
the rest of the profile: git, python3, uv, node, jq, Tailscale, Docker, restic, Samba and smartmontools
(optional), Ollama, faster-whisper (optional), FreeLLMAPI (optional, Linux), the server setup, disk-probe
(timer off) and thor-sleep (timer off).

## What runs

| Service | Port | Job | Kill switch |
|---|---|---|---|
| Ollama | 11434 (tailnet only) | local models, OpenAI-compatible API at `/v1`; one big model plus a small one loaded at once | `sudo systemctl stop ollama` |
| faster-whisper (optional) | none (a library) | speech to text on the GPU, CPU fallback | nothing runs on its own |
| FreeLLMAPI (optional) | 127.0.0.1:3001 | side router over free-tier providers, never the primary path | `systemctl --user disable --now freellmapi` |
| backup receiving folder | SSH 2222 (tailnet only) | `/srv/backups/<host>`, write-only per sender (heimdall pushes) | remove the sender's key line |
| backup-pull | timer, 04:45 (wakes thor) | pulls loki's backups through a read-only account | `sudo systemctl disable --now backup-pull.timer` |
| thor-sleep | root timer, 10 min (off until you enable it) | suspends after 60 idle minutes, holding heimdall first | `sudo touch /etc/thor-sleep.disable` |
| disk-probe (optional) | user timer, 6 h (off) | one alert when a data disk passes 90 % or is unmounted | `touch ~/.local/state/disk-probe/HALT` |
| host-heat | timer, 1 min | heat watch: matters most here; ignores HALT on purpose | `/etc/host-heat/env` |

## Hardware

| Memory for the model (GPU VRAM, or unified memory on a Mac) | Comfortable model size (4-bit) |
|---|---|
| 6-8 GB | 7-8B parameters |
| 12-16 GB | 12-14B |
| 24 GB | 27-32B |
| 48 GB+ | 70B |

A model that does not fit the GPU spills into system RAM and gets 5-20x slower. NVIDIA on Linux, or Apple
silicon on macOS, are the easy paths.

**Never run models on a thin laptop.** Sustained full load can overheat a slim chassis to a hard
power-off. thor needs a desktop, a thick gaming laptop, or a Mac with fans, plus good airflow.
Set the BIOS to **power on after AC loss** and enable **Wake-on-LAN** (thor sleeps; heimdall and odin wake it).

## Setup

### Linux

From the kit's root folder (or let `install/install.sh --profile thor` run it):

```sh
sudo servers/linux/setup.sh thor                # preview first: servers/linux/setup.sh thor --dry-run
```

- Installs Ollama from its pinned release tarball (sha256-checked before unpacking), laid out the way its official script does it, with an `ollama` system user and `ollama.service`; the official script itself is not run.
- Drop-in `/etc/systemd/system/ollama.service.d/10-homeserver.conf`:
  `OLLAMA_HOST=0.0.0.0:11434`, bound to `homeserver-ollama-guard.service`: a small nftables table of its own
  (`inet homeserver_ollama`) lets port 11434 in only over loopback and `tailscale0`, whatever the main firewall does
  (its trial can roll back to nothing), and Ollama does not run without it,
  `OLLAMA_KEEP_ALIVE=5m` (unload a model 5 minutes after last use, so the memory comes back),
  `OLLAMA_NUM_PARALLEL=1` (predictable memory on one GPU) and `OLLAMA_MAX_LOADED_MODELS=2` (a big model plus a
  small one, such as an embedding model, stay loaded side by side; when both do not fit, Ollama evicts as usual).
- Creates `/srv/backups` for heimdall's nightly copies and installs `backup-pull` for loki's.
- **Power:** unlike every other lane, thor keeps sleep and suspend allowed. If an earlier run of the kit masked
  those targets, this run lifts exactly the masks the kit made (a mask someone else made is left alone, with a
  warning). It installs `thor-sleep`, its timer and the system-sleep hook that holds heimdall first, all left
  **off** until the hold works (below).
- Like every lane it also gets the rest of the base: lid closed keeps running, SSH keys only, the firewall,
  updates, heat watch, alerts ([servers/common.md](../../servers/common.md)).

NVIDIA driver, if `nvidia-smi` is missing: Debian `sudo apt install nvidia-driver firmware-misc-nonfree`
(needs `contrib non-free` in apt sources), Ubuntu `sudo ubuntu-drivers install`; reboot;
`sudo systemctl restart ollama`.

Container variant instead of the native install: `servers/compose/workhorse/compose.yaml` (`--profile nvidia`
needs the NVIDIA Container Toolkit). Share it with `sudo tailscale serve --bg --tcp=11434 tcp://127.0.0.1:11434`.

### macOS (Apple silicon)

From the kit's root folder:

```sh
servers/macos/setup.sh thor
```

Installs Ollama with Homebrew and runs it as a LaunchAgent with the same settings, listening on all
interfaces; a `pf` rule (loaded at boot by a LaunchDaemon) lets port 11434 in only from loopback and
tailnet addresses (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`), on every interface, so Wi-Fi and Ethernet
alike are closed to the home network. The rule comes first: setup.sh loads it before it writes or starts
the agent, and stops without starting Ollama when that fails (an agent an earlier run left, which did not
wait for the rule, is stopped and set aside first). At every boot the LaunchDaemon loads the rule and only
then records this boot's id in `/var/run/org.homeserver.pf.ready`; the agent waits for that record before
it starts Ollama, so after a restart Ollama never listens on every interface before the rule is loaded,
nor at all when loading fails. `~/Library/Logs/ollama.log` says when it waits; retry the boot job with
`sudo launchctl kickstart -k system/org.homeserver.pf`. Containers on a Mac cannot use the GPU: keep Ollama native.
Turn on automatic login so it starts after a power cut. A Mac thor never sleeps (the base keeps it awake;
thor-sleep is Linux-only): use the Mac's own energy settings if you want it to, and keep `thor-up` for the wake.

### Windows

From the kit's root folder, in an admin PowerShell:

```powershell
.\servers\windows\setup.ps1 -Lane thor
```

Installs Ollama with winget, sets `OLLAMA_HOST`, `OLLAMA_KEEP_ALIVE`, `OLLAMA_NUM_PARALLEL` as machine
variables, and allows port 11434 on the `Tailscale` interface only. The Ollama app runs in the tray of a
signed-in user: quit and reopen it after setup, and use automatic sign-in for a headless box.

## Sleep when idle, wake on demand

A GPU box that idles all day wastes power and heat. thor suspends itself after an hour with nothing happening.

- **`thor-sleep`** (root timer every 10 minutes) checks for real work, not logins: a login session, CPU load,
  GPU use, an Ollama model loaded and computing, busy processes (restic, rsync, ffmpeg, apt, docker builds).
  Six idle checks in a row, then it suspends. A probe that errors counts as busy, so a broken probe means thor
  stays up, never that it sleeps mid-job. `thor-sleep --dry-run` says what it sees, `thor-sleep --self-test`
  runs offline. Kill switch: `sudo touch /etc/thor-sleep.disable`.
- **The intentional-sleep hold.** Right before any suspend (thor-sleep's or yours), the hook
  `/lib/systemd/system-sleep/fleet-hold` tells heimdall's fleet-wake "this is on purpose", so heimdall does not
  wake thor straight back up. A deliberate shutdown does the same through `fleet-wake-shutdown@thor.service`.
- **`thor-up`** (on odin and raven) wakes it: probe port 22, else Wake-on-LAN plus a request to heimdall, then
  wait up to 120 seconds. Anything that needs thor runs `thor-up` first (backups to it, the session distiller's
  local fallback, lane calls). Needs `THOR_HOST` and `THOR_MAC` in `hosts.env`.
- **Timers wake it too:** `backup-pull.timer` has `WakeSystem=true`, and heimdall's backup-push sends a
  Wake-on-LAN packet when `WAKE_MAC` is set.
- **What must stay up lives elsewhere:** vault-search embeddings run on heimdall for exactly this reason.

Turn it on, after setup.sh printed the hold key (it makes `~/.ssh/fleet-hold` for you):

1. On heimdall, add the printed line to your `~/.ssh/authorized_keys`:
   `command="/usr/local/bin/fleet-wake hold thor",restrict ssh-ed25519 AAAA... fleet-hold@thor`
   (fleet-wake on heimdall needs `THOR_HOST` and `THOR_MAC` in its `hosts.env`.)
2. On thor, in your `~/.ssh/config` (port 2222: OpenSSH with the one-command key; Tailscale SSH on 22 would
   ignore the restriction):

```
Host heimdall
  # its tailnet name, or better its address: it must resolve while thor goes down
  HostName heimdall
  Port 2222
  IdentityFile ~/.ssh/fleet-hold
  IdentitiesOnly yes
```

   Then once, to accept heimdall's host key: `ssh heimdall true`.
3. Test the hold: `sudo /lib/systemd/system-sleep/fleet-hold pre`, then on heimdall `fleet-wake status` says
   `thor: hold`.
4. Enable: `sudo systemctl enable --now thor-sleep.timer fleet-wake-shutdown@thor.service`.

`servers/linux/test_setup.sh` covers this path in containers: the power policy per lane, the hold reaching
heimdall over the forced-command key, a held thor left alone while an unheld one is woken, and the installed
thor-sleep holding first and then calling `systemctl suspend`.

thor-sleep needs `nvidia-smi`. Without an NVIDIA GPU every check reads "probe failed: nvidia-smi", which counts
as busy: thor simply never sleeps (safe, just no saving).

## Use it from other machines

```sh
ollama pull qwen3:8b                                  # on thor; pick a size from the table
OLLAMA_HOST=http://thor:11434 ollama run qwen3:8b "hello"     # from any tailnet machine (thor-up first)
curl http://thor:11434/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen3:8b","messages":[{"role":"user","content":"hello"}]}'
```

Any tool that speaks the OpenAI API works with base URL `http://thor:11434/v1` and any API key string.
The first request after an idle spell loads the model: expect a pause (up to a minute or two for big ones),
longer if thor was asleep. On odin, `THOR_OLLAMA_URL=http://thor:11434` in `hosts.env` is what the kit's lanes use.

## Whisper on the GPU (optional)

`faster-whisper` goes into its own venv (`~/.local/lane-intel/venv`, with the CUDA libraries it needs); the
kit's lane-intel job uses it for video transcripts. It runs on the GPU and falls back to the CPU (`small.en`,
int8) when CUDA is missing or busy, so a transcript is slower but never fails for lack of a GPU. Check:
`~/.local/lane-intel/venv/bin/python -c 'import faster_whisper'`. thor-sleep counts a running transcription as
GPU work.

## FreeLLMAPI (optional side router)

[FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) routes requests across free tiers of hosted model
providers (and your Ollama) behind one OpenAI-compatible endpoint, with health-aware fallback. It is a side
router, never the primary path. Install as your user (not root), needs git, Node.js/npm and build tools; from
the kit's root folder:

```sh
bash servers/tools/freellmapi-bootstrap.sh ~/freellmapi
```

It checks out one pinned, reviewed upstream commit and installs exactly its lockfile (`npm ci`; bumping it:
[install/README.md](../../install/README.md#pins-exact-versions-and-how-to-bump-them)); an existing
`freellmapi.service` that differs is kept as `freellmapi.service.bak-<timestamp>` before the new one replaces it.
It runs as a `systemd --user` service on `127.0.0.1:3001` only (linger on, so it starts at boot), with a fresh
encryption key in `.env`. Add provider keys in its dashboard (`ssh -L 3001:127.0.0.1:3001 thor`, then
`http://localhost:3001`). To share it with the tailnet: `sudo tailscale serve --bg --http=3001 http://127.0.0.1:3001`,
and put `FREELLMAPI_URL=http://thor:3001/v1` in heimdall's `hosts.env` so freellmapi-probe can check it.
Stick to genuinely free models; set spending caps of $0 wherever a provider asks for a card.

## Backups

**Receiving (heimdall pushes).** `/srv/backups/<sender>` gets one folder per box. Each sender's key goes in your
`~/.ssh/authorized_keys` as `command="rrsync -wo -no-del -no-overwrite /srv/backups/<sender>",restrict ssh-ed25519 ...`
([profiles/heimdall](../heimdall/README.md#backups)). Senders can only add: each night goes into a new dated
folder, and existing files are never deleted or replaced. `-no-overwrite` needs rsync 3.5+ (Debian 13+; check
`rrsync -help`). Debian 12 and Ubuntu 24.04 ship 3.2.7, which rejects the flag, so backups fail loudly there;
dropping `-no-overwrite` makes them work but lets a hijacked sender overwrite old files. Old dated folders are
yours to prune. The senders connect to port 2222, because Tailscale SSH on port 22 would ignore that restriction.

**Pulling (loki).** loki may not start connections, so thor fetches its backups with `backup-pull` through a
read-only account on loki. Setup and restore: [profiles/loki](../loki/README.md#backups-thor-pulls-them). Config:
`/etc/homeserver/backup-pull.conf`; `backup-pull list` shows the dated copies.

**restic.** The profile installs restic, so thor can also hold restic repositories (odin's laptop backups, or
heimdall's project backups, over SFTP on port 2222 with a key of their own). Remember thor sleeps: the sending
side wakes it first (the heimdall deploy kit's [restic.env.example](../heimdall/deploy/restic.env.example) shows
the `WAKE_*` lines).

**Samba (optional).** The profile can install Samba to share a folder with the house; the loki template
[media-share.conf](../loki/samba/media-share.conf) is a read-only starting point (change `path`).

## Optional extras: photos, media backup, SSH check

### Photos: Immich

[Immich](https://github.com/immich-app/immich) (AGPL-3.0) is a self-hosted photo and video library: phone backup,
timeline, face and object search. It runs on thor next to your media disk, tailnet only.
`servers/compose/workhorse/immich/compose.yaml` is Immich's v3.3.3 release file with exact image pins, a read-only
mount of your existing photo folder, and CPU caps so the first indexing pass over a big library cannot overheat the
box. Immich asks for 6 GB RAM (8 GB comfortable) and 2 CPU cores (4 comfortable); keep its database on a local SSD.

```sh
cd servers/compose/workhorse/immich
cp env.example .env && chmod 600 .env          # fill in the paths and DB_PASSWORD (openssl rand -hex 24)
docker compose up -d
sudo tailscale serve --bg --tcp=2283 tcp://127.0.0.1:2283   # tailnet only, never Funnel
```

Then, from a tailnet machine, open `http://thor:2283`: the first visit creates the admin account. Add your photo
folder under Administration, External Libraries, with the import path `/external/library` (Immich only reads it).
On a box that also runs models, lower the job concurrency (Administration, Settings, Job settings) to 1 or 2. The
phone app uses the same address over Tailscale.

thor sleeps when idle, and a phone cannot wake it: uploads wait until thor is up (`thor-up` from a laptop, or the
nightly backup wake). While Immich indexes, thor-sleep sees the CPU load and stays awake.

Updates: bump the four images together, and read the [release notes](https://github.com/immich-app/immich/releases)
first; Immich has breaking changes between versions. Kill switch: `docker compose down` (your files stay).

### Media backup to a second drive

`servers/tools/media-backup` takes a nightly restic snapshot of your media folders (and a fresh dump of Immich's
database, since Immich's own nightly dump misses while thor sleeps) onto a **different drive** in thor. It wakes
thor for the run (`WakeSystem=true`), keeps 7 daily, 8 weekly and 12 monthly snapshots, checks the repository after
each run (Sundays it re-reads 5 % of the stored data), and alerts through `/usr/local/bin/alert` when something
fails. It clears **stale restic locks** first: a run killed by a reboot or a suspend leaves its lock behind, and every
later prune would fail on it. `restic unlock` without `--remove-all` removes only stale locks (from a process that
is gone, or not refreshed for 30 minutes), never a running backup's.

A second drive in the same box covers a dying drive, not fire, theft or ransomware on thor: keep an off-box copy of
what you cannot lose as well.

```sh
sudo install -m 755 servers/tools/media-backup/media-backup /usr/local/sbin/media-backup
sudo mkdir -p /etc/homeserver && sudo cp servers/tools/media-backup/media-backup.conf.example /etc/homeserver/media-backup.conf
sudoedit /etc/homeserver/media-backup.conf       # your folders, the second drive, a folder that must not be empty
sudo sh -c 'umask 077; mkdir -p /root/.config/restic; openssl rand -base64 32 > /root/.config/restic/media.pass'
sudo env RESTIC_PASSWORD_FILE=/root/.config/restic/media.pass restic init --repo /srv/backup-disk/media
sudo cp servers/tools/media-backup/media-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now media-backup.timer
```

Copy the password file somewhere off thor (your password manager): without it the repository cannot be read.
Kill switch: `sudo touch /etc/media-backup.disable`.

Check it:

```sh
sudo media-backup --check                        # config, mounts, repository opens, latest snapshot
sudo systemctl start media-backup && journalctl -u media-backup -n 20 --no-pager
cat /var/lib/media-backup/last-ok                # time of the last good run
bash servers/tools/media-backup/test.sh          # offline: a throwaway repository, a stale lock, an empty folder
```

Restore a folder: `sudo env RESTIC_PASSWORD_FILE=/root/.config/restic/media.pass restic --repo /srv/backup-disk/media restore latest --target /tmp/restore --include /srv/media/Photos/2024`.
Immich's database: stop Immich, then feed a dump from `/var/lib/immich/db-dumps` to a fresh database as
[Immich's restore guide](https://immich.app/docs/administration/backup-and-restore) shows.

Undo: `sudo systemctl disable --now media-backup.timer`, remove the two units, the script and the config. The
repository stays on the second drive; delete it yourself only when you no longer want those snapshots.

### SSH: check it is key-only

The server setup turns password logins off, but only once `~/.ssh/authorized_keys` has a key (so a headless box
cannot lock you out). A thor set up before you copied your key, or by hand, still takes passwords. Check:

```sh
sudo sshd -T | grep -E '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin) '
# passwordauthentication no, kbdinteractiveauthentication no, permitrootlogin no
```

Anything else: add your key (`ssh-copy-id`), then re-run `sudo servers/linux/setup.sh thor`. sshd keeps the
first value it reads, so a file in `/etc/ssh/sshd_config.d/` that sorts before `10-homeserver.conf`, or a line
above the `Include` in `/etc/ssh/sshd_config`, wins; `sshd -T` always shows what is in force.

## Verify

```sh
systemctl status ollama --no-pager | head -3
ss -ltnp | grep 11434                       # *:11434 or 0.0.0.0:11434
systemctl is-active homeserver-ollama-guard   # active: 11434 only from loopback and tailscale0 (sudo nft list table inet homeserver_ollama)
curl -s http://127.0.0.1:11434              # "Ollama is running"
# from another tailnet machine:     curl -s http://thor:11434     -> "Ollama is running"
# from a LAN-only device (phone with Tailscale off): http://<thor's LAN address>:11434 must time out
nvidia-smi                                  # GPU visible (NVIDIA)
ollama ps                                   # what is loaded, and on GPU or CPU
host-heat --status
systemctl is-enabled sleep.target suspend.target   # not "masked"
sudo thor-sleep --dry-run                   # "busy: ..." or "idle n/6"
backup-pull list                            # loki's nightly copies, once set up
```

On a Mac thor: `sudo pfctl -a com.apple/homeserver -s rules` lists the three rules, and
`cat /var/run/org.homeserver.pf.ready` matches `sysctl -n kern.bootsessionuuid` (the rule is loaded for this boot).

## Maintenance

- Ollama (Linux): setup.sh installs the version pinned at the top of `servers/linux/setup.sh` (`OLLAMA_VERSION` plus
  the `OLLAMA_SHA256_*` sums from that release's sha256sum.txt). To update, bump those, then
  `sudo systemctl stop ollama && sudo mv /usr/local/bin/ollama /usr/local/bin/ollama.old && sudo servers/linux/setup.sh thor`
  (your drop-in stays; the old lib folder is kept as /usr/local/lib/ollama.bak-<time>). Installed without the server
  lane, bump `install/tools.json` instead ([pins](../../install/README.md#pins-exact-versions-and-how-to-bump-them)),
  `brew upgrade ollama` (macOS), `winget upgrade Ollama.Ollama` (Windows).
- Old models eat disk: `ollama list`, `ollama rm <model>`.
- Watch `host-heat --status` during your first long jobs. Clean the fans yearly.
- Prune old dated backup folders in `/srv/backups/<host>/` now and then; nothing does it for you.

## Gotchas

- **Service bound to the tailnet IP, started before Tailscale had one:** it reports "active" and listens on
  nothing, for as long as nobody restarts it. Bind to `0.0.0.0` plus the
  firewall (as here), or to `127.0.0.1` plus `tailscale serve`, or wait for the address in the unit:
  `ExecStartPre=/bin/sh -c 'until tailscale ip -4 >/dev/null 2>&1; do sleep 2; done'`.
- Ollama listening only on `127.0.0.1` checks the request's host name (DNS-rebinding protection) and can
  answer 403 Forbidden to requests proxied in under another name, e.g. through `tailscale serve`. The setup
  here avoids that: native Ollama listens on all interfaces behind the firewall, and the Docker variant
  listens on all interfaces inside its container.
- `OLLAMA_NUM_PARALLEL` above 1 multiplies the memory for context; on one consumer GPU leave it at 1.
- Wake-on-LAN cannot help after a hard power cut unless the BIOS has power-on-after-AC-loss.
- thor woken by heimdall right after it went to sleep: the hold did not arrive. Run the test in step 3 above;
  the usual cause is a missing host key (`ssh heimdall true` once) or `HostName` that does not resolve.
- An idle thor that never sleeps: `sudo thor-sleep --dry-run` names the reason (a login session counts; so does
  a `docker build` or an apt run).
