# The base every lane gets

`setup.sh base` (and every lane, which includes base) does the parts marked **(script)**. The rest needs
your hands once. Linux is the main path; macOS and Windows notes follow each section.

## 1. BIOS (once, at the keyboard)

Enter the BIOS: power on and tap the setup key (F2 Dell, F1 Lenovo, Esc then F10 HP, Del on most desktops).
Some firmware ignores `systemctl reboot --firmware-setup`; use the key.

| Setting | Set to | Why |
|---|---|---|
| Power on after AC loss / AC recovery / Restore on AC power loss | **On** | After a power cut the box comes back by itself. Without it, it waits for a finger on the power button. |
| Wake on LAN | **On** (often "LAN only") | heimdall can switch a box back on remotely. |
| ErP / Deep Sleep / Energy saving in S5 | **Off** | These cut standby power to the network port, which makes Wake-on-LAN impossible. |
| Battery charge limit (Dell "Primarily AC use", Lenovo "Battery charge threshold", HP "Battery Health Manager") | **60-80%** | A battery held at 100% around the clock ages fast and can swell. |
| Fan always on (some HP) | **On** if there is no charge limit | Heat is the bigger battery risk. |
| SATA operation | **AHCI** | RAID/RST mode hides the disk from Linux. |
| Boot order | internal disk first | Some HP firmware keeps booting the USB stick after install. See lessons.md. |

Not every laptop has every option. Missing "power on after AC loss" is the big one: plan for a person to press
the button after a long outage, or keep the battery in so short outages never reach it.

## 2. Lid closed, never sleep

**Linux (script):** logind ignores the lid and idleness (`/etc/systemd/logind.conf.d/10-server.conf`) and the
sleep, suspend and hibernate targets are masked (the script notes each mask it makes in
`/etc/homeserver/power-masks`). With the lid closed the backlight is turned off by acpid
(`servers/tools/lid-backlight`), so the panel does not heat the keyboard deck.

**thor is the exception:** it sleeps when idle on purpose (thor-sleep), so the thor lane lifts the masks the kit
made (and only those) and installs thor-sleep instead ([profiles/thor](../profiles/thor/README.md)).

**macOS (script):** `pmset -a sleep 0 disksleep 0 womp 1 autorestart 1 powernap 0` and `pmset -a disablesleep 1`
(the last one keeps it running with the lid closed and no display attached).

**Windows (script):** `powercfg` standby/hibernate timeouts 0, lid action "Do nothing" on AC and battery,
hibernate off (this also disables Fast Startup, which breaks Wake-on-LAN from a normal shutdown).

Check: close the lid, wait 5 minutes, `ssh` in from another machine.

## 3. SSH: keys only

Password logins are the thing internet scanners guess. Keys cannot be guessed.

1. On your laptop (odin), once: `ssh-keygen -t ed25519` (press Enter for the defaults).
2. Copy the key to the server **before** turning passwords off: `ssh-copy-id you@loki.local`
3. **Linux (script):** `/etc/ssh/sshd_config.d/10-homeserver.conf` turns password and root logins off and adds
   port 2222 (if your user has no `authorized_keys` yet, passwords stay on with a warning; re-run after step 2). Port 22 is for you; port 2222 is for machine jobs (backups, wake-watch) whose keys are pinned to a
   single command. Why two ports: on the tailnet, Tailscale SSH answers port 22 and does not read
   `authorized_keys`, so a key restriction there would be silently ignored. The firewall opens 2222 to the
   tailnet only.

**macOS (script):** turns on Remote Login (or tells you where to click if macOS refuses) and writes
`/etc/ssh/sshd_config.d/100-homeserver.conf` (passwords off only once `~/.ssh/authorized_keys` has a key).
**Windows (script):** installs the OpenSSH Server feature and sets `PasswordAuthentication no` once a key is in
place (no key: passwords stay on, with a warning). Keys for an
administrator account go in `C:\ProgramData\ssh\administrators_authorized_keys` (not your profile folder),
with permissions for Administrators and SYSTEM only; the script prints the `icacls` line. It finds ProgramData
through Windows, never the `%ProgramData%` variable, and writes `sshd_config` only when no folder above it is a link
or another account's.

## 4. Tailscale

Every box joins your tailnet, so you can reach it by name (`ssh heimdall`) from anywhere, with nothing
opened on your router. Full guide: [tailscale.md](tailscale.md).

