#!/usr/bin/env bash
# Claude PostToolUse hook for the shared Claude/Codex activity log.
# Silent by design: never prints context, never blocks Claude.
# Bash port of codex-claude-shared-log.ps1. Fail-open: always exits 0.
# Log format and 100-line rotation live in shared_log.py (unchanged); this
# hook only decides whether/what to append.

set -u

EMITTER="$HOME/.claude/skills/codex-goal-dispatcher/scripts/shared_log.py"
ERR_LOG_DIR="$HOME/.claude/hooks/logs"
ERR_LOG="$ERR_LOG_DIR/codex-claude-shared-log.err.log"

# Structured evidence (linux-relaunch T7 item 11). Coarse by design: the core
# logic runs in a `run_hook()` subshell specifically to keep `set -e` live
# (see the NOTE near its call site below) -- variables assigned inside a real
# `(...)` subshell never escape it, so per-branch outcomes can't reach here
# without breaking that errexit isolation. This only distinguishes
# ok-including-every-benign-early-exit vs an actual internal error; see the
# rc-based HOOK_OUTCOME set right after run_hook is called.
HOOK_NAME="codex-claude-shared-log"
HOOK_EVENT_NAME="PostToolUse"
HOOK_SESSION_ID="unknown"
# shellcheck source=/dev/null
source "$HOME/.claude/hooks/hook-event.sh"

log_err() {
    {
        mkdir -p "$ERR_LOG_DIR"
        local stamp
        stamp=$(date -u '+%Y-%m-%dT%H:%M:%S.%7NZ' 2>/dev/null) || stamp=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
        printf '[%s] %s\n' "$stamp" "${1:-unknown error}" >> "$ERR_LOG"
    } 2>/dev/null || true
}

