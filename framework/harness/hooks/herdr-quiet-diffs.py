#!/usr/bin/env python3
"""Herdr panes only: quiet edits during a turn, full diff reveal at the end.

  post  (PostToolUse Edit|MultiEdit|Write): hide the inline red/green diff,
        show a one-line +N -M summary, and log the patch for this session.
  stop  (Stop): print every logged patch from this turn, then clear the log.

The model's tool result is unchanged (it never receives structuredPatch).
New files keep Claude Code's short native preview; they appear in full at the end.
"""
import json
import os
import sys
import tempfile

if os.environ.get("HERDR_ENV") != "1":
    sys.exit(0)

try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(0)

log = os.path.join(tempfile.gettempdir(), f"herdr-diffs-{data.get('session_id', 'x')}.jsonl")
mode = sys.argv[1] if len(sys.argv) > 1 else "post"
RED, GREEN, RESET = "\x1b[31m", "\x1b[32m", "\x1b[0m"


def color(line):
    if line.startswith("+"):
        return GREEN + line + RESET
    if line.startswith("-"):
        return RED + line + RESET
    return line


if mode == "post":
    resp = data.get("tool_response")
    if not isinstance(resp, dict):
        sys.exit(0)
    hunks = resp.get("structuredPatch")
    if not hunks and resp.get("type") == "create":
        new = ["+" + l for l in (resp.get("content") or "").splitlines()]
        hunks = [{"oldStart": 0, "oldLines": 0, "newStart": 1, "newLines": len(new), "lines": new}]
    if not hunks:
        sys.exit(0)
    with open(log, "a") as f:
        f.write(json.dumps({"path": resp.get("filePath", "?"), "hunks": hunks}) + "\n")
    if not resp.get("structuredPatch"):
        sys.exit(0)
    lines = [l for h in hunks for l in h.get("lines", [])]
    added = sum(l.startswith("+") for l in lines)
    removed = sum(l.startswith("-") for l in lines)
    print(json.dumps({
        "systemMessage": f"+{added} -{removed}",
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": {**resp, "structuredPatch": []},
        },
    }))

elif mode == "stop" and os.path.exists(log):
    with open(log) as f:
        edits = [json.loads(l) for l in f if l.strip()]
    os.remove(log)
    cwd = data.get("cwd") or os.getcwd()
    out = []
    for e in edits:
        out.append(f"--- {os.path.relpath(e['path'], cwd)}")
        for h in e["hunks"]:
            out.append(f"@@ -{h.get('oldStart')},{h.get('oldLines')} +{h.get('newStart')},{h.get('newLines')} @@")
            out.extend(color(l) for l in h.get("lines", []))
    if out:
        print(json.dumps({"systemMessage": "Changes this turn:\n" + "\n".join(out)}))
