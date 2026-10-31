"""Append a routing decision to second-brain vault.

Spec: PHASE_5_DISPATCH.md § 3.3
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_VAULT_ROOT = Path(
    os.environ.get(
        "AI_PROJECTS_ROOT", __import__('os').path.expanduser("~/AI_Projects")
    )
) / "_system" / "second-brain"

ROUTING_LOG_SECTION = "## Routing log"


def _project_decisions_path(vault: Path, slug: str) -> Path:
    return vault / "Projects" / slug / "decisions.md"


def _actions_routing_path(vault: Path) -> Path:
    return vault / "Actions" / "routing-defaults.md"


def _is_recent_duplicate(content: str, task: str, lane: str) -> bool:
    pattern = re.compile(
        rf"^## (\d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}}) — {re.escape(task)}$",
        re.MULTILINE,
    )
    for m in pattern.finditer(content):
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
            age = (datetime.now() - ts).total_seconds()
            if age < 60:
                trailing = content[m.end():m.end() + 200]
                if f"**Lane:** {lane}" in trailing:
                    return True
        except ValueError:
            continue
    return False


def _format_entry(lane: str, task: str, reason: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        f"\n## {now} — {task}\n\n"
        f"**Lane:** {lane}\n"
        f"**Reason:** {reason}\n"
    )


def _append_to_actions(file_path: Path, entry: str) -> None:
    if not file_path.exists():
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            f"---\ntype: preference\nai-first: true\n---\n\n# routing-defaults\n\n{ROUTING_LOG_SECTION}\n{entry}",
            encoding="utf-8",
        )
        return
    content = file_path.read_text(encoding="utf-8")
    if ROUTING_LOG_SECTION not in content:
        content = content.rstrip() + f"\n\n{ROUTING_LOG_SECTION}\n{entry}"
    else:
        idx = content.index(ROUTING_LOG_SECTION) + len(ROUTING_LOG_SECTION)
        content = content[: idx] + entry + content[idx:]
    file_path.write_text(content, encoding="utf-8")


def _append_to_project_decisions(file_path: Path, entry: str) -> None:
    if not file_path.exists():
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            f"---\ntype: decisions-log\nai-first: true\n---\n\n# decisions log\n{entry}",
            encoding="utf-8",
        )
        return
    content = file_path.read_text(encoding="utf-8") + entry
    file_path.write_text(content, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lane", required=True, choices=["claude", "codex", "gemini", "freellm"])
    ap.add_argument("--task", required=True)
    ap.add_argument("--reason", required=True)
    ap.add_argument("--project", default=None)
    ap.add_argument("--vault-root", type=Path, default=DEFAULT_VAULT_ROOT)
    args = ap.parse_args()

    if args.project:
        target = _project_decisions_path(args.vault_root, args.project)
    else:
        target = _actions_routing_path(args.vault_root)

    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    if _is_recent_duplicate(existing, args.task, args.lane):
        print("duplicate within 60s, skipping", file=sys.stderr)
        return 0

    entry = _format_entry(args.lane, args.task, args.reason)
    if args.project:
        _append_to_project_decisions(target, entry)
    else:
        _append_to_actions(target, entry)
    print(str(target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
