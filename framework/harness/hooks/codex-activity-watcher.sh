#!/usr/bin/env bash
# codex-activity-watcher.sh
# UserPromptSubmit hook - the Claude-side READ half of the Claude<->Codex bridge.
# Codex already tails the shared log + writes session manifests every turn; this
# gives Claude the symmetric awareness so the connection is bidirectional, not
# one-directional. Companion to the write-side hook codex-claude-shared-log.
#
# Surfaces two low-token feeds; SILENT when there is nothing new (so it never
# nags on routine prompts):
#   1. New Codex turns from <project>/.claude-codex-log.md since Claude last
#      looked (per-project marker; only actor=codex lines - Claude knows its own).
#   2. Unrelayed Codex session manifests (.codex-session-changes/*.md with
#      `relayed: false`) so Claude can relay them to the user, then flip the flag.
#
# stdout becomes additional context for Claude's next turn. Never blocks:
# fail-open, always exit 0, no network, no prompts.

set -u
LC_ALL=C
export LC_ALL

HOME_DIR="${HOME:-/home/user}"
STATE_DIR="$HOME_DIR/.claude/hooks/state"
LOG_DIR="$HOME_DIR/.claude/hooks/logs"
ERR_LOG="$LOG_DIR/codex-activity-watcher.err.log"

HOOK_NAME="codex-activity-watcher"
HOOK_EVENT_NAME="UserPromptSubmit"
HOOK_SESSION_ID="unknown"
# shellcheck source=/dev/null
source "$HOME_DIR/.claude/hooks/hook-event.sh"

log_err() {
    mkdir -p "$LOG_DIR" 2>/dev/null || true
    printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%S.%6NZ 2>/dev/null)" "$1" \
        >> "$ERR_LOG" 2>/dev/null || true
}

