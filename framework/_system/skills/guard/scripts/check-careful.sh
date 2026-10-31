#!/usr/bin/env bash
# Pattern-match destructive Bash commands and ask the user.
# Spec: PHASE_5_DISPATCH.md § 2.1

set -uo pipefail

input="$(cat)"

if command -v jq >/dev/null 2>&1; then
  cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // empty')
else
  cmd=$(printf '%s' "$input" | sed -n 's/.*"command"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
fi

emit() {
  local decision="$1" reason="$2"
  if command -v jq >/dev/null 2>&1; then
    jq -n --arg d "$decision" --arg r "$reason" '{permissionDecision: $d, reason: $r}'
  else
    printf '{"permissionDecision":"%s","reason":"%s"}\n' "$decision" "$reason"
  fi
}

# Safe-exception allowlist: `rm -rf <name>` where <name> is a universally
# rebuildable artifact (per careful.md "Safe exceptions"). These pass without
# warning — flagging them would be friction with no payoff.
is_safe_rm() {
  case "$cmd" in
    *"rm -rf "*|*"rm -fr "*|*"rm -r "*|*"rm --recursive "*) : ;;
    *) return 1 ;;
  esac
  local name
  for name in node_modules .next dist __pycache__ .cache build .turbo \
              coverage .venv venv target out .parcel-cache .vite; do
    case "$cmd" in
      *" $name"|*" $name/"|*" $name "|*" ./$name"|*" ./$name/"|*" ./$name ") return 0 ;;
    esac
  done
  return 1
}

match() {
  # Safe rebuildable deletes never warn (checked first so they win over rm -r*).
  if is_safe_rm; then return 1; fi
  case "$cmd" in
    *"rm -rf "*|*"rm -fr "*|*"rm -r "*|*"rm --recursive "*|"rm -rf"|"rm -fr") echo "rm -r (recursive delete)"; return 0 ;;
    *"git push --force"*|*"git push -f "*|*"git push -f"|*" -f"*"refs/"*) echo "git push --force"; return 0 ;;
    *"git reset --hard"*) echo "git reset --hard"; return 0 ;;
    *"git checkout -- "*|*"git checkout ."*|*"git restore ."*|*"git restore --staged ."*) echo "git checkout/restore . (path discard)"; return 0 ;;
    *"git clean -f"*) echo "git clean -f"; return 0 ;;
    *"git branch -D "*) echo "git branch -D (force delete)"; return 0 ;;
    *"git commit --amend"*) echo "git commit --amend (history mutation)"; return 0 ;;
    *"kubectl delete"*) echo "kubectl delete"; return 0 ;;
    *"docker rm -f"*|*"docker system prune"*|*"docker volume prune"*|*"docker image prune"*) echo "docker rm -f / prune"; return 0 ;;
    *"helm uninstall"*) echo "helm uninstall"; return 0 ;;
    *"pg_dump --clean"*) echo "pg_dump --clean (destructive restore)"; return 0 ;;
    *"chmod -R 000"*|*"chmod -R 777"*) echo "chmod -R 000/777"; return 0 ;;
    *"kill -9"*|*"pkill"*) echo "kill -9 / pkill"; return 0 ;;
    *"shutdown"*|*"reboot"*) echo "shutdown / reboot"; return 0 ;;
    *"rmdir /s"*) echo "rmdir /s"; return 0 ;;
    *"Remove-Item -Recurse -Force"*|*"Remove-Item -Force -Recurse"*) echo "Remove-Item -Recurse -Force"; return 0 ;;
  esac
  case "$(printf '%s' "$cmd" | tr '[:lower:]' '[:upper:]')" in
    *"DROP TABLE"*|*"DROP DATABASE"*|*"TRUNCATE TABLE"*|*"TRUNCATE "*) echo "destructive SQL"; return 0 ;;
  esac
  return 1
}

# Opt-in gate: careful mode only acts when the user has explicitly activated it.
# Absent flag = allow (hard rule #10: a default-on hook must never return 'ask').
# 'ask' below is legitimate only because reaching it means careful is opted-in.
FLAG_FILE="${HOME}/.claude/guard-state/careful-active.flag"
if [ ! -f "$FLAG_FILE" ]; then
  emit "allow" ""
  exit 0
fi

pattern=$(match) && {
  # Audit trail: append-only, one line per warning (per careful.md).
  LOG_FILE="${HOME}/.claude/guard-state/careful-log.jsonl"
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "")
  if command -v jq >/dev/null 2>&1; then
    jq -cn --arg ts "$ts" --arg p "$pattern" --arg c "$cmd" \
      '{ts:$ts, decision:"ask", pattern:$p, command:$c}' >> "$LOG_FILE" 2>/dev/null || true
  else
    esc_cmd=$(printf '%s' "$cmd" | sed 's/\\/\\\\/g; s/"/\\"/g')
    printf '{"ts":"%s","decision":"ask","pattern":"%s","command":"%s"}\n' \
      "$ts" "$pattern" "$esc_cmd" >> "$LOG_FILE" 2>/dev/null || true
  fi
  emit "ask" "Destructive command matched (${pattern}). Confirm intent before running."
  exit 0
}

emit "allow" ""
exit 0
