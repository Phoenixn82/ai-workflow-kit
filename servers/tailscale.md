# Tailscale: how the boxes find and trust each other

Tailscale puts all your devices on one private network (a "tailnet"), wherever they are: home, office,
phone on mobile data. Nothing is opened on your router. Free for personal use.

## Names, not addresses

Turn on **MagicDNS** (admin console > DNS; on by default for new tailnets). Then every device is reachable
by its machine name: `ssh heimdall`, `http://loki:8096`, `OLLAMA_HOST=http://thor:11434`. Name each
machine after its lane (the Linux script passes `--hostname`; rename others under Machines).

Do not hard-code tailnet IPs in scripts; use the names.

## Install and join

| OS | Install | Join |
|---|---|---|
| Linux | Tailscale's apt repository (setup.sh adds it, with a pinned key and version) | `sudo tailscale up --ssh` |
| macOS server | `setup.sh` installs the standalone app (`brew install --cask tailscale-app`) | open the app, log in |
| macOS laptop (odin, raven) | `brew install --cask tailscale-app` | from the menu bar icon |
| Windows | `winget install Tailscale.Tailscale` | `tailscale up --unattended` |
| Phone | App Store / Play Store | sign in |

Open the link it prints and approve the device. Run a login on a remote box as a background unit (as
`setup.sh` does), not in your SSH session: if the session drops before you approve, the login is lost.

## Tailscale SSH

`--ssh` lets Tailscale answer SSH on the tailnet: no keys to copy, access decided by your policy file, works
from the phone app. Linux only; Mac and Windows hosts use normal OpenSSH over the tailnet.

The rule that makes it work for every device you own:

```json
"ssh": [
  { "action": "accept", "src": ["autogroup:member"], "dst": ["autogroup:self"], "users": ["autogroup:nonroot"] }
]
```

`autogroup:self` means "devices owned by the same person". Tagged devices (below) are not owned by a person,
so they need their own rule (in the full example). `"action": "check"` instead of `"accept"` asks you to
re-confirm in a browser now and then; it breaks scripts, so use it only where you want that friction.

