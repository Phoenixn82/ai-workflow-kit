"""Append one automation run's token usage to today's ledger.

Usage: record_spend.py <base-dir> <automation-name> <tokens> [status]
"""
from __future__ import annotations
from contextlib import contextmanager
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from preflight import today_str


def _ledger_path(base: Path) -> Path:
    return Path(base) / f"spend-ledger-{today_str()}.jsonl"


def _lock_path(base: Path) -> Path:
    return Path(base) / f"spend-ledger-{today_str()}.jsonl.lock"


@contextmanager
def ledger_lock(base: Path, timeout_s: float = 10.0):
    lock = _lock_path(base)
    deadline = time.time() + timeout_s
    fd = None
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii", "ignore"))
            break
        except FileExistsError:
            if time.time() >= deadline:
                raise TimeoutError(f"could not acquire ledger lock: {lock}")
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def _row(automation: str, tokens: int, status: str) -> dict[str, object]:
    if not isinstance(automation, str) or not automation.strip():
        raise ValueError("automation must be a non-empty string")
    if isinstance(tokens, bool):
        raise ValueError("tokens must be a non-negative integer")
    try:
        token_count = int(tokens)
    except (TypeError, ValueError) as exc:
        raise ValueError("tokens must be a non-negative integer") from exc
    if token_count < 0:
        raise ValueError("tokens must be a non-negative integer")
    if not isinstance(status, str) or not status.strip():
        raise ValueError("status must be a non-empty string")
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "automation": automation,
        "tokens": token_count,
        "status": status,
    }


def _encode_row(row: dict[str, object]) -> str:
    line = json.dumps(row)
    parsed = json.loads(line)
    if not isinstance(parsed, dict) or "tokens" not in parsed:
        raise ValueError("malformed ledger row")
    return line + "\n"


def record(base, automation: str, tokens: int, status: str = "ok") -> None:
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    line = _encode_row(_row(automation, tokens, status))
    with ledger_lock(base):
        with open(_ledger_path(base), "a", encoding="utf-8") as f:
            f.write(line)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("Usage: record_spend.py <base-dir> <automation-name> <tokens> [status]", file=sys.stderr)
        return 1
    try:
        record(
            Path(argv[0]),
            argv[1],
            int(argv[2]),
            argv[3] if len(argv) > 3 else "ok",
        )
    except Exception as exc:
        print(f"record_spend: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