**Linux (script):** installs Tailscale and starts `tailscale up --ssh` as a background unit, so the login
survives a dropped SSH session. Get the link with `journalctl -u ts-login -n 20`.
**macOS (script):** installs Tailscale's signed standalone app (`brew install --cask tailscale-app`). Open it
once at the Mac's screen (or over Screen Sharing), allow its system extension and VPN configuration, and log in.
It starts at login, so turn on automatic login. It cannot be a Tailscale SSH server: Macs use Remote Login
(OpenSSH, keys only) over the tailnet. Not Homebrew's `tailscaled` as a root service: root would run it from the
Homebrew folder, which your account can change. A re-run of setup.sh retires one that an earlier run started.
Run that re-run from a new ssh session from another computer on the home network, or from a terminal at the Mac's
screen, outside tmux and screen. When it cannot be sure that stopping the old service is safe, it leaves it
running, says why and asks for that re-run. That happens over a Tailscale session; inside tmux, screen, zellij,
herdr, dtach or abduco (a shell there keeps the SSH_CONNECTION it started with, whatever you attach over later);
with an SSH_CONNECTION that no live ssh session backs (an editor's remote server, mosh, nohup) or that comes from
the Mac itself (an ssh to itself, a forward, a tunnel); and while an ssh or Screen Sharing connection from a
Tailscale address is open (end it first). A fresh ssh started from inside tmux on your laptop passes
TERM=tmux-256color along and counts too; run it as `TERM=xterm-256color servers/macos/setup.sh <lane>`.
**Windows:** `winget install Tailscale.Tailscale`, then `tailscale up --unattended` so it stays connected
with nobody signed in. Tailscale SSH does not run on Windows hosts; use OpenSSH over the tailnet.

Then in the admin console: **disable key expiry** for each server (or tag it, which does the same), or it
drops off the tailnet after 180 days.

## 5. Firewall

**Linux (script):** nftables table `inet homeserver`, loaded by `homeserver-firewall.service`:

- inbound default **drop**; allowed: replies, loopback, ICMP, everything arriving over Tailscale, SSH from
  your home network (and IPv6 link-local, which is what `name.local` often resolves to), mDNS, DHCP.
- heimdall adds DNS (53) from the home network for Pi-hole (and 80/443 once you set up Caddy for a public
  project); loki adds Jellyfin (8096, 7359) from the home network for TVs (and Samba, 445, once you install the
  share), plus a one-way egress guard (see [profiles/loki](../profiles/loki/README.md)).
- It only replaces its own table. Debian's stock `/etc/nftables.conf` starts with `flush ruleset`, which would
  also wipe Docker's and Tailscale's rules on a reload, so it is not used.
- Every change is a trial, local or remote (sudo drops `SSH_CONNECTION`, so the script cannot tell): the new
  rules go into the running firewall only. A rollback timer (`homeserver-fw-rollback.timer`, armed before anything
  loads) puts the old table back 5 minutes after the run, and the rules file and boot setting change only when you
  confirm. Open a NEW SSH session (that it gets in is the test) and run `sudo servers/linux/setup.sh --confirm-firewall`.
  A confirm from a session that was already open is refused, also from inside tmux or screen while no new session
  is open; if your ssh shares connections (ControlMaster), open the new one with `ssh -o ControlPath=none`. A
  restart during the trial also brings the old firewall back. The trial, its rollback and the confirm take turns
  through one lock, `/run/homeserver-fw/lock`, in a folder only root can open: a confirm racing the deadline either
  keeps the new rules or reports 'too late' with the old firewall back, never both. The confirm reloads the running
  firewall in one nft transaction (never a restart, which leaves the box open for a moment); if that fails, the
  rules file and boot setting stay as they were and the rollback stays armed.
- SSH stays open on the ports sshd really listens on (`sshd -T`; 2222 stays tailnet-only). The address you ran the
  script from over SSH (another subnet, the internet, a custom port) gets its own allow line; never for 2222, and
  not for a tailnet address, which tailscale0 lets in anyway.

Two things a host firewall cannot do, by design:

- **Docker-published ports skip it.** Docker routes published ports before the input chain sees them. That
  is why every compose file here binds to `127.0.0.1` and you share services with `tailscale serve`.
- **Tailscale traffic skips it.** Tailscale accepts tailnet packets in its own chain first. Who may reach
  what over the tailnet is set in the Tailscale access policy ([tailscale.md](tailscale.md)).

**macOS (script):** application firewall on, stealth mode on. The thor lane adds a small `pf` rule that
blocks Ollama's port from the home network. It is loaded before Ollama listens on every interface: setup.sh
loads it before it writes or starts Ollama's agent (and stops if that fails), and after a restart the agent
waits until the boot job has loaded it for that boot ([profiles/thor](../profiles/thor/README.md#macos-apple-silicon)).
**Windows (script):** all firewall profiles on; per-lane allow rules are scoped to the `Tailscale` interface
(and the Private profile for Jellyfin).

## 6. Updates

**Linux (script):** `unattended-upgrades` installs security updates daily
(`/etc/apt/apt.conf.d/20auto-upgrades`). Kernel updates need a reboot; do one monthly:
`sudo apt full-upgrade && sudo reboot`. Containers do not update themselves: see each lane page.
**macOS (script):** automatic check, download and security responses on. Big macOS upgrades stay manual.
**Windows:** Windows Update is automatic; set Active Hours so it never reboots in the middle of a job.
Monthly: `winget upgrade --all`.

## 7. Heat watch

Laptops on a shelf with the lid shut run warmer than on a desk, and old fans clog.

**Linux (script):** `host-heat` runs every minute (`host-heat.timer`):

| Level | When | What happens |
|---|---|---|
| OK | normal | a CSV row in `/var/lib/host-heat/csv/` (90 days kept) |
| WARN | CPU 10 C under its limit, or a drive/battery near its own limit | alert |
| HOT | CPU 5 C under its limit, ... | cool down: turbo off, max CPU speed 50%, power-saver profile. Undone after 5 cool minutes. Nothing is stopped. |
| EMERG | at the limit | clean shutdown after 3 checks; also after 30 minutes of HOT with cooling on |

Two samples must agree before a level goes up, and it only drops 3 C below the line, so one spike does
nothing. Commands: `host-heat` (readout), `host-heat --status`, `sudo host-heat --resume` (undo cooling now).
Try it safely: `sudo host-heat --check --dry-run --fake cpu=99` (twice). Tune in `/etc/host-heat/env`: a big
laptop that normally runs near its limit gets `HOST_HEAT_CPU_OFFSETS=5,1` and `HOST_HEAT_HOT_MIN=15`.
Pause all actions: `sudo touch /etc/host-heat/HALT`.

**macOS:** the system throttles on its own; check with `pmset -g therm`. **Windows:** no built-in equivalent;
watch temperatures with HWiNFO now and then, and keep vents clear.

## 8. Battery: look at it monthly

A lithium battery held at 100% and warm all day ages fast and can swell. Once a month: is the case flat,
does the touchpad still click, does the lid close evenly? If not, unplug, power off, take the battery out
and recycle it. Most laptops run fine from the charger alone (no UPS effect then). Prefer a BIOS charge limit
(section 1). macOS: System Settings > Battery > Charging (Apple silicon) or the AlDente app.

## 9. Disk encryption vs. rebooting by itself

An encrypted disk asks for its passphrase at every boot, before Tailscale is up. A server that reboots
after a power cut or an update would sit at that prompt.

| Option | Reboots alone? | Protects against | Cost |
|---|---|---|---|
| No encryption (these servers' default) | yes | nothing on a stolen disk | keep secrets off the box; a stolen loki loses movies |
| Encrypted, TPM auto-unlock (`systemd-cryptenroll --tpm2-device=auto`; Windows BitLocker default) | yes | someone pulling the disk out | not someone stealing the whole laptop |
| Encrypted, unlock over SSH at boot (`dropbear-initramfs`) | no, you type the passphrase remotely | stolen laptop or disk | LAN only (no Tailscale yet at that stage); you must be around |
| OS plain, data partition encrypted, unlocked after boot | services wait | stolen data disk | one manual step per reboot |

macOS FileVault blocks automatic login; after a power cut someone must type the password. Pick per box:
your laptop (odin) encrypted, the servers plain or TPM, holding nothing you could not lose.

## 10. Backups

Rule of thumb: what exists in one place does not exist. Backups flow one way, to thor (Linux):

- **heimdall pushes** its service data every night with [backup-push](tools/backup-push), over SSH port 2222
  with a key thor pins to `rrsync -wo -no-del -no-overwrite /srv/backups/heimdall`, into a new dated folder each
  night: it can add files in that one folder, never read, list, delete or overwrite. A wiped or hijacked heimdall
  cannot take the backups with it. (`-no-overwrite` needs rsync 3.5+ on thor.) thor sleeps when idle, so
  backup-push wakes it first when `WAKE_MAC` is set.
- **thor pulls loki's** with [backup-pull](tools/backup-pull), because loki may not start connections: loki's
  `backup-pull` account has one key, forced to `rrsync -ro` on `/opt/homeserver/media` (read that folder,
  nothing else). Each night is a new dated folder on thor, hard-linked to the last one.
- Live SQLite databases are snapshotted with SQLite's own backup call (uptime-kuma by backup-push, Jellyfin by
  [db-snapshot](tools/db-snapshot)), Pi-hole with its export.
- A failure sends an alert (`OnFailure=alert@%n.service`). Nothing deletes old dated folders for you.
- Setup steps are printed by `setup.sh heimdall`, `setup.sh thor` and `setup.sh loki`, and in
  [profiles/heimdall](../profiles/heimdall/README.md#backups) and [profiles/loki](../profiles/loki/README.md#backups-thor-pulls-them).

The media library itself is usually too big for this; back up the Jellyfin config folder and the list of
what you own, not the files. A copy of anything irreplaceable (photos) also belongs off-site.

## 11. Alerts

`alert "message"` (`servers/tools/alert`, installed as `/usr/local/bin/alert`) sends one line to your phone. Put an ntfy topic (or a Telegram bot) in
`/etc/homeserver/alert.conf`. host-heat, wake-watch, backups and the virus scanner all use it.
Easiest: install the ntfy app, subscribe to a long random topic name, put `NTFY_URL=https://ntfy.sh/<topic>`
in the file, test with `sudo alert hello`. Self-hosted ntfy on heimdall: [profiles/heimdall](../profiles/heimdall/README.md#alerts-on-your-phone).
The file is mode 640, readable by your group, so jobs that run as you (fleet-wake, the probes) can alert too.
