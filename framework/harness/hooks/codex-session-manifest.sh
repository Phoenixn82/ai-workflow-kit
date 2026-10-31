#!/usr/bin/env bash
# Fail-open Codex Stop hook; writes the Claude-bridge manifest only.
set -u

payload="$(cat 2>/dev/null || true)"
cwd="$(printf '%s' "$payload" | jq -r '.cwd // empty' 2>/dev/null || true)"
session_id="$(printf '%s' "$payload" | jq -r '.session_id // "unknown"' 2>/dev/null || true)"
[[ "$cwd" == /* && -d "$cwd" && "$cwd" != / ]] || exit 0
[[ "$session_id" =~ ^[A-Za-z0-9._-]{1,128}$ ]] || session_id="unknown"

project="$(realpath -e -- "$cwd" 2>/dev/null || true)"
[[ -n "$project" && -d "$project" ]] || exit 0
dir="$project/.codex-session-changes"
mkdir -p -- "$dir" 2>/dev/null || exit 0
stamp="$(date -u +%Y%m%dT%H%M%S%NZ)"
out="$dir/$stamp-$session_id.md"
[[ -e "$out" ]] && exit 0
tmp="$(mktemp "$dir/.${stamp}.XXXXXX" 2>/dev/null || true)"
[[ -n "$tmp" ]] || exit 0
{
  printf '%s\n' '---'
  printf '%s\n' 'agent: codex'
  printf 'session_id: %s\n' "$session_id"
  printf '%s\n' 'relayed: false'
  printf '%s\n' '---' '' 'Outcome: Codex session ended; handoff-v2 seal was requested.'
} >"$tmp" 2>/dev/null || { rm -f -- "$tmp"; exit 0; }
mv -n -- "$tmp" "$out" 2>/dev/null || true
[[ ! -e "$tmp" ]] || rm -f -- "$tmp"
exit 0
