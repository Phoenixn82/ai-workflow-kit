#!/usr/bin/env python3
"""Fixed context cost a session pays before its first prompt.

Sums the auto-loaded instruction files plus every SessionStart hook's injected
context. Tokens are estimated at 4 bytes/token. Flags hooks whose output is
byte-identical to an earlier hook's — the duplicate-injection bug this pipeline
found on 2026-09-12.
"""
import hashlib
import json
import os
import subprocess
import sys

HOME = os.path.expanduser("~")
cwd = sys.argv[1] if len(sys.argv) > 1 else f"{HOME}/AI_Projects"
payload = json.dumps({"session_id": "measure", "transcript_path": "/nonexistent",
                      "cwd": cwd, "source": "startup"})

rows, total = [], 0

for f in (f"{HOME}/.claude/CLAUDE.md", f"{cwd}/CLAUDE.md",
          f"{HOME}/.claude/projects/-home-you-AI-Projects/memory/MEMORY.md"):
    if os.path.isfile(f):
        n = os.path.getsize(f)
        rows.append((n, f.replace(HOME, "~"), ""))
        total += n

seen = {}
for cfg in (f"{HOME}/.claude/settings.json", f"{cwd}/.claude/settings.json"):
    try:
        d = json.load(open(cfg))
    except Exception:
        continue
    for m in d.get("hooks", {}).get("SessionStart", []):
        for h in m.get("hooks", []):
            cmd = h.get("command", "")
            try:
                out = subprocess.run(cmd, shell=True, input=payload, capture_output=True,
                                     text=True, timeout=20).stdout
            except Exception:
                out = ""
            try:
                ctx = json.loads(out).get("hookSpecificOutput", {}).get("additionalContext", "")
                out = ctx or out
            except Exception:
                pass
            n = len(out.encode())
            digest = hashlib.sha256(out.encode()).hexdigest()
            tag = "  <- DUPLICATE" if n and digest in seen else ""
            seen[digest] = cmd
            rows.append((n, cmd[:66], tag))
            total += n

for n, name, tag in rows:
    print(f"  {n:7d} B  {n // 4:6d} tok  {name}{tag}")
print(f"\n  FIXED STARTUP COST: {total:,} bytes ≈ {total // 4:,} tokens")
