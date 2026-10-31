# loki: the media server

Your own Netflix: Jellyfin streams your movies, shows and music to TVs, phones and browsers, at home and
(over Tailscale) away. Samba shares the library with the house's computers, thor pulls loki's settings
backups every night, and a one-way firewall keeps loki from opening a connection to any of your other
machines. Optionally, a download stack that finds and fetches what you request, locked inside a VPN and
fenced off from the rest.

Named after the maintainer's media box. It is the **loki** profile ([profile.json](profile.json)) and the
`loki` lane of the server scripts (`media` is the old name and still works).

## Install

From the kit's root folder (your clone; every command on this page runs from there unless it says `cd`):

```sh
install/install.sh --profile loki --dry-run     # the plan: tools, the server setup, Jellyfin
install/install.sh --profile loki               # asks once, then installs
```

The installer runs the server setup for you (`sudo servers/linux/setup.sh loki`, or `servers/macos/setup.sh
loki` on a Mac), then pulls Jellyfin and starts it. Tools in the profile: git, python3, Tailscale, Docker,
Jellyfin, Samba and smartmontools (optional), and the server setup. The rest of this page is what that setup
does, the steps only you can do, and how to check it.

> **Legal note.** Use this only for media you own or are licensed to use: your ripped discs, your own
> recordings, public-domain and Creative Commons works, files you bought DRM-free. Downloading or sharing
> copyrighted material without permission is illegal in most countries. The VPN and firewall here protect
> your network and privacy; they do not make unlicensed downloading legal.

## What runs

| Service | Port | Profile | Job |
|---|---|---|---|
| Jellyfin 12.1 | 8096 | `core` | the media server and its apps |
| gluetun | (holds 9091, 9696) | `arr` | WireGuard VPN tunnel + killswitch |
| Transmission 4.1.3 | 9091 | `arr` | torrent client, inside gluetun |
| Prowlarr 2.6.5 | 9696 | `arr` | indexer manager, inside gluetun |
| FlareSolverr 3.5.2 | 8191 (internal) | `arr` | gets Prowlarr past some bot checks, inside gluetun |
| Radarr 6.4.4 / Sonarr 4.0.20 | 7878 / 8989 | `arr` | movie / TV library managers |
| Seerr 3.5.0 | 5055 | `arr` | request page for the household |

