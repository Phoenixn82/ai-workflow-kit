# odin: the cockpit

Your daily computer: the laptop or desktop where you run Claude Code and Codex. Everything that makes the agents
safer and more useful lives here: hooks that fail closed, a secret firewall, a push gate, lanes and routes for
picking models, one CLI for every web job, a vault the sessions write themselves, search over it on every prompt,
and session handoffs. odin never runs local models or 24/7 jobs (heat, battery): those go to thor and heimdall.

Named after the maintainer's own laptop. It is the **odin** profile ([profile.json](profile.json)); it needs
8 GB RAM and runs on Arch, Debian/Ubuntu and macOS.

## Install

From the kit's root folder (your clone; every command on this page runs from there):

```sh
install/doctor.sh --profile odin                     # what this machine already has
install/install.sh --profile odin --dry-run          # the plan: every tool, its method, what is present
install/install.sh --profile odin                    # tools and CLIs; your agent config is left alone
install/install.sh --profile odin --harness          # also merges the rules, hooks and settings below
```

Without `--harness` nothing under `~/.claude`, `~/.codex` or your instruction files is touched. With it, each
of those files is backed up beside itself (`<file>.bak-<timestamp>`) and merged, never replaced: your own rules
and settings stay, the kit's are added. Fill in `~/.config/ai-workflow-kit/hosts.env` for the machines you have
([examples/hosts.env.example](../../examples/hosts.env.example)); a tool that needs a missing value stops and
names it.

## What you get

**Agents and their guard rails** ([docs/safety.md](../../docs/safety.md))

| Piece | One line |
|---|---|
| claude, codex, gemini | the coding agents (gemini optional, for long-context and media reads) |
| `claude-harness` | the hook dispatcher (one entry point per event; a hook that crashes blocks instead of silently passing), the secret firewall, the model guard, output style, slash commands |
| `codex-harness` | the same discipline for Codex: its hooks and agent-state reporting (optional) |
| secret firewall | blocks a tool call that would read or print a credential; scored by a 182-case replay corpus (`secret-firewall-corpus`) |
| `guard-gate` | global pre-push hook and `gh` wrapper: a public push or a private-to-public flip passes a secret scan (betterleaks, gitleaks), a personal-data denylist and a Codex review first; private repos have no friction |
| `secret-scrub` | finds a leaked password in agent transcripts and local databases and overwrites it in place, never printing it |
| `transcript-prune` | deletes Codex session logs untouched for 21 days (dry run unless `--apply`) |
| `kit-skills` | the kit's skills for both agents: codex-build, codex-review, guard, router, second-brain, web, telegram |
| superpowers, ponytail, frontend-design, impeccable | Claude Code plugins: plan-then-build discipline, the laziest working solution, design passes (installed only with `--harness`, since they write `~/.claude`) |

**Picking a model** ([docs/agents-and-lanes.md](../../docs/agents-and-lanes.md))

| Piece | One line |
|---|---|
| `route` | which lane (model + effort) fits a task class; there is no default lane, every dispatch names its class |
| `cx` | the one way Claude dispatches Codex: explicit model, sandbox, outfile, timeout |
| `hermes`, `hermes-kit`, `helper-wall` | optional [Hermes harness](../../docs/hermes.md): one session across trusted model pools, with scrubbed outside helpers |
| `thor-wake-proxy` | optional loopback proxy that wakes a sleeping router/GPU box; see [Hermes](../../docs/hermes.md) |
| `lane` | free and local model work with data tiers and a quality gate (optional) |
| `lane-intel` | daily job that reads model advice and adjusts `lanes.json` (opt-in, never max effort; timer off) |
| `thor-up` | wakes thor before a job needs it (optional; only if you have a thor) |

**Memory** ([docs/memory.md](../../docs/memory.md))

| Piece | One line |
|---|---|
| `workspace`, `vault` | the `~/AI_Projects` tree the hooks name, and the second-brain folder with its boot block injected at every session start |
| `handoff-v2` | distills every session into the vault at its end (a small fast model, a local model as fallback) and briefs the next start |
| `vault-rag` | hybrid search (keywords + embeddings) over the vault, injected on every prompt; `vault-recall "<question>"` by hand (Linux) |
| `vault-lint` | enforces the vault schema: report, fill, rotate; no model |
| `weekly-review` | Sunday check of the whole workflow by two short `claude -p` runs, the doer not the judge (timer off) |
| `harness-lint` | checks the harness wiring and the lanes table; the weekly review runs it |
| `retention` | archive-first cleanup of old backups and caches: it moves, never deletes (timer off) |
| `open-loops` | find, reconcile and close open loops across sessions (a Workflow, run on demand) |

