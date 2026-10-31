#!/usr/bin/env bash
# Fail-open Codex PostToolUse bridge. The shared-log implementation is in the
# restricted ~/.claude/skills tree, so this is intentionally a prepared pointer.
set -u

payload="$(cat 2>/dev/null || true)"
tool="$(printf '%s' "$payload" | jq -r '.tool_name // empty' 2>/dev/null || true)"
case "$tool" in Bash|apply_patch|Edit|Write) ;; *) exit 0 ;; esac

# Deferred after permitted access review: invoke shared_log.py append with
# actor=codex and its existing redact_web_cred handling. Never block a tool.
exit 0
