# profiles/: pick the setups that match your machines

A profile is one machine role from the maintainer's own fleet, named after the box that plays it. Take the ones
that fit your machines; one machine can take several, and **telegram** is an add-on for any of them.

| Profile | Role | Page and data |
|---|---|---|
| **odin** | the cockpit: your daily computer, where Claude Code and Codex run | [README](odin/README.md) · [profile.json](odin/profile.json) |
| **loki** | media server: Jellyfin for your own movies, shows and music | [README](loki/README.md) · [profile.json](loki/profile.json) |
| **heimdall** | always-on watchman: jobs, uptime checks, alerts, wake-ups, backups, project deploys | [README](heimdall/README.md) · [profile.json](heimdall/profile.json) |
| **thor** | heavy local AI box: Ollama on the GPU, Whisper, backup target; sleeps when idle | [README](thor/README.md) · [profile.json](thor/profile.json) |
| **raven** | the viewer: a light laptop or tablet that only connects to the others | [README](raven/README.md) · [profile.json](raven/profile.json) |
| **telegram** | add-on: alerts in Telegram, one group per topic, only heat alerts make a sound | [README](telegram/README.md) · [profile.json](telegram/profile.json) |

Each README says what the box runs, how to set it up, the steps only you can do, and how to check it. The
server half of loki, heimdall and thor is `servers/` (old laptops as headless servers, [servers/README.md](../servers/README.md)).
Install one with `install/install.sh --profile <name>[,<name>]` (add `--dry-run` to see the plan first), run from
the kit's root folder.
Tool ids come from [`install/tools.json`](../install/tools.json); its README explains methods and install steps.

```bash
python3 profiles/test_profiles.py              # check every profile against the manifest (PASS or each problem)
python3 profiles/test_profiles.py --self-test  # broken copies of the profiles must each be caught
```

## profile.json

| Field | Type | Meaning |
|---|---|---|
| `name` | string | same as the folder |
| `role`, `for` | string | what the box does, and who the profile is for |
| `addon` | bool | `true` only for telegram: it adds to another profile instead of standing alone |
| `os` | list | the OSes it supports: `arch`, `debian` (Debian and Ubuntu), `macos` |
| `tools` | list | tool ids from `install/tools.json`; must match the `profiles` field of those entries exactly |
| `services` | list | long-running services, each checked by its `health` command after it starts (below) |
| `automations` | list | timers and units to install, each with its kill switch (below) |
| `rules` | list | agent instruction and settings files, merged into yours (below) |
| `requires_hardware` | object | checked before anything is installed (below) |

**services**, `kind: "compose"`: `name`, `tool`, `compose` (the template file in the kit), `service` (its name in
that file), `compose_profiles` (empty = the default profile), `env_example` (copied to `.env` and filled in) or
`env_file` (an env file under `~/` passed with `--env-file`), `project_dir`, `health` (shell command, exit 0 =
healthy; run with `~/.config/ai-workflow-kit/hosts.env` loaded), `optional`, `linux_only`.
`project_dir` maps `linux` (and `macos` unless `linux_only`) to the directory `docker compose` runs in. For the
loki and heimdall stacks the profile's server-lane tool has already copied the compose file and its `.env` there
(`/opt/homeserver/<lane>` on Linux, `~/homeserver/<lane>` on macOS); otherwise the installer copies the template
(and `env_example` as `.env`) in once and never edits a copy that exists. It never runs compose on the template
inside the clone: a second copy of the project would recreate the containers with their data folders inside the
clone. The project name is the file's `name:`, else the directory's name; `test_profiles.py` fails two different
compose files that would share one.
`kind: "systemd"`: `name`, `tool`, `unit` (installed by its tool), `scope` (`user` or `system`), `health`,
`optional`, `linux_only` (always `true`).

**automations**: `name`, `tool`, `unit` (the unit to enable, a file in the kit), `files` (every unit file to
install, that one included), `scope` (`user` = `~/.config/systemd/user`, `system` = `/etc/systemd/system`),
`enable` (`false` = installed but left off until you turn it on), `kill_switch` (how to stop it; also recorded in
your automations registry), `linux_only` (`true` for every systemd unit; skipped on macOS with a message).
A `user` unit on a server only runs without anyone logged in when lingering is on (`loginctl enable-linger
$USER`), so the installer turns lingering on whenever it enables a `user` unit (harmless on a desktop, required
on heimdall and thor).
Every job also stops when `~/AI_Projects/_system/automations/HALT` exists, unless its kill switch says otherwise.

**rules**: `src` (file in the kit), `dest` (under `~/`), `how` (`merge-markdown` or `merge-json`), `tool`.
They are merged into your existing file after a timestamped backup, never replaced, and only with `--harness`.

**requires_hardware**: `ram_gb`, `disk_gb` (minimums), `always_on` (the box should stay powered and wired), and
`any_of`: alternative branches, at least one of which must match, each naming exactly one of
`"gpu": "nvidia"` (with `vram_gb`) or `"cpu": "apple-silicon"` (with `unified_memory_gb`). A machine that matches
no branch stops at the prerequisite check with the reason; it does not install half a profile.