**Web and desktop** ([docs/web.md](../../docs/web.md), [docs/mcp-and-plugins.md](../../docs/mcp-and-plugins.md))

| Piece | One line |
|---|---|
| `web` | one CLI for every web job: read, crawl, search, act in a logged-in browser, and hand a CAPTCHA or 2FA to your phone |
| chromium, agent-browser, scrapling | what `web` drives |
| `jev-browse` | fast browser agent: a small judge model picks each step, `web act` runs it |
| chrome-devtools-mcp, firefox-devtools-mcp | debug your own web apps; a separate agent browser profile |
| computer-use-linux, `desk-control` | native desktop apps through the accessibility tree; a visible, stoppable click loop for pixel-only surfaces (Linux; desk-control needs Hyprland) |
| `motion`, yt-dlp, ffmpeg | motion graphics from code, video and audio tooling |
| `x-scroll` | one slow read-only scroll of an X account per day into a vault digest (timer off) |
| `review-queue` | the last 25 items flagged for review, from the desktop bar (`rq`; Arch/Omarchy) |

**Plumbing**: git, python3, uv, node, mise, jq, ripgrep, openssh, gh, Tailscale, herdr (a multiplexer that shows
each agent's state per pane), Obsidian (to read the vault yourself), restic (backups to thor), `agents-slice`
(`claude-slice` and `heavy`: a CPU and memory ceiling for agent fan-outs and heavy tools, Linux), the `hosts.env`
address book.

**Rules merged with `--harness`**: `framework/harness/CLAUDE.global.md` into `~/.claude/CLAUDE.md`,
`framework/harness/settings.reference.json` into `~/.claude/settings.json`, `framework/CLAUDE.md` and
`framework/AGENTS.md` into `~/AI_Projects/`, and Codex's `AGENTS.md` and `hooks.json` into `~/.codex/`. Read them
first; they are the maintainer's rules, written to be adapted.

## Timers

All Linux user timers, each with its kill switch; the global one is `~/AI_Projects/_system/automations/HALT`.

| Timer | Default | Turn on |
|---|---|---|
| `vault-rag-reindex` | off: it needs `EMBED_URL` (an Ollama with `nomic-embed-text`: heimdall's embed server, thor, or this machine). Without it the index refuses to build ("set EMBED_URL") and prompt-time search has nothing to read | set `EMBED_URL` in `hosts.env`, build the first index by hand (`~/AI_Projects/_tools/vault-rag/reindex.sh`, up to a couple of hours for a big vault), then `systemctl --user enable --now vault-rag-reindex.timer` |
| `weekly-review` | off | `systemctl --user enable --now weekly-review.timer` |
| `retain` | off | `systemctl --user enable --now retain.timer` |
| `lane-intel` | off | `systemctl --user enable --now lane-intel.timer` |
| `x-scroll` | off | `systemctl --user enable --now x-scroll.timer` |

## On a small laptop

Everything above runs on 8 GB, but you can trim. Safe to skip (all optional in the profile): gemini, herdr,
mise, Obsidian, motion, yt-dlp and ffmpeg, jev-browse, desk-control, computer-use-linux, firefox-devtools-mcp,
x-scroll, lane-intel, lane, review-queue, transcript-prune, retention. Keep the guard rails (harness, secret
firewall, guard gate) and the memory pieces: they cost almost nothing at rest. Put the embed server on heimdall,
not here, and never run local models on a thin laptop: use thor. On Linux, start Claude through `claude-slice`
so a big fan-out of subagents cannot pin every core.

## Idle sleep, idle reboot and wake (optional, Linux)

odin can sleep when nothing has happened for an hour and be woken by heimdall the moment you or a job needs it,
the same way thor does. It can also do a pending reboot (after a kernel update) only once it is idle, so a reboot
never lands in the middle of an agent's work. The maintainer runs the idle reboot for real and the idle sleep in
dry-run mode (it logs "would suspend now" instead): turn the sleep on only after the wake path below works for you.

**The idle check: autosuspend.** [autosuspend](https://github.com/languitar/autosuspend) (GPL-2.0; Arch: the
`autosuspend` AUR package, the maintainer runs 11.1.0; Debian and Ubuntu: `sudo apt install autosuspend`) runs a
list of checks every minute; any check that reports activity restarts the hour. Turn logind's own idle action off so
the two do not fight, in `/etc/systemd/logind.conf.d/90-idle-suspend.conf`:

```ini
[Login]
IdleAction=ignore
```

`/etc/autosuspend.conf` (replace `you`, your uid and the interface names; `ip -br link` lists them):

```ini
[general]
interval = 60
idle_time = 3600
# Dry run first. Later: suspend_cmd = /usr/local/bin/odin-idle-action
suspend_cmd = logger -t odin-sleep 'DRY RUN: would suspend now'
# no timed wake-ups: odin wakes only when something asks
wakeup_cmd = true

# agents mid-task in herdr
[check.HerdrAgents]
class = ExternalCommand
enabled = true
command = runuser -u you -- herdr agent list 2>/dev/null | grep -q '"agent_status":"working"'

# Claude or Codex working anywhere (headless, tmux, Remote Control), by CPU use
[check.AgentCPU]
class = ExternalCommand
enabled = true
command = /usr/local/bin/odin-agents-busy

# a model call in flight to thor (Ollama 11434, FreeLLMAPI 3001)
[check.ModelCalls]
class = ExternalCommand
enabled = true
command = ss -Htn state established '( dport = :11434 or dport = :3001 )' | grep -q .

# builds, renders, installs (5-minute load average; scale it to your core count)
[check.Load]
class = Load
enabled = true
threshold = 3.0

# any machine talking to odin over the tailnet: ssh, herdr, streaming, forwards
[check.Tailnet]
class = NetworkBandwidth
enabled = true
interfaces = tailscale0
threshold_send = 3000
threshold_receive = 3000

# downloads and uploads over the LAN or Wi-Fi
[check.Internet]
class = NetworkBandwidth
enabled = true
interfaces = eno1,wlan0
threshold_send = 100000
threshold_receive = 100000

# keyboard or mouse in the last minute (the hypridle listener below makes the flag)
[check.LocalInput]
class = ExternalCommand
enabled = true
command = test ! -e /run/user/1000/odin-input-idle

# anything holding a sleep or idle block (systemd-inhibit around a long job)
[check.Inhibitors]
class = ExternalCommand
enabled = true
command = systemd-inhibit --list --no-legend --no-pager | awk '$$NF=="block" && /sleep|idle/{f=1} END{exit !f}'
```

Then `sudo systemctl enable --now autosuspend`. Your own long jobs (a nightly report, a backup) can get one more
`ExternalCommand` check that exits 0 while their unit is `activating`. Keyboard input on Hyprland, in `~/.config/hypr/hypridle.conf` (other desktops: any
idle tool that creates and removes a file):

```
listener {
    timeout = 60
    on-timeout = touch "$XDG_RUNTIME_DIR/odin-input-idle"
    on-resume = rm -f "$XDG_RUNTIME_DIR/odin-input-idle"
}
```

`/usr/local/bin/odin-agents-busy` (mode 755) says "busy" when a Claude or Codex process used 1.5 s of CPU since
the last check; an idle agent pane uses close to none:

```python
#!/usr/bin/env python3
"""autosuspend check: exit 0 if a claude or codex process used >= 1.5 s CPU since the last run."""
import json, os, sys
STATE, HZ = "/run/odin-agents-cpu.json", os.sysconf("SC_CLK_TCK")
new = {}
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        if open(f"/proc/{pid}/comm").read().startswith(("claude", "codex")):
            f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
            new[pid] = (int(f[11]) + int(f[12])) / HZ   # utime + stime
    except (OSError, IndexError, ValueError):
        pass
try:
    old = json.load(open(STATE))
except (OSError, ValueError):
    old = {}
json.dump(new, open(STATE, "w"))
sys.exit(0 if any(t - old.get(p, t) >= 1.5 for p, t in new.items()) else 1)   # a new process counts next round
```

**What happens after an idle hour: `/usr/local/bin/odin-idle-action`** (mode 755). A pending reboot wins, then the
sleep. The flag is yours to set (`sudo touch /var/lib/odin-reboot-pending` after an update that wants a reboot);
it waits for the package manager to finish:

```bash
#!/bin/bash
# autosuspend's suspend_cmd, after an idle hour
FLAG=/var/lib/odin-reboot-pending
if [ -e "$FLAG" ] && [ ! -e /var/lib/pacman/db.lck ] && ! pgrep -x 'pacman|apt|dpkg' >/dev/null; then
  rm -f "$FLAG"
  logger -t odin-sleep "idle 1 h with reboot pending: rebooting"
  exec systemctl reboot
fi
# tell heimdall the sleep is on purpose, or fleet-wake wakes odin and alerts after three missed checks
runuser -u you -- ssh -o BatchMode=yes -o ConnectTimeout=5 heimdall /usr/local/bin/fleet-wake hold odin \
  || logger -t odin-sleep "fleet-wake hold failed; heimdall may wake odin early"
exec systemctl suspend
```

Keep the dry-run `suspend_cmd` until the wake works; to try only the idle reboot first, end the script with
`exec logger -t odin-sleep 'DRY RUN: would suspend now'` instead of the hold and the suspend. After a reboot an
encrypted odin waits at its disk prompt, and [luks-unlock](../../servers/tools/luks-unlock/README.md) lets heimdall
type it.

**Wake: Wake-on-LAN through heimdall.** Turn Wake-on-LAN on in the BIOS, and keep it armed on the wired port across
reboots with `/etc/systemd/network/50-odin-wol.link` (your port's MAC address; the other lines keep the default
naming):

```ini
[Match]
MACAddress=aa:bb:cc:dd:ee:ff

[Link]
NamePolicy=keep kernel database onboard slot path
AlternativeNamesPolicy=database onboard slot path mac
MACAddressPolicy=persistent
WakeOnLan=magic
```

On heimdall, `ODIN_HOST` and `ODIN_MAC` in `hosts.env` make fleet-wake watch odin: it honours the hold above, and
wakes odin once with an alert when it drops off without one
([profiles/heimdall](../heimdall/README.md#waking-boxes-fleet-wake-and-wake-watch)). A deliberate shutdown sets the
hold too: install `framework/_tools/fleet-wake/fleet-wake-shutdown@.service` to `/etc/systemd/system/` (set
`User=` to you), then `sudo systemctl enable --now fleet-wake-shutdown@odin.service`.

From raven or another machine, wake odin before you use it. In raven's `~/.ssh/config`, an interactive `ssh odin`
wakes it first (services and tunnels do not, so a background poll never keeps odin awake):

```
Match originalhost odin exec "test -t 2 && ~/.local/bin/odin-up"
```

and `~/.local/bin/odin-up` (mode 755):

```bash
#!/bin/bash
# exit 0 once odin answers on port 22; wakes it through heimdall when it does not
up() { timeout 2 bash -c '</dev/tcp/odin/22' 2>/dev/null; }
up && exit 0
echo "odin is asleep, waking it..." >&2
ssh -o BatchMode=yes -o ConnectTimeout=5 heimdall /usr/local/bin/fleet-wake wake odin >/dev/null 2>&1
for _ in $(seq 45); do up && exit 0; sleep 2; done
echo "odin did not wake within 90 s: check its power and Ethernet" >&2; exit 1
```

**Check it**

```sh
systemctl is-active autosuspend                 # active
journalctl -u autosuspend -n 20 --no-pager      # which check kept odin awake, each minute
journalctl -t odin-sleep -n 5 --no-pager        # "DRY RUN: would suspend now" after an idle hour
sudo ethtool <wired port> | grep Wake-on        # "Wake-on: g"
ssh heimdall fleet-wake status                  # odin: up (hold while it sleeps on purpose)
```

**Undo:** `sudo systemctl disable --now autosuspend`; delete the logind drop-in to get the old idle action back.
Kill switch for the reboot: remove `/var/lib/odin-reboot-pending`.

## Verify

```sh
install/doctor.sh --profile odin           # one line per tool: ok / missing / wrong-version
route list                                 # the task classes and their lanes
thor-up --self-test                        # offline
vault-recall "what did I decide about backups"   # once EMBED_URL is set and the index is built
python3 profiles/test_profiles.py          # the profile data is consistent
```
