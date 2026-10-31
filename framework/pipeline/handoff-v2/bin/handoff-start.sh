#!/usr/bin/env bash
# Batch B0 (audit 2026-10-12): a Claude-spawned child run (cx sets CLAUDE_SPAWN=1) must not pay the
# human-session ritual: no vault sync, no boot block, no handoff seal. The parent reads its output file.
[[ "${CLAUDE_SPAWN:-}" == "1" ]] && exit 0
cd "$(dirname "$(readlink -f "$0")")/.." || exit 0
# SessionStart: local reconcile, spawn the detached worker, inject the
# pre-built brief. No vault-sync here - a network fetch can hang during an
# outage and this runs before the first prompt.
if [[ "${1:-}" == "--codex" ]]; then
  # Codex only accepts SessionStart context in hookSpecificOutput JSON. Its
  # rollout is sealed on Stop; do not route it through Claude's start reader.
  payload="$(cat 2>/dev/null || true)"
  payload="$(printf '%s' "$payload" | jq 'del(.transcript_path, .rollout_path)' 2>/dev/null)" || exit 0
  output="$(printf '%s' "$payload" | python3 -m handoff.cli start 2>/dev/null)" || exit 0
  [[ -n "$output" ]] || exit 0
  jq -cn --arg ctx "$output" \
    '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$ctx}}' 2>/dev/null || true
  exit 0
fi
exec python3 -m handoff.cli start
