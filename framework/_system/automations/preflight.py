"""Automation budget circuit breaker. Run FIRST in every automation.

Exit 0 = clear to run. Exit 2 = blocked (budget reached or HALT present).
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def ledger_path(base: Path) -> Path:
    return Path(base) / f"spend-ledger-{today_str()}.jsonl"


def read_budget_ceiling(base: Path) -> int:
    return int(json.loads((Path(base) / "budget.json").read_text()).get("daily_token_ceiling", 0))


def ledger_spent(base: Path) -> tuple[bool, int, str]:
    ledger = ledger_path(base)
    spent = 0
    if not ledger.exists():
        return True, spent, "ok"
    for line_no, line in enumerate(ledger.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            spent += int(row.get("tokens", 0))
        except Exception:
            return False, 0, f"malformed ledger: {ledger.name}:{line_no}"
    return True, spent, "ok"


def check(base: Path) -> tuple[bool, str]:
    base = Path(base)
    if (base / "HALT").exists():
        return False, "HALT file present - all automations halted"
    ceiling = read_budget_ceiling(base)
    if ceiling <= 0:
        return False, "daily_token_ceiling <= 0 - automations disabled"
    ok, spent, reason = ledger_spent(base)
    if not ok:
        return False, reason
    if spent >= ceiling:
        return False, f"daily budget reached: {spent}/{ceiling} tokens"
    return True, "ok"


def check_declared_max(base: Path, declared_max: int, automation: str) -> tuple[bool, str]:
    del automation
    base = Path(base)
    if (base / "HALT").exists():
        return False, "HALT file present - all automations halted"
    ceiling = read_budget_ceiling(base)
    if ceiling <= 0:
        return False, "daily_token_ceiling <= 0 - automations disabled"
    ok, spent, reason = ledger_spent(base)
    if not ok:
        return False, reason
    predicted = spent + int(declared_max)
    if predicted > ceiling:
        return False, f"declared max would exceed budget: {predicted}/{ceiling} tokens"
    return True, "ALLOW"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Automation token budget preflight")
    parser.add_argument("base", nargs="?", default=str(Path(__file__).parent))
    parser.add_argument("--declare-max", type=int, dest="declare_max")
    parser.add_argument("--automation", default="")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    base = Path(args.base)
    if args.declare_max is not None:
        allowed, reason = check_declared_max(base, args.declare_max, args.automation)
        print("ALLOW" if allowed else reason)
        sys.exit(0 if allowed else 1)
    allowed, reason = check(base)
    print(reason)
    sys.exit(0 if allowed else 2)
