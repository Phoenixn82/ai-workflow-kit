# agents-slice: put agent sessions and heavy tools under the agents.slice CPU ceiling (audit 2026-10-12, item A6)

`~/.config/systemd/user/agents.slice` (CPUQuota=1200%, MemoryHigh=24G) is the one persistent ceiling for agent work.
Nothing runs in it unless something puts it there; these two wrappers do.

| file | linked as | does |
|---|---|---|
| `claude-slice` | `~/.local/bin/claude-slice` | `systemd-run --user --scope --slice=agents.slice --unit=claude-<pid> --collect --quiet -- claude "$@"` |
| `heavy` | `~/.local/bin/heavy` | same, plus `-p CPUQuota=400% --nice=10` (the caller's nice if higher), for yt-dlp / ffmpeg / whisper / gitleaks / renders |

`--scope` means systemd-run registers the scope and then execs the command in place: same PID, same TTY, same cwd and env,
and the command's own exit code. Both scripts take `--dry-run` (print the command) and `--self-test`.
If there is no user systemd bus (bare TTY or ssh with no user manager), `claude-slice` warns and runs bare claude.

## aiproj (applied to ~/.bashrc; backup at ~/.bashrc.pre-A6)
```diff
-alias aiproj='cd "$AI_PROJECTS_ROOT" && claude'
+# Runs inside agents.slice (1200% CPU ceiling) via claude-slice; `aiproj --plain` runs bare claude.
+unalias aiproj 2>/dev/null  # ponytail: re-sourcing over the old alias would break the function definition
+aiproj() {
+  cd "$AI_PROJECTS_ROOT" || return
+  if [ "${1:-}" = --plain ]; then shift; claude "$@"; else claude-slice "$@"; fi
+}
```
Open shells keep the old alias until `source ~/.bashrc` or a new terminal.

## How a fan-out from plain `claude` escapes the slice, and why aiproj prevents it
A cgroup is inherited at fork. Subagents and Workflow agents run inside the claude process itself, and every Bash tool call,
hook, MCP server and `codex`/`cx` started from the session is a child of that process. So the whole fan-out lives wherever the
top-level claude was started. A plain `claude` typed in a herdr pane starts in the login scope
(`user-<uid>.slice/session-<n>.scope`), which has no quota now that the hand-set runtime quota is gone. The fan-out then has no ceiling at all.
(Before, it shared a runtime quota on the login scope with the user's other sessions, which throttled them all.)
`aiproj` now starts claude through `claude-slice`, so the top-level process, and everything it forks, sits in
`agents.slice/claude-<pid>.scope`. Verified with a real headless session: the claude PID, its `rg` child, a hook child and a
Bash tool child all showed that cgroup.

Escapes that remain:
1. `systemd-run --user` started by an agent without `--slice=agents.slice` lands in `user@<uid>.service/app.slice`, outside
   any ceiling, and such scopes pile up quickly. Rule: heavy one-shot tools go through `heavy <cmd>`, or `--slice=agents.slice` is passed by hand.
2. `aiproj --plain`, or `claude` typed directly. That is on purpose, as the escape hatch.
3. Codex sessions and timer-driven agents started outside a claude-slice session. These are not wired by this item.
4. `claude-remote-control.service` (phone/browser sessions) runs in `app.slice`, and so do the `claude --print --sdk-url` sessions it spawns.
   Staged, not applied (the restart drops live phone sessions):
   `mkdir -p ~/.config/systemd/user/claude-remote-control.service.d && printf '[Service]\nSlice=agents.slice\n' > ~/.config/systemd/user/claude-remote-control.service.d/slice.conf && systemctl --user daemon-reload && systemctl --user restart claude-remote-control.service`
5. A session typed as plain `claude` in a terminal pane sits in the login scope. None of those goes through aiproj,
   so until launches move to `aiproj`, the slice holds nothing live, and with the login-scope quota gone those fan-outs have no CPU ceiling.

## Sizing notes
- All aiproj sessions share one 1200% budget, because the ceiling is on the slice and not on each scope. The user's own TUI is inside it.
  Size it so a few threads stay free for the desktop. Agents mostly wait on the network, so a fan-out of about 20 live
  agents needs only a few cores, and 1200% suits a machine with 16 threads. If it throttles, `aiproj --plain` gets out.
- On balanced or performance power profiles, 300-400% is a sensible size: `systemctl --user set-property agents.slice CPUQuota=400%`.
  This writes a persistent drop-in. Revert it with `systemctl --user revert agents.slice`.
- A `heavy` scope is capped at 400% and nested in the slice, so it also counts against the 1200%.

## Runtime quota on the login scope: removed
`sudo -n systemctl set-property --runtime session-<n>.scope CPUQuota=` (empty value) clears it: `cpu.max` then reads `max 100000`.
It was runtime-only and would also have gone at logout. Per PLAN A6: never put a quota on the whole login scope again.

## Verify
    claude-slice --self-test && heavy --self-test
    systemd-cgls --no-pager --user-unit agents.slice          # while an aiproj session runs
    cat /proc/$(pgrep -n claude)/cgroup                        # expect .../agents.slice/claude-<pid>.scope

## Optional (not applied)
agents.slice `Documentation=` still points at the vault NOTES.md, which does not mention it. To point it here:
`sed -i 's|^Documentation=.*|Documentation=file://$HOME/AI_Projects/_tools/agents-slice/README.md|' ~/.config/systemd/user/agents.slice && systemctl --user daemon-reload`

## Rollback
`cp ~/.bashrc.pre-A6 ~/.bashrc; rm ~/.local/bin/claude-slice ~/.local/bin/heavy`. Live scopes end with their processes.
