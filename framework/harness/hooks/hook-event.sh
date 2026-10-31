#!/usr/bin/env bash
# hook-event.sh -- SOURCED helper, never registered as a hook itself.
#
# Each hook sources this, then relies on the EXIT trap it installs to record
# one structured evidence line to ~/.claude/hooks/logs/events.jsonl AFTER the
# hook's own stdin/stdout/exit-code work is completely finished. This can
# never sit in the stdin/stdout path of a hook (unlike an external wrapper
# around the hook's invocation), so a SessionStart context injection or a
# secret-firewall deny JSON is byte-for-byte untouched -- the log write goes
# only to the log file, never to stdout/stderr.
#
# Usage, near the top of a bash hook (after any `set -u` etc, before doing
# real work that might exit early):
#
#   HOOK_NAME="my-hook"
#   source "$HOME/.claude/hooks/hook-event.sh"
#   # ... hook reads stdin into $HOOK_STDIN itself if it needs the payload ...
#   # ... at each meaningful branch, set the semantic result: ...
#   HOOK_OUTCOME="injected"
#
# The EXIT trap fires exactly once, whatever exit path the hook takes
# (explicit `exit N` or falling off the end), and logs metadata only: never
# hook payloads (tool inputs, injected context text, transcript content).
#
# HOOK_OUTCOME defaults to "unset" if a script never assigns it -- that is
# itself useful auditor signal (log presence is not proof of effect; a
# missing outcome means nobody instrumented that branch yet).

HOOK_EVENT_LOG="${HOOK_EVENT_LOG:-$HOME/.claude/hooks/logs/events.jsonl}"
HOOK_EVENT_START_MS=$(date +%s%3N)
HOOK_OUTCOME="${HOOK_OUTCOME:-unset}"

_hook_event_emit() {
  local exit_code=$? now_ms elapsed_ms ts_utc sid ev
  now_ms=$(date +%s%3N)
  elapsed_ms=$(( now_ms - HOOK_EVENT_START_MS ))
  ts_utc=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
  sid="${HOOK_SESSION_ID:-unknown}"
  ev="${HOOK_EVENT_NAME:-unknown}"
  mkdir -p "$(dirname "$HOOK_EVENT_LOG")" 2>/dev/null
  printf '{"ts_utc":"%s","session_id":"%s","hook":"%s","event":"%s","exit":%s,"ms":%s,"outcome":"%s"}\n' \
    "$ts_utc" "$sid" "${HOOK_NAME:-unknown}" "$ev" "$exit_code" "$elapsed_ms" "$HOOK_OUTCOME" \
    >> "$HOOK_EVENT_LOG" 2>/dev/null
  return "$exit_code"
}
trap _hook_event_emit EXIT
