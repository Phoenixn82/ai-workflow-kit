# raven: the viewer

A light laptop, an old tablet or a Chromebook that only connects to the others. It stores nothing important and
runs nothing heavy: the agents work on odin, models on thor, jobs on heimdall, and raven is the screen and keyboard
you carry. If it is lost or broken, nothing is lost.

Named after the maintainer's thin client. It is the **raven** profile ([profile.json](profile.json)); any OS,
no hardware minimum.

## Install

From the kit's root folder (your clone; every command on this page runs from there):

```sh
install/install.sh --profile raven --dry-run
install/install.sh --profile raven
```

Tools: git, python3, OpenSSH, Tailscale, the `hosts.env` address book, tmux, mosh (optional), Node.js (Claude Code
installs through npm on Linux), Claude Code, herdr (optional) and `thor-up` (optional). That is all: no agents'
hooks, no vault, no services, no timers. A phone or tablet without a terminal needs only the Tailscale app and, for
Remote Control, the Claude app.

## Connect

1. **Tailscale**: sign in with the same account as the other boxes. Every machine is now reachable by name from
   anywhere, with nothing opened on your router ([servers/tailscale.md](../../servers/tailscale.md)).
2. **SSH config**: copy the blocks you need from [ssh_config.example](ssh_config.example) into `~/.ssh/config`
   (your login name, keep-alives, one shared connection per box, a port forward for a loopback-only web UI).
   Tailscale SSH answers on the Linux and macOS boxes, so there are no keys to copy.
3. **A session that survives**: `ssh odin -t tmux new -A -s main` attaches to (or starts) a tmux session on odin;
   close the lid, reconnect later, everything is still running. On a flaky link use mosh instead of ssh:
   `mosh odin -- tmux new -A -s main`. herdr instead of tmux shows each agent's state per pane.
4. **hosts.env**: `THOR_HOST` and `THOR_MAC` in `~/.config/ai-workflow-kit/hosts.env`, so `thor-up` can wake thor
   before you `ssh thor` ([examples/hosts.env.example](../../examples/hosts.env.example)).

## Drive the agents from here

- **Claude Code Remote Control**: run the agent on odin and drive it from raven's browser or the Claude app, with
  odin's files, tools and hooks. On odin, `claude --remote-control` starts an interactive session you can pick up
  remotely, or keep a Remote Control server running as a user unit
  ([docs](https://code.claude.com/docs/en/remote-control)):

```ini
# odin: ~/.config/systemd/user/claude-remote-control.service, then: systemctl --user enable --now claude-remote-control
[Unit]
Description=Claude Code Remote Control (sessions on this machine, from the phone or another computer)
After=network-online.target
[Service]
WorkingDirectory=%h/AI_Projects
ExecStart=%h/.local/bin/claude remote-control --name odin
Restart=always
RestartSec=15
[Install]
WantedBy=default.target
```

- **CAPTCHAs and 2FA go to your phone.** When a web job on odin hits a checkbox, a CAPTCHA or a login code,
  `web handoff` sends you a link and you tap it; the agent never solves one ([docs/web.md](../../docs/web.md)).
  raven only watches.
- **Usage at a glance**: Claude Code's `/usage` and Codex's `/status` show how much of your plan is left. A
  status-bar widget that reads them is a nice extra on a laptop you keep open; it is not part of the kit.

## Keep it light

Nothing heavy runs here: no local models, no Docker, no vault, no scheduled jobs. If you catch yourself
installing one, it belongs on odin, thor or heimdall. Keep raven's disk free of copies of your work (open files
over SSH or Remote Control instead), so losing it costs nothing but the device.

## On a laptop: power profile follows the charger (optional, Linux)

A raven laptop you carry runs best at full speed on the charger and saver mode on battery.
[servers/tools/ac-power-profile](../../servers/tools/ac-power-profile/README.md) switches the power profile on every
plug and unplug, and stands aside while host-heat is cooling the machine:

```sh
sudo servers/tools/ac-power-profile/install.sh          # undo: --undo
ac-power-profile --dry-run                              # "would set performance" on the charger
```

It needs power-profiles-daemon (`powerprofilesctl`). On a thin laptop that gets warm on the charger, add the heat
watch from odin (`framework/_tools/host-heat/install.sh raven`, over ssh), or change `performance` to `balanced`
in the script. Not the server setup script: its base lane keeps a laptop awake with the lid closed.

## Verify

```sh
tailscale status                     # every box listed
ssh heimdall true && echo ok         # no password prompt
thor-up --dry-run                    # the wake plan for thor (needs THOR_HOST and THOR_MAC)
```
