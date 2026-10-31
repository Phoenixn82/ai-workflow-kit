# fleet-probes (audit 2026-10-12, Batch H feedback loops)

Source lives here on odin; `install.sh` copies it out. Both probes send ONE **silent** Telegram per state change.

| Probe | Host | Timer | Checks | Alert path |
|---|---|---|---|---|
| `freellmapi-probe` | heimdall | `*:07/30` | real completion via `auto:quick` (groq/nvidia/ollama-cloud only; a `custom/` = thor-local route counts as down), one retry after 30 s | `/usr/local/bin/heimdall-alert` |
| `disk-probe` | thor | `00/6:20`, Persistent | `/` and `/srv/data` Use% >= 90, or unmounted | thor `~/telegram-bot.token`, `disable_notification` |

`freellmapi-probe` skips while `fleet-wake status` says `thor: hold|hold1`, and re-checks after a failed retry (thor-sleep
sets the hold ~2 s before suspending, possibly mid-probe). Kill switch: `touch ~/.local/state/<probe>/HALT` on that
host. They also check `~/AI_Projects/_system/automations/HALT`, but neither heimdall nor thor has that directory (same
as fleet-wake), so odin's global HALT does NOT stop them. Both have `--self-test` and `--dry-run` (prints the alert, keeps state).
disk-probe runs on thor because heimdall's thor keys are forced commands (`thor-heat`, example-app reader, rrsync).
The router URL is `FREELLMAPI_URL` (the `/v1` base) in heimdall's `~/.config/ai-workflow-kit/hosts.env` (or the
environment; the unit loads the file with `EnvironmentFile=-`); without it the probe exits 1 with "set FREELLMAPI_URL".

## Apply (orchestrator)
1. `~/AI_Projects/_tools/fleet-probes/install.sh --dry-run` (review), then `install.sh` (copies scripts + units,
   writes the router key to heimdall `~/.config/freellmapi-probe/key` and the chat id to thor
   `~/.config/disk-probe/chat-id`, both 0600, never printed; daemon-reloads). It does not enable anything.
2. Enable (the user's call):
   `ssh heimdall systemctl --user enable --now freellmapi-probe.timer`
   `ssh thor systemctl --user enable --now disk-probe.timer`
   If a disk is over the threshold, the first real disk-probe run sends a line like "thor disk: / is 9x % full".
3. Rollback: `systemctl --user disable --now <probe>.timer`, then remove `~/.local/bin/<probe>`, the units, the
   state dir and the 0600 secret file on that host.

## Notes
- An Uptime Kuma check on `/health` alone misses the cloud leg; freellmapi-probe covers it.
- Side effect to know: thor's ssh key on heimdall is forced to `fleet-wake hold thor`, so any `ssh heimdall ...` from
  thor sets a hold; fleet-wake clears it back to `up` a few minutes later.
