#!/usr/bin/env python3
"""Relay a Codex changes.md manifest as one user-facing sentence."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

OUTCOME_TAGS = {
    "completed": "[OK]",
    "partial": "[~]",
    "budget_hit": "[BUDGET]",
    "failed": "[FAIL]",
    "aborted": "[ABORT]",
}


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    text = text.lstrip("﻿")  # tolerate a UTF-8 BOM (PowerShell-written manifests)
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not match:
        return {}, text
    frontmatter: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            frontmatter[key.strip()] = value.strip()
    return frontmatter, match.group(2)


def mark_relayed(text: str) -> str:
    text = text.lstrip("﻿")  # tolerate a UTF-8 BOM (PowerShell-written manifests)
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not match:
        return text
    block, body = match.group(1), match.group(2)
    if re.search(r"(?m)^relayed:\s*.*$", block):
        block = re.sub(r"(?m)^relayed:\s*.*$", "relayed: true", block)
    else:
        block = block.rstrip() + "\nrelayed: true"
    return f"---\n{block}\n---\n{body}"


def outcome_sentence(body: str) -> str:
    match = re.search(r"^\*\*Outcome:\*\*\s*(.+?)$", body, re.M)
    if match:
        return match.group(1).strip()
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return "(no outcome line in manifest)"


def changes_path(run_dir: Path) -> Path:
    return run_dir / "changes.md"


def check(run_dir: Path) -> int:
    path = changes_path(run_dir)
    if not path.is_file():
        return 1
    frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    return 2 if frontmatter.get("relayed", "false").lower() == "true" else 0


def read(run_dir: Path, relay: bool) -> int:
    path = changes_path(run_dir)
    if not path.is_file():
        print(f"[relay] no changes.md at {path}", file=sys.stderr)
        return 1
    text = path.read_text(encoding="utf-8", errors="replace")
    frontmatter, body = split_frontmatter(text)
    tag = OUTCOME_TAGS.get(frontmatter.get("outcome", "").lower(), "[?]")
    print(f"{tag} {outcome_sentence(body)}")
    if relay and frontmatter.get("relayed", "false").lower() != "true":
        path.write_text(mark_relayed(text), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check")
    group.add_argument("--read")
    group.add_argument("--relay")
    args = parser.parse_args(argv)

    if args.check:
        return check(Path(args.check))
    if args.read:
        return read(Path(args.read), relay=False)
    if args.relay:
        return read(Path(args.relay), relay=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
