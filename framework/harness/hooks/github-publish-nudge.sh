#!/usr/bin/env bash
# UserPromptSubmit hook - github-publish-nudge
# When the user signals they're making/adding/publishing a GitHub repo PUBLIC,
# inject a reminder so Claude proactively runs the repo doc-standard check.
# Injects context only; never asks permission; silent (no output) when no match.
# Fail-open: any error still exits 0 so a broken hook never breaks the session.
set -u

raw=$(cat 2>/dev/null) || raw=""

HOOK_NAME="github-publish-nudge"
HOOK_EVENT_NAME="UserPromptSubmit"
HOOK_SESSION_ID="unknown"
if [ -n "$raw" ] && command -v jq >/dev/null 2>&1; then
    HOOK_SESSION_ID=$(printf '%s' "$raw" | jq -r '.session_id // "unknown"' 2>/dev/null || echo unknown)
fi
# shellcheck source=/dev/null
source "$HOME/.claude/hooks/hook-event.sh"

if [ -z "$raw" ]; then HOOK_OUTCOME="empty-stdin"; exit 0; fi

# Extract .prompt if stdin is the hook's JSON payload; fall back to raw text.
# If jq is missing this also falls back to raw-text matching over the JSON
# bytes - real payloads ({"prompt":...}) still fire correctly; only unusual
# payload shapes can over/under-fire until jq is installed.
prompt=""
if parsed=$(printf '%s' "$raw" | jq -r '.prompt // empty' 2>/dev/null); then
    prompt="$parsed"
else
    prompt="$raw"
fi
if [ -z "$prompt" ]; then HOOK_OUTCOME="empty-prompt"; exit 0; fi

# PS1 -imatch scans the whole multi-line prompt (.NET \s matches \n, so e.g.
# "going\npublic" hits); grep is line-based and would miss those. Flatten
# CR/LF to spaces so newline-bridged phrases still match. Slight overshoot vs
# the original (a .{0,25} gap may now cross a former newline) is fine for an
# advisory nudge - an extra nudge beats a missed one.
prompt=$(printf '%s' "$prompt" | tr '\r\n' '  ')

patterns=(
    'mak\w*\s+.{0,25}\bpublic\b'
    '\bgo(ing)?\s+public\b'
    '\bpublic(ly|ize|ise)?\b.{0,30}\b(repo|repository|github)\b'
    '\b(repo|repository|github)\b.{0,30}\bpublic\b'
    '\b(publish\w*|open[-[:space:]]?sourc\w*)\b.{0,30}\b(repo|repository|project|github|it|this)\b'
    '\bnew\s+public\s+repo\b'
)

hit=0
for p in "${patterns[@]}"; do
    if printf '%s' "$prompt" | grep -Eiq "$p" 2>/dev/null; then
        hit=1
        break
    fi
done
if [ "$hit" -ne 1 ]; then HOOK_OUTCOME="no-match"; exit 0; fi
HOOK_OUTCOME="nudged"

msg='the user may be taking a GitHub repo public. Per their standing request, proactively offer/run the repo doc-standard check as part of this work: README has an H1 title + a one-line description + a Getting-started (or Install+Run) section + a screenshot-or-code-example + a License section; a LICENSE file exists at root; the repo description equals the README one-liner; 5-8 relevant topics; and run a privacy/secret scan against the publishing denylist. Standard + checklist: _system/second-brain/Reference/github-repo-doc-standard.md . Visually vet any screenshot for private data BEFORE it lands on a public repo. NEVER flip repo visibility yourself - Approach-C: the human does the public flip.'

# Emit via jq when available (safe escaping if msg ever changes); otherwise
# printf the static JSON directly - msg is ASCII with no ", \, or control
# chars, so this is valid JSON as-is. The PS1 original had no external deps;
# without this fallback a missing jq would silently swallow the nudge.
if command -v jq >/dev/null 2>&1; then
    jq -cn --arg msg "$msg" \
        '{hookSpecificOutput:{hookEventName:"UserPromptSubmit",additionalContext:$msg}}' \
        2>/dev/null || true
else
    printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"%s"}}\n' "$msg"
fi

exit 0
