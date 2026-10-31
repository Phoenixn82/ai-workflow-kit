#!/usr/bin/env bash
# Batch B0 (audit 2026-10-12): a Claude-spawned child run (cx sets CLAUDE_SPAWN=1) must not pay the
# human-session ritual: no vault sync, no boot block, no handoff seal. The parent reads its output file.
[[ "${CLAUDE_SPAWN:-}" == "1" ]] && exit 0
# inject-vault-boot.sh -- SessionStart hook: inject the lean vault boot block.
# FAIL-OPEN: every path exits 0; a failure means "no injection", never a blocked session.

set -u

# Read stdin once (hook JSON) so we can both use it (session_id, for the
# structured-evidence log below) and behave exactly as before otherwise.
stdin_json="$(cat 2>/dev/null || true)"

HOOK_NAME="inject-vault-boot"
HOOK_EVENT_NAME="SessionStart"
HOOK_SESSION_ID="$(printf '%s' "$stdin_json" | jq -r '.session_id // "unknown"' 2>/dev/null || echo unknown)"
HOOK_SOURCE="$(printf '%s' "$stdin_json" | jq -r '.source // "startup"' 2>/dev/null || echo startup)"
# Compaction keeps the session id; re-injecting the boot block there would
# repeat ~1,200 tokens on every compact. The marker below also guards this,
# but the rule is worth stating rather than relying on a side effect.
if [[ "$HOOK_SOURCE" == "compact" ]]; then HOOK_OUTCOME="skipped-compact"; exit 0; fi
# shellcheck source=/dev/null
home_dir="${HOME:-}"
hook_event_helper="$home_dir/.claude/hooks/hook-event.sh"
if [[ -n "$home_dir" && -r "$hook_event_helper" ]]; then
  source "$hook_event_helper" 2>/dev/null || true
fi

# If HOME is somehow unset, the empty default yields a nonexistent path and we fail open.
vault="$home_dir/AI_Projects/_system/second-brain"

ctx=""
count=0
# Batch C1 (audit 2026-10-12): boot.md only. session-state.md pushed the block over the 10,000-char
# hook cap (only a 2 KB preview reached the model) and was 10 days stale. Current state is injected
# per project by the handoff brief; a generated recency summary replaces it in Batch F.
for f in "$vault/boot.md"; do
  [[ -f "$f" ]] || continue
  content="$(cat -- "$f" 2>/dev/null)" || continue
  if (( count > 0 )); then
    ctx+=$'\n\n'
  fi
  ctx+="$content"
  count=$(( count + 1 ))
done

# Nothing to inject: exit quietly.
if (( count == 0 )); then
  HOOK_OUTCOME="empty"
  exit 0
fi

# Keep the vault source stable while still putting the session rituals where
# every injection can see them.
ctx+=$'\n\nRituals: /handoff (optional deep pass; distillation is automatic); HALT: _system/automations/HALT; audit + plan: _system/second-brain/Projects/workflow/audit-2026-10-12/'

# Batch B0/C1 (audit 2026-10-12): the web-status line (real CPU at every session start for 48 bytes) and the
# open-loops hint (the user 2026-10-08: "stop bringing up the open loops") were removed.

# Hook additionalContext over 10,000 chars is truncated to a 2 KB preview by Claude Code; fail loudly instead.
if (( ${#ctx} > 9500 )); then
  ctx="${ctx:0:9000}"$'\n\n[boot block truncated by inject-vault-boot.sh: source exceeded 9,500 chars; shrink boot.md]'
fi

# Build the documented SessionStart additionalContext JSON before claiming the
# per-session marker. If jq is missing or fails, fail open with no output and
# leave the session eligible for a later retry.
if ! output_json="$(jq -cn --arg ctx "$ctx" \
  '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$ctx}}' \
  2>/dev/null)"; then
  HOOK_OUTCOME="fail-open-jq-error"
  exit 0
fi

# Session IDs are normally UUIDs. Refuse unsafe or missing IDs rather than
# turning untrusted input into a state path; no marker means no injection.
if [[ "$HOOK_SESSION_ID" == "unknown" || ! "$HOOK_SESSION_ID" =~ ^[A-Za-z0-9._-]{1,128}$ ]]; then
  HOOK_OUTCOME="fail-open-invalid-session-id"
  exit 0
fi

# mkdir is the atomic test-and-set: exactly one concurrent invocation for a
# session can create its marker and emit context. Old empty marker directories
# are pruned so this state cannot grow forever.
marker_root="$home_dir/.claude/state/vault-boot-injected"
if ! mkdir -p -- "$marker_root" 2>/dev/null; then
  HOOK_OUTCOME="fail-open-marker-root-error"
  exit 0
fi
find "$marker_root" -mindepth 1 -maxdepth 1 -type d -name '*.seen' -mtime +30 \
  -exec rmdir -- {} + 2>/dev/null || true

marker="$marker_root/$HOOK_SESSION_ID.seen"
if ! mkdir -- "$marker" 2>/dev/null; then
  HOOK_OUTCOME="duplicate-skipped"
  exit 0
fi

if printf '%s\n' "$output_json"; then
  HOOK_OUTCOME="injected"
else
  # A failed write did not inject context, so release the claim for a retry.
  rmdir -- "$marker" 2>/dev/null || true
  HOOK_OUTCOME="fail-open-output-error"
fi

exit 0
