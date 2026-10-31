#!/usr/bin/env python3
"""Tiny shared activity log for Claude and Codex.

Each project gets <project_root>/.claude-codex-log.md. The log is intentionally
small: one line per action, capped at 100 lines, cheap to tail at turn start.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

LOG_NAME = ".claude-codex-log.md"
MAX_LINES = 100
MAX_SUMMARY = 180


def find_project_root(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (
            (candidate / ".git").exists()
            or (candidate / "CLAUDE.md").exists()
            or (candidate / "AGENTS.md").exists()
        ):
            return candidate
    return None


def resolve_project(project: str | None, cwd: str | None) -> Path | None:
    if project:
        path = Path(project).resolve()
        return path if path.exists() else None
    return find_project_root(Path(cwd or os.getcwd()))


def now_utc_minute() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def clean_summary(summary: str) -> str:
    text = " ".join(summary.strip().split())
    if len(text) > MAX_SUMMARY:
        return text[: MAX_SUMMARY - 1] + "\u2026"
    return text


def rotate(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(True)
        if len(lines) > MAX_LINES:
            path.write_text("".join(lines[-MAX_LINES:]), encoding="utf-8")
    except Exception:
        pass


def append_line(project_root: Path, actor: str, action: str, summary: str) -> None:
    log_path = project_root / LOG_NAME
    line = f"[{now_utc_minute()}] {actor} {action} \u2014 {clean_summary(summary)}\n"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line)
    rotate(log_path)


def tail(project_root: Path, lines: int) -> str:
    log_path = project_root / LOG_NAME
    if not log_path.exists():
        return ""
    content = log_path.read_text(encoding="utf-8", errors="replace").splitlines(True)
    return "".join(content[-lines:])


def main(argv: list[str] | None = None) -> int:
    # Force UTF-8 stdout/stderr so tailing a log with non-cp1252 chars (e.g. '→')
    # doesn't crash on Windows consoles whose code page is cp1252.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    append = sub.add_parser("append")
    append.add_argument("--project")
    append.add_argument("--cwd")
    append.add_argument("--actor", required=True, choices=("claude", "codex"))
    append.add_argument("--action", required=True)
    append.add_argument("--summary", required=True)

    tail_cmd = sub.add_parser("tail")
    tail_cmd.add_argument("--project")
    tail_cmd.add_argument("--cwd")
    tail_cmd.add_argument("--lines", type=int, default=20)

    find = sub.add_parser("find-project")
    find.add_argument("--cwd", required=True)

    args = parser.parse_args(argv)

    if args.command == "find-project":
        root = find_project_root(Path(args.cwd))
        if not root:
            return 1
        print(root)
        return 0

    root = resolve_project(getattr(args, "project", None), getattr(args, "cwd", None))
    if not root:
        print(
            "shared_log: could not resolve project root; pass --project or run under a dir with .git, CLAUDE.md, or AGENTS.md",
            file=sys.stderr,
        )
        return 1

    if args.command == "append":
        append_line(root, args.actor, args.action, args.summary)
        return 0
    if args.command == "tail":
        sys.stdout.write(tail(root, args.lines))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
