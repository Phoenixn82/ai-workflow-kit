"""Register finished files dropped into the shared Review Queue Inbox."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from . import queue

INBOX = Path.home() / "Review Queue Inbox"
STATE = queue.state_dir() / "intake.json"


def scan(inbox: Path = INBOX, state: Path = STATE, settle_seconds: int = 3) -> int:
    inbox.mkdir(parents=True, exist_ok=True)
    try:
        seen = json.loads(state.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        seen = {}

    candidates = []
    for path in inbox.iterdir():
        if path.name.startswith(".") or not path.is_file() or not queue.reviewable(path):
            continue
        stat = path.stat()
        if time.time() - stat.st_mtime < settle_seconds:
            continue
        signature = [stat.st_mtime_ns, stat.st_size]
        if seen.get(path.name) != signature:
            candidates.append((stat.st_mtime_ns, path, signature))

    for _, path, signature in sorted(candidates):
        queue.push(path)
        seen[path.name] = signature

    if candidates:
        state.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=state.parent, prefix=".intake-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(seen, handle)
            os.replace(temporary, state)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return len(candidates)


if __name__ == "__main__":
    automations = Path.home() / "AI_Projects/_system/automations"
    if subprocess.run(["/usr/bin/python3", str(automations / "preflight.py")],
                      stdout=subprocess.DEVNULL).returncode == 0:
        count = scan()
        if count:
            subprocess.run(["/usr/bin/python3", str(automations / "record_spend.py"),
                            str(automations), "review-queue-intake", "0", "ok"],
                           check=True)
            print(f"queued {count} file(s)")
