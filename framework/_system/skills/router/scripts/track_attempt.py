"""Per-sub-task attempt counter for the no-self-rescue rule.

Spec: PHASE_5_DISPATCH.md § 3.2
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

STATE_FILE = Path(os.environ.get("CLAUDE_STATE_DIR", str(Path.home() / ".claude" / "state"))) / "attempt-counters.json"


def _load() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save(data: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data["_last_modified"] = time.time()
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["get", "inc", "reset", "list"])
    ap.add_argument("task_id", nargs="?", default=None)
    ap.add_argument("--max", dest="max_attempts", type=int, default=2)
    args = ap.parse_args()

    data = _load()
    counters = {k: v for k, v in data.items() if not k.startswith("_")}

    if args.action == "list":
        print(json.dumps(counters, indent=2))
        return 0

    if not args.task_id:
        print("task_id required for get/inc/reset", file=sys.stderr)
        return 2

    if args.action == "get":
        print(counters.get(args.task_id, 0))
        return 0

    if args.action == "inc":
        new_val = counters.get(args.task_id, 0) + 1
        data[args.task_id] = new_val
        _save(data)
        print(new_val)
        return 2 if new_val >= args.max_attempts else 0

    if args.action == "reset":
        if args.task_id in data:
            del data[args.task_id]
            _save(data)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
