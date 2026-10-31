#!/usr/bin/env bash
# Block Edit/Write outside the active freeze scope.
# Spec: PHASE_5_DISPATCH.md § 2.2

set -uo pipefail

STATE_FILE="${HOME}/.claude/guard-state/freeze-dir.txt"

input="$(cat)"

if command -v jq >/dev/null 2>&1; then
  file_path=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')
else
  file_path=$(printf '%s' "$input" | sed -n 's/.*"file_path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
fi

emit() {
  local decision="$1" reason="$2"
  if command -v jq >/dev/null 2>&1; then
    jq -n --arg d "$decision" --arg r "$reason" '{permissionDecision: $d, reason: $r}'
  else
    printf '{"permissionDecision":"%s","reason":"%s"}\n' "$decision" "$reason"
  fi
}

if [ ! -f "$STATE_FILE" ]; then
  emit "allow" ""
  exit 0
fi

scope=$(head -n 1 "$STATE_FILE" | tr -d '\r' | sed 's:[[:space:]]*$::')

if [ -z "$scope" ]; then
  emit "allow" ""
  exit 0
fi

in_scope=$(python -c "
import os, sys
scope = os.path.normpath(os.path.abspath(sys.argv[1]))
target = os.path.normpath(os.path.abspath(sys.argv[2]))
try:
    common = os.path.commonpath([scope, target])
    print('1' if os.path.normcase(common) == os.path.normcase(scope) else '0')
    print(scope)
except ValueError:
    print('0')
    print(scope)
" "$scope" "$file_path" 2>/dev/null)

verdict=$(echo "$in_scope" | sed -n '1p')
scope_resolved=$(echo "$in_scope" | sed -n '2p')

if [ "$verdict" = "1" ]; then
  emit "allow" ""
  exit 0
fi

emit "deny" "freeze active: edits restricted to ${scope_resolved}"
exit 0
