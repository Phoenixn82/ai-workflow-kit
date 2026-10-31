#!/usr/bin/env bash
# codex-handoff-watcher.sh
# UserPromptSubmit hook - scans the active project's .codex-claude-handoff dir
# for any *.request.md without a matching *.done.md and injects them as
# additional context for Claude's next turn.
#
# stdout becomes additional context appended to the user's prompt.
# Stays silent (no output) when there's nothing pending - zero noise.
# Fail-open: always exits 0; a broken hook must not break the session.

set -u

# Read the hook payload from stdin (may be empty or malformed - fail open).
raw=$(cat 2>/dev/null || true)

HOOK_NAME="codex-handoff-watcher"
HOOK_EVENT_NAME="UserPromptSubmit"
HOOK_SESSION_ID="unknown"
if [[ -n "$raw" ]] && command -v jq >/dev/null 2>&1; then
    HOOK_SESSION_ID=$(printf '%s' "$raw" | jq -r '.session_id // "unknown"' 2>/dev/null || echo unknown)
fi
# shellcheck source=/dev/null
source "$HOME/.claude/hooks/hook-event.sh"

payload_cwd=""
if [[ -n "$raw" ]] && command -v jq >/dev/null 2>&1; then
    payload_cwd=$(printf '%s' "$raw" | jq -r '.cwd // empty' 2>/dev/null || true)
fi

# Resolve the project root. Prefer the env Claude exposes; fall back to
# the payload's cwd, then the process cwd.
project_root="${CLAUDE_PROJECT_DIR:-}"
if [[ -z "$project_root" ]]; then
    if [[ -n "$payload_cwd" ]]; then
        project_root="$payload_cwd"
    else
        project_root="$PWD"
    fi
fi

if [[ ! -d "$project_root" ]]; then HOOK_OUTCOME="no-project-root"; exit 0; fi

# Walk up from project_root looking for .codex-claude-handoff/ (Codex may have
# written into a parent dir if it considers the parent the project root).
current=$(cd "$project_root" 2>/dev/null && pwd) || exit 0
handoff_dir=""
for _ in 1 2 3 4 5 6; do
    if [[ -d "$current/.codex-claude-handoff" ]]; then
        handoff_dir="$current/.codex-claude-handoff"
        break
    fi
    parent=$(dirname "$current")
    [[ "$parent" == "$current" ]] && break
    current="$parent"
done

if [[ -z "$handoff_dir" ]]; then HOOK_OUTCOME="no-handoff-dir"; exit 0; fi

# Find pending requests: *.request.md files whose matching *.done.md is absent.
pending=()
for req in "$handoff_dir"/*.request.md; do
    [[ -e "$req" ]] || continue
    id=$(basename "$req" .request.md)
    [[ -e "$handoff_dir/$id.done.md" ]] || pending+=("$req")
done

if (( ${#pending[@]} == 0 )); then HOOK_OUTCOME="no-pending"; exit 0; fi

# Emit a single block Claude will see as additional context for this turn.
HOOK_OUTCOME="pending:${#pending[@]}"
echo ""
echo "<codex-handoff-pending>"
echo "Codex has left ${#pending[@]} pending handoff request(s) for you in $handoff_dir."
echo "Read each one, fire the named skill, then write a matching <id>.done.md per the schema in ~/.codex/AGENTS.md so Codex can resume."
echo ""
for req in "${pending[@]}"; do
    echo "## $(basename "$req")"
    # Match PS Write-Output semantics: file bytes, then one appended newline,
    # then the blank separator line (echo "" below).
    if cat "$req" 2>/dev/null; then
        printf '\n'
    else
        echo "(could not read $req)"
    fi
    echo ""
done
echo "</codex-handoff-pending>"
exit 0
