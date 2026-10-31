#!/usr/bin/env python3
"""Self-check for herdr-quiet-diffs.py: python3 test_herdr_quiet_diffs.py"""
import json
import os
import subprocess
import sys

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "herdr-quiet-diffs.py")


def run(mode, payload, herdr="1"):
    env = {**os.environ, "HERDR_ENV": herdr}
    out = subprocess.run([sys.executable, HOOK, mode], input=json.dumps(payload),
                         capture_output=True, text=True, env=env, check=True).stdout
    return json.loads(out) if out.strip() else None


sid = f"selftest-{os.getpid()}"
edit = {"session_id": sid, "tool_response": {"filePath": "/w/a.py", "type": "update", "structuredPatch": [
    {"oldStart": 1, "oldLines": 2, "newStart": 1, "newLines": 3, "lines": [" a", "-b", "+B", "+c"]}]}}
create = {"session_id": sid, "tool_response": {"filePath": "/w/new.txt", "type": "create",
                                               "content": "x\ny\n", "structuredPatch": []}}

assert run("post", edit, herdr="") is None, "must do nothing outside Herdr"
post = run("post", edit)
assert post["systemMessage"] == "+2 -1"
assert post["hookSpecificOutput"]["updatedToolOutput"]["structuredPatch"] == []
assert post["hookSpecificOutput"]["updatedToolOutput"]["filePath"] == "/w/a.py"
assert run("post", create) is None, "creates keep native preview"

msg = run("stop", {"session_id": sid, "cwd": "/w"})["systemMessage"]
assert "--- a.py" in msg and "--- new.txt" in msg
assert "\x1b[31m-b" in msg and "\x1b[32m+B" in msg and "\x1b[32m+y" in msg
assert run("stop", {"session_id": sid, "cwd": "/w"}) is None, "log cleared after reveal"
print("ok")
