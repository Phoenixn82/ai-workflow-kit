#!/usr/bin/env bash
# Install the credential courier's guardrails against the agents themselves (spec §3.5), for this user, for Claude
# Code and Codex. Run as the user, no sudo (the courier itself is courier/install.sh):
#   1. ~/.claude/hooks/webcred-guard.sh             a copy of courier/webcred-guard.sh (the PreToolUse hook)
#   2. ~/.claude/settings.json                      its PreToolUse entry (guards/claude-settings-entry.json)
#   3. ~/.codex/hooks.json                          its PreToolUse entry (guards/codex-hooks-entry.json)
#   4. ~/.codex/rules/webcred.rules                 Codex execpolicy prefix rules (guards/webcred.rules)
#   5. ~/.claude/hooks/codex-claude-shared-log.sh   nothing after `web cred` / `web login` reaches the shared
#                                                   activity log (guards/shared-log-redaction.patch)
# Idempotent: a second run changes nothing. Every file it changes is backed up first as <file>.bak-webcred-<stamp>.
# What it changed (files created or replaced + their backups, registrations it added, the patch, directories it
# made) goes into ~/.local/share/web/guards-manifest.json, which courier/uninstall-guards.sh undoes exactly.
# It never touches ~/.codex/config.toml. Codex runs a hooks.json hook only once that hook is trusted (Codex keeps
# the trust in config.toml): after the first install, open `codex` and trust webcred-guard under /hooks.
# Exit 1 when a step was skipped with a WARNING.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
G="$HERE/guards"
STAMP=$(date +%Y%m%d-%H%M%S)
HOOK="$HOME/.claude/hooks/webcred-guard.sh"
LOG="$HOME/.claude/hooks/codex-claude-shared-log.sh"
M=(python3 "$G/manifest.py" "$HOME/.local/share/web/guards-manifest.json" "$STAMP")
warn=0

"${M[@]}" file "$HERE/webcred-guard.sh" "$HOOK" 0755
"${M[@]}" register "$HOME/.claude/settings.json" "$G/claude-settings-entry.json" "$HOOK" || warn=1
"${M[@]}" register "$HOME/.codex/hooks.json" "$G/codex-hooks-entry.json" "$HOOK" || warn=1
"${M[@]}" file "$G/webcred.rules" "$HOME/.codex/rules/webcred.rules" 0644

if [[ ! -f "$LOG" ]]; then
    echo "skipped:   $LOG (not installed here: no shared log to redact)"
elif command grep -q 'redact_web_cred' "$LOG"; then
    echo "unchanged: $LOG (redaction present)"
elif patch --dry-run --forward --silent -r - "$LOG" < "$G/shared-log-redaction.patch" >/dev/null; then
    cp -p "$LOG" "$LOG.bak-webcred-$STAMP"
    echo "backup:    $LOG.bak-webcred-$STAMP"
    patch --forward --silent --no-backup-if-mismatch -r - "$LOG" < "$G/shared-log-redaction.patch"
    "${M[@]}" patched "$LOG" "$LOG.bak-webcred-$STAMP"
    echo "patched:   $LOG"
else
    echo "WARNING:   $LOG changed upstream and doesn't take guards/shared-log-redaction.patch; left as it was" >&2
    warn=1
fi

echo "note:      Codex runs the hooks.json guard only once it is trusted: open codex, /hooks, trust webcred-guard"
exit "$warn"
