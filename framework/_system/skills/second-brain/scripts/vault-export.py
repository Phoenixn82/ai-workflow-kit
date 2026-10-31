"""Export vault content as a portable bundle (markdown or JSON).

Spec: PHASE_5_DISPATCH.md § 1.2 (provenance only; not shipped)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date as date_cls
from pathlib import Path

DEFAULT_VAULT_ROOT = Path(
    os.environ.get(
        "AI_PROJECTS_ROOT", __import__('os').path.expanduser("~/AI_Projects")
    )
) / "_system" / "second-brain"

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(content)
    if not m:
        return {}, content
    raw, body = m.group(1), m.group(2)
    fm: dict = {}
    current_list_key = None
    for line in raw.splitlines():
        if not line.strip():
            current_list_key = None
            continue
        if line.startswith("  - ") and current_list_key:
            fm[current_list_key].append(line[4:].strip())
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            if v == "":
                fm[k] = []
                current_list_key = k
            elif v.startswith("[") and v.endswith("]"):
                fm[k] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
                current_list_key = None
            else:
                v_stripped = v.strip("'\"")
                if v_stripped.lower() in ("true", "false"):
                    fm[k] = v_stripped.lower() == "true"
                else:
                    fm[k] = v_stripped
                current_list_key = None
    return fm, body


def _collect_files(root: Path, axis: str, project: str | None) -> list[Path]:
    files: list[Path] = []
    if axis in ("actions", "all"):
        actions = root / "Actions"
        if actions.exists():
            files += sorted(actions.glob("*.md"))
    if axis in ("projects", "all"):
        projects = root / "Projects"
        if projects.exists():
            for proj in sorted(projects.iterdir()):
                if not proj.is_dir():
                    continue
                if project and proj.name != project:
                    continue
                files += sorted(proj.glob("*.md"))
    if axis in ("mechanics", "all"):
        mechanics = root / "Mechanics"
        if mechanics.exists():
            for mech in sorted(mechanics.iterdir()):
                if mech.is_dir():
                    files += sorted(mech.glob("*.md"))
    return files


def _filter_files(
    files: list[Path], since: str | None, tag: str | None
) -> list[Path]:
    keep: list[Path] = []
    for f in files:
        content = f.read_text(encoding="utf-8")
        fm, _ = _parse_frontmatter(content)
        if since:
            try:
                since_d = date_cls.fromisoformat(since)
                f_date_raw = fm.get("date") or fm.get("last-touched") or fm.get(
                    "date-updated"
                )
                if f_date_raw:
                    f_date = date_cls.fromisoformat(str(f_date_raw))
                    if f_date < since_d:
                        continue
            except (ValueError, TypeError):
                pass
        if tag:
            tags = fm.get("tags", [])
            if isinstance(tags, str):
                tags = [tags]
            if tag not in tags:
                continue
        keep.append(f)
    return keep


def _emit_markdown(files: list[Path]) -> str:
    out = []
    for f in files:
        out.append(f"<!-- {f} -->")
        out.append(f.read_text(encoding="utf-8"))
        out.append("\n---\n")
    return "\n".join(out)


def _emit_json(files: list[Path]) -> str:
    items = []
    for f in files:
        content = f.read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(content)
        items.append({"path": str(f), "frontmatter": fm, "body": body})
    return json.dumps(items, indent=2, default=str)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--axis", choices=["actions", "projects", "mechanics", "all"], default="all"
    )
    ap.add_argument("--project", default=None)
    ap.add_argument("--since", default=None, help="YYYY-MM-DD")
    ap.add_argument("--tag", default=None)
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("--out", default="-")
    ap.add_argument("--vault-root", type=Path, default=DEFAULT_VAULT_ROOT)
    args = ap.parse_args()

    if not args.vault_root.exists():
        print(f"vault root not found: {args.vault_root}", file=sys.stderr)
        return 1

    files = _collect_files(args.vault_root, args.axis, args.project)
    files = _filter_files(files, args.since, args.tag)

    payload = (
        _emit_markdown(files) if args.format == "markdown" else _emit_json(files)
    )

    if args.out == "-":
        sys.stdout.write(payload)
    else:
        Path(args.out).write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
