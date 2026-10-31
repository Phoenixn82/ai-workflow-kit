#!/usr/bin/env bash
# run.sh: the daily lane-intel pass (lane-intel.timer): theo_watch -> x_watch -> slopalytics -> stance -> propose.
# Each step is isolated (a failure logs and the next step runs); at most ONE Telegram line per run, only when something
# new was found or a step failed.  run.sh --dry-run (no vault/state/lanes writes, no Telegram) | run.sh --self-test
set -u
cd "$(dirname "$(readlink -f "$0")")"
if [ "${1:-}" = --self-test ]; then
  for s in whisper_thor theo_watch summarize x_watch slopalytics stance propose backfill_claims apply_lanes; do
    python3 "$s.py" --self-test >/dev/null || { echo "FAIL $s"; exit 1; }
  done
  bash -n install.sh && echo "self-test passed (all scripts)"; exit 0
fi
[ -e $HOME/AI_Projects/_system/automations/HALT ] && { echo "HALT present"; exit 0; }
python3 $HOME/AI_Projects/_system/automations/preflight.py >/dev/null || { echo "preflight blocked (budget ceiling)"; exit 0; }
DRY=${1:-}
export LANE_INTEL_NOTIFY_FILE; LANE_INTEL_NOTIFY_FILE=$(mktemp)
trap 'rm -f "$LANE_INTEL_NOTIFY_FILE"' EXIT
fails=()
for step in theo_watch x_watch slopalytics stance propose; do
  echo "== $step $(date +%T)"
  timeout -s KILL 3600 nice -n 10 python3 "$step.py" $DRY || { fails+=("$step"); logger -t lane-intel "$step failed"; }
done
msg=$(paste -sd '|' "$LANE_INTEL_NOTIFY_FILE" | sed 's/|/; /g')
[ ${#fails[@]} -gt 0 ] && msg="${msg:+$msg; }failed: ${fails[*]} (journalctl --user -u lane-intel)"
echo "== done. notify: ${msg:-nothing}"
if [ -n "$msg" ] && [ -z "$DRY" ]; then
  timeout -s KILL 60 tg lane-intel "lane-intel: ${msg:0:900}" || logger -t lane-intel "telegram send failed"
fi
exit 0