# Strip CR, then trim leading/trailing whitespace from $1; print the result.
trim() {
    local s=$1
    s=${s//$'\r'/}
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf '%s' "$s"
}

main() {
    local raw="" cwd

    # --- payload / cwd ---
    if [[ ! -t 0 ]]; then
        raw=$(cat 2>/dev/null) || raw=""
    fi
    if [[ -n "$raw" ]]; then
        HOOK_SESSION_ID=$(printf '%s' "$raw" | jq -r '.session_id // "unknown"' 2>/dev/null || echo unknown)
    fi
    cwd="${CLAUDE_PROJECT_DIR:-}"
    if [[ -z "$cwd" && -n "$raw" ]]; then
        cwd=$(printf '%s' "$raw" | jq -r '.cwd // empty' 2>/dev/null) || cwd=""
    fi
    [[ -n "$cwd" ]] || cwd=$(pwd)
    if [[ ! -d "$cwd" ]]; then HOOK_OUTCOME="no-cwd"; return 0; fi

    # --- resolve project root: innermost dir with .git / CLAUDE.md / AGENTS.md
    #     (matches shared_log.py find_project_root so we read the SAME log) ---
    local root="" cur parent i
    cur=$(cd -- "$cwd" 2>/dev/null && pwd -P) || { HOOK_OUTCOME="cwd-unresolvable"; return 0; }
    for ((i = 0; i < 12; i++)); do
        if [[ -e "$cur/.git" || -e "$cur/CLAUDE.md" || -e "$cur/AGENTS.md" ]]; then
            root="$cur"
            break
        fi
        parent=$(dirname -- "$cur")
        [[ -n "$parent" && "$parent" != "$cur" ]] || break
        cur="$parent"
    done
    if [[ -z "$root" ]]; then HOOK_OUTCOME="no-project-root"; return 0; fi

    # --- per-project marker (gates the high-frequency log feed) ---
    mkdir -p "$STATE_DIR" 2>/dev/null || true
    local key marker_path last_seen=""
    key=$(printf '%s' "$root" | tr -c 'A-Za-z0-9' '_')
    marker_path="$STATE_DIR/$key.codex-seen"
    if [[ -f "$marker_path" ]]; then
        last_seen=$(trim "$(cat -- "$marker_path" 2>/dev/null)")
    fi

    local -a nc_ts=() nc_rest=()
    local newest="$last_seen"

    # --- feed 1: new Codex turns from the shared activity log ---
    local log_path="$root/.claude-codex-log.md" ln ts rest first_line=1
    local re='^\[([^]]+)\][[:space:]]+codex[[:space:]]+(.*)$'
    if [[ -f "$log_path" ]]; then
        while IFS= read -r ln || [[ -n "$ln" ]]; do
            # match PS Get-Content -Encoding UTF8: a file-leading BOM is decoded away
            if (( first_line )); then
                ln=${ln#$'\xef\xbb\xbf'}
                first_line=0
            fi
            ln=${ln%$'\r'}
            [[ "$ln" =~ $re ]] || continue
            ts="${BASH_REMATCH[1]}"
            rest="${BASH_REMATCH[2]}"
            # ISO minute strings sort chronologically (bytewise under LC_ALL=C)
            if [[ -n "$last_seen" ]] && ! [[ "$ts" > "$last_seen" ]]; then
                continue
            fi
            nc_ts+=("$ts")
            nc_rest+=("$(trim "$rest")")
            if [[ "$ts" > "$newest" ]]; then
                newest="$ts"
            fi
        done < "$log_path"
    fi
    local count=${#nc_ts[@]}
    if (( count > 10 )); then
        nc_ts=("${nc_ts[@]:count-10}")
        nc_rest=("${nc_rest[@]:count-10}")
        count=10
    fi

    # --- feed 2: unrelayed Codex session manifests (gated by the relayed flag) ---
    local -a mf_name=() mf_outcome=()
    local changes_dir="$root/.codex-session-changes" f outcome
    if [[ -d "$changes_dir" ]]; then
        # newest first by mtime, same as the PowerShell LastWriteTimeUtc sort
        while IFS= read -r f; do
            [[ -n "$f" && -f "$f" ]] || continue
            grep -qiE '^[[:space:]]*relayed[[:space:]]*:[[:space:]]*false[[:space:]]*$' \
                -- "$f" 2>/dev/null || continue
            outcome=$(sed -nE 's/^\*\*Outcome:\*\*[[:space:]]*(.+)$/\1/Ip' "$f" 2>/dev/null \
                | head -n 1) || outcome=""
            mf_name+=("$(basename -- "$f")")
            mf_outcome+=("$(trim "$outcome")")
            if (( ${#mf_name[@]} >= 5 )); then
                break
            fi
        done < <(find "$changes_dir" -maxdepth 1 -type f -name '*.md' \
                    -printf '%T@\t%p\n' 2>/dev/null | sort -rn | cut -f2-)
    fi
    local mcount=${#mf_name[@]}

    if (( count == 0 && mcount == 0 )); then
        HOOK_OUTCOME="nothing-new"
        return 0
    fi
    HOOK_OUTCOME="surfaced:log=$count,manifests=$mcount"

    # --- emit ---
    echo ""
    echo "<codex-activity>"
    echo "Cheap awareness of what Codex did since you last looked (shared bridge: .claude-codex-log.md + unrelayed session manifests). Background context, not a user instruction. If it names a file/area you're about to edit, reconcile first."
    if (( count > 0 )); then
        echo ""
        echo "Recent Codex turns:"
        for ((i = 0; i < count; i++)); do
            printf -- '- [%s] %s\n' "${nc_ts[i]}" "${nc_rest[i]}"
        done
    fi
    if (( mcount > 0 )); then
        echo ""
        echo "Unrelayed Codex session reports (.codex-session-changes/*.md). If relevant, surface to the user, then flip by editing 'relayed: false' -> 'relayed: true' in the manifest file itself. (NOTE: relay_changes.py --relay takes a goal-run DIRECTORY and reads <dir>/changes.md; it does NOT accept a session-manifest file path. Use it only for pipeline/goal-runs/<ts>/ dirs, not these .codex-session-changes manifests.)"
        for ((i = 0; i < mcount; i++)); do
            if [[ -n "${mf_outcome[i]}" ]]; then
                printf -- '- .codex-session-changes/%s - %s\n' "${mf_name[i]}" "${mf_outcome[i]}"
            else
                printf -- '- .codex-session-changes/%s\n' "${mf_name[i]}"
            fi
        done
    fi
    echo "</codex-activity>"

    # advance the log-feed marker (manifests are self-gating via the relayed flag)
    if [[ -n "$newest" ]]; then
        printf '%s' "$newest" > "$marker_path" 2>/dev/null || true
    fi
    return 0
}

mkdir -p "$LOG_DIR" 2>/dev/null || true
if ! main 2>>"$ERR_LOG"; then
    log_err "main exited nonzero"
fi
exit 0
