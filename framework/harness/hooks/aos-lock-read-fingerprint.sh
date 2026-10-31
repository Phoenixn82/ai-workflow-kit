#!/usr/bin/env bash
# Claude PostToolUse hook: aos_lock read-fingerprinting (bridge read-side).
#
# Pipes the Read tool event JSON to "aos_lock.py hook postuse-read" so Claude's
# reads are recorded as sha256 fingerprints in
# ~/agentic-os/locks/session_reads/<session_id>.json. This is the Claude half
# of the shared Claude/Codex stale-read gate: it lets either side see what the
# other has read, and seeds the fingerprints an acquire would later check.
#
# Scope by design: read-fingerprinting ONLY. There is intentionally NO
# PreToolUse acquire hook on Edit/Write. aos_lock's preuse-edit gate exits 2
# (deny-with-feedback) whenever the session has no prior read fingerprint for a
# file -- which would block routine Claude edits (overwrites, harness-cached
# reads, or a read whose async fingerprint has not landed yet). That friction
# breaks the no-permission-prompts / never-block-routine-work rule, so
# acquire/release stay a manual or skill-invoked step:
#   python3 "$HOME/agentic-os/bin/aos_lock.py" acquire <path> --agent claude --intent "..."
#   python3 "$HOME/agentic-os/bin/aos_lock.py" release <path>
#
# Silent by design: never prints context, never blocks Claude. Pure ASCII.
# Fail-open: every failure path still exits 0.

set -u

# Fail-open even if HOME is somehow unset (set -u would otherwise abort non-zero).
[ -n "${HOME:-}" ] || exit 0

HOOK_NAME="aos-lock-read-fingerprint"
HOOK_EVENT_NAME="PostToolUse"
HOOK_SESSION_ID="unknown"
# shellcheck source=/dev/null
source "$HOME/.claude/hooks/hook-event.sh"

log_error() {
    # Best-effort error log; never let logging itself fail the hook.
    {
        local log_dir="$HOME/.claude/hooks/logs"
        mkdir -p "$log_dir" 2>/dev/null || return 0
        printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" "$1" \
            >> "$log_dir/aos-lock-read-fingerprint.err.log" 2>/dev/null
    } 2>/dev/null || true
}

aos_lock="$HOME/agentic-os/bin/aos_lock.py"
if [ ! -f "$aos_lock" ]; then HOOK_OUTCOME="no-aos-lock-script"; exit 0; fi

python_bin=""
if command -v python3 >/dev/null 2>&1; then
    python_bin="python3"
elif command -v python >/dev/null 2>&1; then
    python_bin="python"
else
    HOOK_OUTCOME="no-python"
    exit 0
fi

# Read the hook event JSON off stdin. Linux is UTF-8 native, so the
# PowerShell-era console/pipe encoding workarounds are unnecessary here.
raw="$(cat 2>/dev/null)" || raw=""
if command -v jq >/dev/null 2>&1; then
    HOOK_SESSION_ID=$(printf '%s' "$raw" | jq -r '.session_id // "unknown"' 2>/dev/null || echo unknown)
fi
case "$raw" in
    *[![:space:]]*) ;;  # has non-whitespace content, proceed
    *) HOOK_OUTCOME="empty-stdin"; exit 0 ;;
esac

# PYTHONUTF8 kept from the original as cheap insurance: a legacy non-UTF-8
# locale (not coerced by PEP 538) would otherwise mis-decode non-ASCII paths.
export PYTHONUTF8=1
printf '%s' "$raw" | "$python_bin" "$aos_lock" hook postuse-read >/dev/null 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
    log_error "aos_lock.py hook postuse-read failed (exit $rc)"
    HOOK_OUTCOME="aos-lock-error:$rc"
else
    HOOK_OUTCOME="fingerprinted"
fi

exit 0
