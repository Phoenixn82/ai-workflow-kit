"""Backfill retrieval scoring fields in second-brain markdown frontmatter."""

from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

DEFAULT_VAULT_ROOT = Path(
    os.environ.get("AI_PROJECTS_ROOT", __import__('os').path.expanduser("~/AI_Projects"))
) / "_system" / "second-brain"

FRONTMATTER_RE = re.compile(
    r"^(?P<open>---(?P<nl>\r\n|\n))(?P<frontmatter>.*?)(?P<close>(?P=nl)---(?P=nl))(?P<body>.*)\Z",
    re.DOTALL,
)
EXCLUDED_DIRS = {"_archive", "log-archive"}
EXCLUDED_FILES = {"boot.md"}


def _decode_utf8(data: bytes) -> tuple[str, bool] | None:
    has_bom = data.startswith(codecs.BOM_UTF8)
    payload = data[len(codecs.BOM_UTF8) :] if has_bom else data
    try:
        return payload.decode("utf-8"), has_bom
    except UnicodeDecodeError:
        return None


def _encode_utf8(text: str, has_bom: bool) -> bytes:
    payload = text.encode("utf-8")
    return codecs.BOM_UTF8 + payload if has_bom else payload


def _is_excluded(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    return rel.as_posix() in EXCLUDED_FILES or any(
        part in EXCLUDED_DIRS for part in rel.parts[:-1]
    )


def _frontmatter_mapping(raw: str) -> dict | None:
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _date_from_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()


def _body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _plan_update(path: Path, root: Path) -> dict:
    original_bytes = path.read_bytes()
    decoded = _decode_utf8(original_bytes)
    rel = path.relative_to(root).as_posix()
    if decoded is None:
        return {"path": rel, "status": "skipped_invalid_utf8"}

    text, has_bom = decoded
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {"path": rel, "status": "skipped_no_frontmatter"}

    frontmatter = match.group("frontmatter")
    data = _frontmatter_mapping(frontmatter)
    if data is None:
        return {"path": rel, "status": "skipped_invalid_frontmatter"}
    if "importance" in data:
        return {"path": rel, "status": "skipped_existing_importance"}

    nl = match.group("nl")
    additions = ["importance: 5"]
    if "last-touched" not in data:
        additions.append(f"last-touched: {_date_from_mtime(path)}")

    new_frontmatter = frontmatter
    if new_frontmatter and not new_frontmatter.endswith(nl):
        new_frontmatter += nl
    new_frontmatter += nl.join(additions)
    body = match.group("body")
    new_text = (
        match.group("open")
        + new_frontmatter
        + match.group("close")
        + body
    )
    new_bytes = _encode_utf8(new_text, has_bom)
    body_changed = _body_hash(body) != _body_hash(FRONTMATTER_RE.match(new_text).group("body"))
    return {
        "path": rel,
        "status": "touched",
        "original_bytes": original_bytes,
        "new_bytes": new_bytes,
        "body_changed": body_changed,
    }


def _iter_markdown(root: Path) -> list[Path]:
    return [
        path
        for path in sorted(root.rglob("*.md"))
        if path.is_file() and not _is_excluded(path, root)
    ]


def _backup_original(record: dict, backup_dir: Path) -> None:
    backup_path = backup_dir / record["path"]
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.write_bytes(record["original_bytes"])


def run(vault_root: Path, backup_dir: Path | None, dry_run: bool) -> dict:
    if not vault_root.exists():
        raise FileNotFoundError(f"vault root not found: {vault_root}")
    if not dry_run and backup_dir is None:
        raise ValueError("--backup-dir is required when writing")

    plans = [_plan_update(path, vault_root) for path in _iter_markdown(vault_root)]
    touched = [record for record in plans if record["status"] == "touched"]
    skipped = [record for record in plans if record["status"] != "touched"]

    if not dry_run:
        assert backup_dir is not None
        backup_dir.mkdir(parents=True, exist_ok=True)
        for record in touched:
            path = vault_root / record["path"]
            _backup_original(record, backup_dir)
            path.write_bytes(record["new_bytes"])

    skipped_by_reason: dict[str, int] = {}
    for record in skipped:
        skipped_by_reason[record["status"]] = skipped_by_reason.get(record["status"], 0) + 1

    return {
        "dry_run": dry_run,
        "vault_root": str(vault_root),
        "backup_dir": str(backup_dir) if backup_dir else None,
        "seen": len(plans),
        "touched": len(touched),
        "skipped": len(skipped),
        "skipped_by_reason": skipped_by_reason,
        "body_changed": sum(1 for record in touched if record["body_changed"]),
        "touched_paths": [record["path"] for record in touched],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-root", type=Path, default=DEFAULT_VAULT_ROOT)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args.vault_root, args.backup_dir, args.dry_run)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        verb = "WOULD_TOUCH" if args.dry_run else "TOUCHED"
        print(f"{verb}: {result['touched']}")
        print(f"SKIPPED: {result['skipped']}")
        print(f"BODY_CHANGED: {result['body_changed']}")
        for reason, count in sorted(result["skipped_by_reason"].items()):
            print(f"{reason}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