Plus, on the host: ClamAV scanning of every finished download, a 2-minute timer that keeps the VPN's
forwarded port in Transmission, Samba (optional, read-only share of the library), a nightly
`db-snapshot` (consistent copies of Jellyfin's databases, 04:15) and the `backup-pull` account thor reads
the backups through (04:45, from thor).

## Why it looks like this

```
  your devices ──tailnet──▶ loki ──▶ Jellyfin ◀── reads /data/media (read-only)
                              │
                              ├─ radarr / sonarr / seerr  (normal network, 172.30.0.10/.11)
                              │        │ hardlink, no copy
                              │        ▼
                              │   /data  (one filesystem: downloads/ + media/, noexec)
                              │        ▲
                              └─ gluetun ══ WireGuard ══▶ VPN provider ──▶ internet
                                   └─ transmission, prowlarr, flaresolverr share gluetun's network:
                                      tunnel down = no network at all (killswitch)

  one-way: loki may answer, never start a connection to your LAN or tailnet devices
  thor ──tailnet, port 2222──▶ loki: pulls /opt/homeserver/media through a read-only account
```

- **Killswitch by construction.** The download containers have no network of their own. If the tunnel
  drops, they go silent instead of leaking your home address.
- **Quarantine.** Downloaded files are untrusted. The box is one-way at both layers: its host firewall
  rejects new connections to your home network, IPv4 and IPv6 (private and link-local ranges plus anything
  directly on the LAN, its global IPv6 prefix included; the router and DNS excepted), and `tag:quarantine`
  does the same on the tailnet ([servers/tailscale.md](../../servers/tailscale.md)). Storage is mounted `noexec`, ClamAV scans every file
  as it lands, and the box never holds your family photos or anything else you would miss.
- **One `/data` path everywhere**, so Radarr/Sonarr hardlink finished downloads into the library: instant,
  and no double disk use.
- **Ports bind to 127.0.0.1.** Docker publishes ports around the host firewall, so `0.0.0.0` would put every
  UI on your Wi-Fi. You reach them through `tailscale serve`.

## Hardware

Intel 7th gen or newer with the iGPU enabled: Quick Sync transcodes 4K HEVC with a few watts. Without it, a
CPU transcode of one 1080p stream needs a fairly modern 4-core CPU; most TVs direct-play and need no
transcoding at all. 8 GB RAM for the full stack (Jellyfin alone: 4 GB). Storage: an internal SSD for the OS,
the library on a big internal drive if it fits, or a USB 3 disk.

## Setup

### Linux (recommended)

From the kit's root folder (or let `install/install.sh --profile loki` run it):

```sh
sudo servers/linux/setup.sh loki                # preview first: servers/linux/setup.sh loki --dry-run
```

It installs Docker, creates `/srv/media/{downloads,media/movies,media/tv}`, copies the compose files to
`/opt/homeserver/media`, writes `.env` (your user id, time zone; enables hardware transcoding when it finds
`/dev/dri`), installs the ClamAV watcher and timers, starts Jellyfin, makes the read-only `backup-pull`
account thor pulls through, and turns on the nightly database snapshot. Like every lane it also gets the
base: lid closed keeps running, sleep masked, SSH keys only, the firewall, updates, heat watch
([servers/common.md](../../servers/common.md)).

Storage (the script never touches disks): give `/srv/media` its own partition or disk and mount it
`noexec,nosuid,nodev`. Example `/etc/fstab` line (find the UUID with `lsblk -f`):

```
UUID=xxxx-xxxx  /srv/media  ext4  defaults,noexec,nosuid,nodev  0  2
```

Then:

```sh
sudo tailscale serve --bg --http=8096 http://127.0.0.1:8096
# open http://loki:8096 from odin (your laptop), create the admin user, add libraries:
#   Movies -> /media/movies     Shows -> /media/tv
```

TVs on your home Wi-Fi that cannot run Tailscale: set `JELLYFIN_BIND=0.0.0.0` in `/opt/homeserver/media/.env`
and `docker compose --profile core up -d` again. That exposes only Jellyfin to the home network.

Hardware transcoding: Jellyfin > Dashboard > Playback > Transcoding > **Intel QuickSync (QSV)**, device
`/dev/dri/renderD128`, tick the codecs your CPU supports.

### Samba: the library on the house's computers (optional)

Template: [samba/media-share.conf](samba/media-share.conf), a read-only `[media]` share of `/srv/media/media`
for the members of the `sambashare` group. From the kit's root folder:

```sh
sudo apt install samba
sudo cp profiles/loki/samba/media-share.conf /etc/samba/media-share.conf
# then add this line under [global] in /etc/samba/smb.conf:   include = /etc/samba/media-share.conf
sudo usermod -aG sambashare "$USER" && sudo smbpasswd -a "$USER"   # Samba keeps its own password
testparm -s >/dev/null && sudo systemctl restart smbd
sudo servers/linux/setup.sh loki --only=firewall                    # opens TCP 445 to the home network
sudo servers/linux/setup.sh --confirm-firewall          # from a NEW ssh session, within 5 minutes
```

Over the tailnet it works without the last step. With `/etc/samba/media-share.conf` present the firewall step
lets the home network reach port 445 (Samba) and nothing else new; loki still cannot start a connection.
Connect with `smb://loki.local/media` (macOS, Linux) or `\\loki\media` (Windows). Read-only on purpose:
downloads stay quarantined on loki, and nothing from the house can change the library.

### Backups: thor pulls them

loki may not open connections, so it cannot push a backup anywhere. thor pulls instead: every night at 04:45
`backup-pull` on thor logs in to loki as `backup-pull`, an account whose only key is forced to
`rrsync -ro /opt/homeserver/media`. That key can read that one folder and nothing else: no shell, no writes,
no other path; root owns the key file, so the account cannot add keys to itself, and loki's ACLs (`setfacl`)
give it read access without changing anyone's file modes. At 04:15 `db-snapshot` writes consistent copies of
Jellyfin's databases to `/opt/homeserver/media/db-snapshots/` (a copy of a live SQLite file can be torn).
What is copied: `/opt/homeserver/media` minus `jellyfin/cache` (settings, users, watch history, the
`.env`); never the media itself (`/srv/media`, too big, and yours to keep copies of elsewhere).

1. On thor: `sudo servers/linux/setup.sh thor` (from the kit's root folder) made the pull key. Show it:
   `sudo cat /etc/homeserver/backup-pull/id_ed25519.pub`
2. On loki: add it as ONE line, with this prefix, to `/var/lib/backup-pull/.ssh/authorized_keys`
   (`sudoedit` it; root owns it):
   `command="rrsync -ro /opt/homeserver/media",restrict ssh-ed25519 AAAA... backup-pull@thor`
3. Tailscale policy: let servers reach loki's port 2222 (`"ip": ["tcp:22", "tcp:2222"]` in the
   server-to-quarantine grant; [servers/tailscale.md](../../servers/tailscale.md) has the full policy, with a
   test that loki can reach nothing).
4. On thor: `SOURCE_loki=backup-pull@loki` in `/etc/homeserver/backup-pull.conf`, pin loki's host key, test,
   then turn the timer on:

```sh
sudo sh -c 'ssh-keyscan -p 2222 loki >> /etc/homeserver/backup-pull/known_hosts'   # check the fingerprint once
sudo systemctl start backup-pull && journalctl -u backup-pull -n 5      # "backup-pull: loki ok -> ..."
sudo systemctl enable --now backup-pull.timer
backup-pull list                                                        # one dated folder per night
```

Each night is its own folder (`/srv/backups/loki/2026-10-12/`), hard-linked to the night before, so unchanged
files take no extra space. Nothing is ever deleted for you: prune old folders on thor yourself. The timer wakes
thor from sleep for the pull (`WakeSystem=true`). Tested by `servers/linux/test_setup.sh`: the pull works
through the one-way firewall, the key cannot write, run a command or read outside the folder, and a restore
brings a deleted file back.

### Restore

From your own computer (odin or raven), which may reach both boxes; loki still starts nothing:

```sh
thor-up                                                         # thor sleeps when idle: wake it first
ssh thor backup-pull list                                       # pick a night
ssh loki 'cd /opt/homeserver/media && docker compose --profile core down'
ssh thor 'tar -C /srv/backups/loki/2026-10-12 -cf - .' | ssh loki 'tar -C /opt/homeserver/media -xpf -'
ssh loki 'cd /opt/homeserver/media && docker compose --profile core up -d'
```

The files land as your user on loki (everything there is yours; Jellyfin runs with your user id). If Jellyfin
complains about its database, copy the snapshot over the live file while it is stopped: on loki,
`cp db-snapshots/data_jellyfin-2026-10-12.db jellyfin/config/data/jellyfin.db` (in `/opt/homeserver/media`;
the snapshot name is the database's path under `jellyfin/config` with `/` turned into `_`).
A new loki: run `setup.sh loki` first, then the restore.

### The download stack (optional)

1. Get WireGuard details from a VPN provider that gluetun supports; port forwarding makes downloads much
   faster (check that your provider offers it). Generate a **new** key for this box; do not reuse one from elsewhere.
2. Fill in `/opt/homeserver/media/.env`: `VPN_SERVICE_PROVIDER`, `WIREGUARD_PRIVATE_KEY` (and
   `WIREGUARD_ADDRESSES` if your provider needs it), `TRANSMISSION_USER/PASS`. Or use
   `VPN_SERVICE_PROVIDER=custom` and drop the provider's file at `gluetun/wireguard/wg0.conf`.
3. Start: `cd /opt/homeserver/media && docker compose --profile core --profile arr up -d`
4. Share the UIs: `sudo tailscale serve --bg --http=5055 http://127.0.0.1:5055` (and 7878, 8989, 9696, 9091
   if you want them away from home; otherwise `ssh -L 7878:127.0.0.1:7878 loki`).
5. Wire the apps together (each app's Settings):

| App | Setting | Value |
|---|---|---|
| Transmission | Download dir / incomplete dir | `/data/downloads/complete`, `/data/downloads/incomplete` |
| Radarr | Root folder; download client | `/data/media/movies`; Transmission, host `gluetun`, port 9091, your user/pass |
| Sonarr | Root folder; download client | `/data/media/tv`; same as Radarr |
| Prowlarr | Apps > Radarr / Sonarr | Prowlarr server `http://localhost:9696`; Radarr `http://172.30.0.10:7878`, Sonarr `http://172.30.0.11:8989`, API keys from each app |
| Prowlarr | Indexers > proxy | FlareSolverr at `http://localhost:8191` (tag the indexers that need it) |
| Seerr | Jellyfin; services | `http://jellyfin:8096`; Radarr `http://radarr:7878`, Sonarr `http://sonarr:8989` |

6. Turn on authentication in Radarr, Sonarr and Prowlarr (Settings > General > Authentication: Forms).
7. Tailscale admin console: tag the box `tag:quarantine`. Do not install `wake-watch-hold` here (it may not
   start connections); run `wake-watch hold loki` on heimdall before you switch it off on purpose.

### macOS

From the kit's root folder:

```sh
servers/macos/setup.sh loki
```

Installs the native Jellyfin app (it uses the Mac's VideoToolbox encoder: Dashboard > Playback >
Transcoding > Apple VideoToolbox) and colima + Docker for the optional download stack, copied to
`~/homeserver/media` with `DATA_DIR=~/Media`. Jellyfin runs as an app: turn on automatic login and add it
to Login Items. A Mac has no equivalent of the one-way host firewall here, so on macOS rely on
`tag:quarantine` and keep the Mac free of anything else you care about. The read-only backup account and the
database snapshot are Linux-only: on a Mac, back up `~/Library/Application Support/jellyfin` with Time Machine.

### Windows

From the kit's root folder, in an admin PowerShell:

```powershell
.\servers\windows\setup.ps1 -Lane loki                  # Jellyfin only
.\servers\windows\setup.ps1 -Lane loki -WithDownloads   # + Sonarr, Radarr, Prowlarr, qBittorrent (native)
```

Native apps, no VPN container, so the killswitch is yours to set: install your VPN's app, then qBittorrent >
Options > Advanced > **Network interface** = the VPN adapter. With that, qBittorrent cannot use any other
connection. For a box nobody signs in to, rerun the Jellyfin installer and pick "Install as a Service".
Set your home network to Private so TVs can reach port 8096.

## A Hermes that lives here (optional)

A second Hermes agent can run on loki and work only on loki, for jobs you hand it on this box. You start it from
your cockpit with `aihermes-loki`, which opens an `ssh -R` reverse tunnel for its model calls. loki still starts no
connection of its own: the tunnel exists only while your session is open, and `loki-llm-gate` on the cockpit passes
only the router's `/v1` API (its admin API and dashboard get 403). The example brief keeps it on this machine, keeps
cameras off and answers without refusals; because whatever it reads may reach free public-tier models, keep private
files off loki, as the quarantine already asks. The permissive brief changes how it answers, not what is legal: the
legal note above still applies.

Install, checks, the two-sessions quirk and undo: [docs/hermes.md](../../docs/hermes.md#a-second-hermes-on-a-quarantined-box).
Templates: [examples/hermes-loki/](../../examples/hermes-loki/README.md). If loki runs a desktop VM, `desk-shot` and
`desk-gui` let that Hermes see and drive its screen.

## Verify

```sh
docker compose -f /opt/homeserver/media/compose.yaml ps                # all "running", gluetun "healthy"
curl -s ifconfig.me; echo                                              # your home address
docker exec transmission curl -s ifconfig.me; echo                     # must be the VPN's address, not yours
docker exec gluetun cat /tmp/gluetun/forwarded_port; echo              # a port number (if forwarding is on)
docker stop gluetun; docker exec transmission curl -s -m 5 ifconfig.me || echo "killswitch OK"
docker start gluetun                                                   # port-sync re-attaches the others within 2 min
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8096/health  # 200
# one-way check, from loki: these must FAIL
nc -zv -w 3 <odin's LAN address> 22
nc -zv -w 3 <odin's IPv6 address> 22                                   # if your LAN has IPv6 (ip -6 addr)
nc -zv -w 3 heimdall 22
nc -zv -w 3 thor 22
# backups, on thor: a folder for last night, and the read-only key cannot write (this must FAIL)
backup-pull list
rsync -e 'ssh -p 2222 -i /etc/homeserver/backup-pull/id_ed25519' /etc/hostname backup-pull@loki:x
# antivirus: download the harmless EICAR test file into /srv/media/downloads/complete,
# then: journalctl -t media-scan -n 5   -> "INFECTED, moved to quarantine"
```

## Maintenance

- **Updates are deliberate.** Images are pinned to versions. To update: change the tag in `compose.yaml`
  (check the app's release notes), then `docker compose --profile core --profile arr pull` and `... up -d`.
- **After recreating gluetun** always run
  `docker compose up -d --force-recreate transmission prowlarr flaresolverr`. Otherwise they keep running in
  the old, dead network namespace: "Up" but with no network. `port-sync.sh` checks for this every 2 minutes.
- Quarantined files: `/srv/media/.quarantine` (ClamAV logs: `journalctl -t media-scan`). Delete, do not restore.
- Backups of the small stuff (`/opt/homeserver/media` minus `jellyfin/cache`) are thor's pull, above; the
  library itself is yours to copy elsewhere.

## Gotchas

- **Names do not resolve inside gluetun.** Prowlarr shares gluetun's network and cannot look up `radarr`; that
  is why Radarr and Sonarr have fixed IPs. If `172.30.0.0/24` clashes with your network, change the subnet,
  both IPs and `FIREWALL_OUTBOUND_SUBNETS` together.
- **Tunnel up, no traffic, DNS timing out:** with port forwarding on, gluetun only picks servers that support
  it; some plans or keys are not allowed on those. Try `VPN_PORT_FORWARDING=off`, or a key generated for a
  port-forwarding server.
- **Wi-Fi only?** The setup turns Wi-Fi power saving off; without that, slow LAN pings and failing
  `.local` names are normal on Intel cards.
- Seerr 2.x (Jellyseerr) cannot log in to Jellyfin 12; use Seerr 3.x as here.
- Jellyfin "Dynamic Image Provider" errors and no posters: the box has no working public DNS (see
  [servers/tailscale.md](../../servers/tailscale.md#troubleshooting), static resolv.conf).
- Copying a file from loki to another machine takes it out of every protection here. Scan it there.
- `backup-pull` fails with "Permission denied" on some files: a file was created later with a mode the ACL
  cannot reach. On loki: `sudo setfacl -R -m u:backup-pull:rX /opt/homeserver/media`.