# Collapse whitespace, redact obvious secrets, truncate to limit chars.
shorten() {
    local text="${1-}" limit="${2:-150}"
    if [[ -z "$text" ]]; then
        printf '%s' ""
        return 0
    fi
    local clean
    clean=$(printf '%s' "$text" \
        | tr '\r\n\t' '   ' \
        | sed -E 's/[[:space:]]+/ /g; s/^ +//; s/ +$//' \
        | sed -E 's/(api[_-]?key|token|secret|password) *= *[^ ]+/\1=<redacted>/Ig' \
        | sed -E 's/sk-[A-Za-z0-9_-]{12,}/sk-<redacted>/g') || clean="$text"
    if (( ${#clean} > limit )); then
        printf '%s' "${clean:0:limit-1}..."
    else
        printf '%s' "$clean"
    fi
}

# Credential courier (spec §3.5): nothing after `web cred` / `web login` reaches the shared log.
# Newlines are flattened first so a multi-line command can't carry text past the cut.
redact_web_cred() {
    local out
    out=$(printf '%s' "${1-}" | tr '\r\n\t' '   ' \
        | sed -E 's/(^|[^[:alnum:]_-])(web[[:space:]]+(cred|login))([^[:alnum:]_-].*)?$/\1\2 [redacted]/') \
        || out="[redacted]"
    printf '%s' "$out"
}

# Normalized path relative to project root when inside it, else absolute.
# Linux filesystems are case-sensitive, so the prefix match is too
# (the Windows original matched case-insensitively).
relative_path() {
    local path="${1-}" root="${2-}"
    if [[ -z "$path" ]]; then
        printf '%s' ""
        return 0
    fi
    local full base
    if ! full=$(realpath -m -s -- "$path" 2>/dev/null); then
        printf '%s' "$path"
        return 0
    fi
    if ! base=$(realpath -m -s -- "$root" 2>/dev/null); then
        printf '%s' "$path"
        return 0
    fi
    base="${base%/}/"
    if [[ "$full" == "$base"* ]]; then
        printf '%s' "${full#"$base"}"
    else
        printf '%s' "$full"
    fi
}

# Extract a string field (dotted path) from the JSON payload in $raw.
# Prefers jq; falls back to python (already a hard dependency via the
# emitter) so a missing jq does not silently disable the hook.
# Nonzero exit on unparseable JSON; empty output on a missing field.
# Relies on caller-scope $jq_bin, $python_bin, $raw.
json_field() {
    local path="$1"
    if [[ -n "$jq_bin" ]]; then
        "$jq_bin" -r ".${path} // empty" <<<"$raw" 2>/dev/null
    else
        printf '%s' "$raw" | "$python_bin" -c '
import json, sys
obj = json.load(sys.stdin)
for key in sys.argv[1].split("."):
    obj = obj.get(key) if isinstance(obj, dict) else None
if isinstance(obj, str):
    sys.stdout.write(obj)
elif obj is not None:
    sys.stdout.write(json.dumps(obj))
' "$path" 2>/dev/null
    fi
}

# Core logic runs in a subshell with errexit; any unexpected failure is
# caught by the wrapper at the bottom and logged, mirroring the original
# try/catch. Benign early exits use `exit 0`.
run_hook() (
    set -e

    raw=$(cat 2>/dev/null) || exit 0
    # Glob test, not ${raw//[[:space:]]/}: that substitution is quadratic in
    # bash and spun for minutes on large Edit/Write payloads (2026-09-29).
    if [[ $raw != *[![:space:]]* ]]; then
        exit 0
    fi

    jq_bin=$(command -v jq 2>/dev/null) || jq_bin=""
    python_bin=$(command -v python3 2>/dev/null || command -v python 2>/dev/null) || python_bin=""
    if [[ -z "$jq_bin" && -z "$python_bin" ]]; then
        echo "neither jq nor python available for JSON parsing" >&2
        exit 1
    fi

    # Invalid JSON: silent exit, matching the original.
    tool=$(json_field tool_name) || exit 0

    case "$tool" in
        Edit|MultiEdit|Write|NotebookEdit|Bash|PowerShell) ;;
        *) exit 0 ;;
    esac

    cwd=$(json_field cwd) || exit 0
    if [[ -z "$cwd" || ! -d "$cwd" ]]; then
        exit 0
    fi

    if [[ ! -f "$EMITTER" ]]; then
        exit 0
    fi

    # The original err-logged a missing python (the `& python` call threw
    # into the catch block); stay loud rather than silently no-op.
    if [[ -z "$python_bin" ]]; then
        echo "python interpreter not found for shared_log emitter" >&2
        exit 1
    fi

    project_root=$("$python_bin" "$EMITTER" find-project --cwd "$cwd" 2>/dev/null) || exit 0
    project_root=$(printf '%s' "$project_root" | tr -d '\r' | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')
    if [[ -z "$project_root" || ! -d "$project_root" ]]; then
        exit 0
    fi

    action="note"
    summary=""

    case "$tool" in
        Edit)
            fp=$(json_field tool_input.file_path) || exit 0
            action="edit"
            summary="edited $(relative_path "$fp" "$project_root")"
            ;;
        MultiEdit)
            fp=$(json_field tool_input.file_path) || exit 0
            action="edit"
            summary="multi-edited $(relative_path "$fp" "$project_root")"
            ;;
        Write)
            fp=$(json_field tool_input.file_path) || exit 0
            action="write"
            summary="wrote $(relative_path "$fp" "$project_root")"
            ;;
        NotebookEdit)
            np=$(json_field tool_input.notebook_path) || exit 0
            action="edit"
            summary="edited notebook $(relative_path "$np" "$project_root")"
            ;;
        Bash|PowerShell)
            action="run"
            desc=$(json_field tool_input.description) || exit 0
            cmd=$(json_field tool_input.command) || exit 0
            text="$cmd"
            if [[ -n "$desc" ]]; then
                text="$desc"
            fi
            text=$(redact_web_cred "$text")
            summary="ran: $(shorten "$text")"
            ;;
    esac

    if [[ -z "$summary" ]]; then
        exit 0
    fi

    # Never log actions about the shared log itself or goal-run bookkeeping.
    if [[ "$summary" == *".claude-codex-log.md"* ]]; then
        exit 0
    fi
    skip_re='pipeline[/\\]goal-runs[/\\].*(status|attempts|events)\.'
    if [[ "$summary" =~ $skip_re ]]; then
        exit 0
    fi

    "$python_bin" "$EMITTER" append --project "$project_root" --actor claude --action "$action" --summary "$summary" >/dev/null 2>&1 || true
    exit 0
)

# NOTE: run_hook must NOT be called from an `if !` / `||` condition:
# bash ignores `set -e` inside a function invoked from a context where
# the exit status is tested, which would make the errexit guard inert.
# A plain assignment keeps errexit live inside the subshell; the status
# is checked afterwards via $?.
err_output=$(run_hook 2>&1)
rc=$?
if [[ "$rc" -ne 0 ]]; then
    log_err "${err_output:-unexpected hook failure}"
    HOOK_OUTCOME="internal-error:$rc"
else
    HOOK_OUTCOME="ok"
fi
exit 0