Tailscale SSH answers port 22 on the tailnet and ignores `~/.ssh/authorized_keys`. Machine-to-machine jobs
that need a key pinned to one command use OpenSSH on port 2222 instead ([common.md](common.md#3-ssh-keys-only)).

## Tags: servers and the quarantined media box (loki)

A tag replaces a device's owner with a role. Two are used here:

- `tag:server` for heimdall and thor. Tagged devices never hit key expiry.
- `tag:quarantine` for loki (the media box) if it runs the download stack: your devices may reach it, it may
  start nothing toward anything else. Filters are stateful, so its replies still flow. The servers may reach
  its SSH ports: 22 for heimdall's check that it is alive, 2222 for thor's nightly backup pull.

Apply a tag: admin console > Machines > the device > Edit ACL tags. (Or `sudo tailscale up --advertise-tags=tag:server`.)

## The firewall gotcha

Tailscale accepts tailnet packets before the host firewall (ufw, nftables, Windows Firewall) sees them. A
host firewall rule cannot block one tailnet device from another. **Isolation on the tailnet lives in the
policy file**, not on the hosts. The one-way media rule therefore has two halves: the host firewall on the
media box, loki (for the home network) and `tag:quarantine` in the policy (for the tailnet).

## Example policy file

Admin console > Access controls > JSON editor. Replace the contents, save (the `tests` must pass or the save
is refused).

```hujson
// Home server tailnet policy.
// Your own devices reach everything. Servers reach each other. loki, the media box (tag:quarantine),
// can be reached but cannot start a connection to anything.
{
	"tagOwners": {
		"tag:server":     ["autogroup:admin"],
		"tag:quarantine": ["autogroup:admin"],
	},

	"grants": [
		// Your devices (laptop, phone): everything, including loki.
		{"src": ["autogroup:member"], "dst": ["*"], "ip": ["*"]},
		// Servers to servers: backups, monitoring, Ollama, wake-watch probes.
		{"src": ["tag:server"], "dst": ["tag:server"], "ip": ["*"]},
		// heimdall may check loki is alive (22) and thor pulls its backups through the read-only account (2222).
		// Nothing has tag:quarantine as a source.
		{"src": ["tag:server"], "dst": ["tag:quarantine"], "ip": ["tcp:22", "tcp:2222"]},
	],

	"ssh": [
		// You, into your own untagged devices.
		{"action": "accept", "src": ["autogroup:member"], "dst": ["autogroup:self"], "users": ["autogroup:nonroot"]},
		// You, into the servers and loki.
		{"action": "accept", "src": ["autogroup:member"], "dst": ["tag:server", "tag:quarantine"], "users": ["autogroup:nonroot"]},
	],

	"tests": [
		{"src": "tag:quarantine", "deny": ["tag:server:22", "tag:server:2222", "tag:server:11434"]},
		{"src": "tag:server", "accept": ["tag:server:2222", "tag:quarantine:22", "tag:quarantine:2222"], "deny": ["tag:quarantine:8096"]},
	],
}
```

Add a test with your own login as `src` (for example `"accept": ["tag:quarantine:8096"]`) if you like; it
needs your email address, so it is left out here.

## Sharing a web UI with the tailnet only

Services here listen on `127.0.0.1`. `tailscale serve` publishes one on the tailnet and nowhere else:

```sh
sudo tailscale serve --bg --http=8096 http://127.0.0.1:8096     # Jellyfin -> http://loki:8096
sudo tailscale serve --bg --http=5055 http://127.0.0.1:5055     # Seerr
sudo tailscale serve --bg --tcp=11434 tcp://127.0.0.1:11434     # raw TCP forward (Ollama in Docker)
tailscale serve status                                          # list
sudo tailscale serve --http=8096 off                            # remove one
```

`--bg` keeps it across reboots. `--https=443` gives a real certificate at `https://loki.<tailnet>.ts.net`
(turn on HTTPS in the admin console first). `tailscale funnel` would publish to the whole internet: do not.

## Your phone

Install the Tailscale app and sign in. Your phone can then open `http://heimdall:3001` (uptime-kuma, after
`tailscale serve` on heimdall: [profiles/heimdall](../profiles/heimdall/README.md)),
`http://loki:8096` (Jellyfin), SSH from an SSH app, and receive ntfy alerts from heimdall. Approving a new
device login is a tap in the app or the admin console.

## Key expiry

Device keys expire after 180 days by default and the device silently drops off. For servers: tag them, or
admin console > Machines > ... > **Disable key expiry**. Leave expiry on for laptops and phones.

## Exit node (optional)

`sudo tailscale up --advertise-exit-node` on heimdall, approve it in the admin console, and your phone can
route its internet through home when on public Wi-Fi. Never on loki.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `tailnet policy does not permit you to SSH as user X` | Wrong user name on that box, or a real policy block. Both give the same message. | Check the account exists (`id X` on the box) before touching the policy. |
| Box vanished after months | key expiry | Re-login on the box, then disable expiry / tag it. |
| `NeedsLogin` after you approved | the login ran in an SSH session that died | `sudo systemd-run --unit=ts-login tailscale up --ssh`, approve the new link |
| Public names stop resolving on a box | `/etc/resolv.conf` was set by hand to the MagicDNS resolver only | Let Tailscale manage it, or point it at your router; `tailscale set --accept-dns=false` alone does not rewrite a static file. |
| A firewall rule does not block a tailnet device | Tailscale traffic bypasses host firewalls | Use the policy file. |
| A service bound to the tailnet IP is "active" but nothing answers | it started before Tailscale had its address | Bind to `0.0.0.0` + firewall, or `127.0.0.1` + `tailscale serve` ([lessons.md](lessons.md)). |
| `ping` fails but the box is fine | ICMP is dropped somewhere | Test with TCP: `nc -zv heimdall 22` or `tailscale ping heimdall`. |
| Slow, "relay" in `tailscale status` | strict NAT on one side | Usually fine; for speed open UDP 41641 on the router or use a wired connection. |
