"""Bidirectional sync between canonical vault and Claude Code harness memory.

Spec: PHASE_5_DISPATCH.md § 1.1 (provenance only; not shipped)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_VAULT_ROOT = Path(
    os.environ.get(
        "AI_PROJECTS_ROOT", __import__('os').path.expanduser("~/AI_Projects")
    )
) / "_system" / "second-brain"

DEFAULT_HARNESS_ROOT = Path(
    os.environ.get("CLAUDE_HARNESS_ROOT", str(Path.home() / ".claude" / "projects"))
)

SYNC_STATE_FILE = "_system/second-brain/.vault-sync-state.json"


def _slug_for_harness(project_slug: str) -> str:
    return project_slug


def _read_mtime(p: Path) -> float:
    return p.stat().st_mtime if p.exists() else 0.0


def _safe_read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


@dataclass
class PlannedCopy:
    direction: str
    src: Path
    dst: Path
    action: str
    reason: str = ""


def _load_state(vault_root: Path) -> dict:
    state_path = vault_root.parent.parent / SYNC_STATE_FILE
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_state(vault_root: Path, state: dict) -> None:
    state_path = vault_root.parent.parent / SYNC_STATE_FILE
    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(state_path)


def _pair_key(src: Path, dst: Path) -> str:
    return f"{src}|{dst}"


def _plan_actions_axis(
    vault_root: Path, harness_root: Path, project: str | None, direction: str
) -> list[PlannedCopy]:
    plans: list[PlannedCopy] = []
    actions_dir = vault_root / "Actions"
    if not actions_dir.exists():
        return plans
    for md in sorted(actions_dir.glob("*.md")):
        slug = md.stem
        for proj_dir in _project_dirs(harness_root, project):
            harness = proj_dir / f"feedback_{slug}.md"
            plans.append(_compare_pair(md, harness, direction))
    return plans


def _plan_project_axis(
    vault_root: Path, harness_root: Path, project: str | None, direction: str
) -> list[PlannedCopy]:
    plans: list[PlannedCopy] = []
    projects_dir = vault_root / "Projects"
    if not projects_dir.exists():
        return plans
    for proj_folder in sorted(projects_dir.iterdir()):
        if not proj_folder.is_dir():
            continue
        slug = proj_folder.name
        if project and slug != project:
            continue
        for proj_dir in _project_dirs(harness_root, slug):
            status_vault = proj_folder / "status.md"
            # Per-slug harness filename: pairing all projects to one `project_status.md`
            # collapsed 12 statuses into a single file and (in `both` mode) wrote foreign
            # status back into the wrong vault project. Keep one harness file per project.
            status_harness = proj_dir / f"project_status_{slug}.md"
            if status_vault.exists() or status_harness.exists():
                plans.append(_compare_pair(status_vault, status_harness, direction))
    return plans


def _project_dirs(harness_root: Path, project: str | None) -> list[Path]:
    # The harness "projects" dir is keyed by WORKSPACE PATH, not by vault project slug,
    # and may contain stale Windows-era and duplicate-slug dirs (both -home-you-AI-Projects and -home-you-AI_Projects exist; only the underscore one holds the memories). Syncing into all of them fanned
    # one project's status into every dir (incl. dead ones). This workspace has exactly
    # one canonical harness memory dir — target only that.
    if not harness_root.exists():
        return []
    canonical = harness_root / "-home-you-AI_Projects" / "memory"
    return [canonical] if canonical.exists() else []


def _compare_pair(vault_file: Path, harness_file: Path, direction: str) -> PlannedCopy:
    v_mtime = _read_mtime(vault_file)
    h_mtime = _read_mtime(harness_file)
    v_exists = vault_file.exists()
    h_exists = harness_file.exists()

    if direction == "vault-to-harness":
        if not v_exists:
            return PlannedCopy("v2h", vault_file, harness_file, "no-source")
        if not h_exists or v_mtime > h_mtime:
            return PlannedCopy("v2h", vault_file, harness_file, "copy")
        return PlannedCopy("v2h", vault_file, harness_file, "no-change")
    if direction == "harness-to-vault":
        if not h_exists:
            return PlannedCopy("h2v", harness_file, vault_file, "no-source")
        if not v_exists or h_mtime > v_mtime:
            return PlannedCopy("h2v", harness_file, vault_file, "copy")
        return PlannedCopy("h2v", harness_file, vault_file, "no-change")

    if v_exists and h_exists and v_mtime != h_mtime:
        v_content = _safe_read(vault_file)
        h_content = _safe_read(harness_file)
        if v_content == h_content:
            return PlannedCopy("both", vault_file, harness_file, "no-change")
        if v_mtime > h_mtime:
            return PlannedCopy("both", vault_file, harness_file, "copy v2h")
        return PlannedCopy("both", harness_file, vault_file, "copy h2v")
    if v_exists and not h_exists:
        return PlannedCopy("both", vault_file, harness_file, "copy v2h")
    if h_exists and not v_exists:
        return PlannedCopy("both", harness_file, vault_file, "copy h2v")
    return PlannedCopy("both", vault_file, harness_file, "no-change")


def _detect_conflicts(
    plans: list[PlannedCopy], state: dict
) -> list[PlannedCopy]:
    conflicts: list[PlannedCopy] = []
    for plan in plans:
        if plan.action not in ("copy", "copy v2h", "copy h2v"):
            continue
        key = _pair_key(plan.src, plan.dst)
        last_sync = state.get(key, {}).get("last_sync", 0.0)
        if last_sync == 0.0:
            continue
        src_mtime = _read_mtime(plan.src)
        dst_mtime = _read_mtime(plan.dst)
        if src_mtime > last_sync and dst_mtime > last_sync:
            conflicts.append(
                PlannedCopy(plan.direction, plan.src, plan.dst, "CONFLICT")
            )
    return conflicts


def _apply(plans: list[PlannedCopy], state: dict) -> None:
    import time

    for plan in plans:
        if not plan.action.startswith("copy"):
            continue
        plan.dst.parent.mkdir(parents=True, exist_ok=True)
        plan.dst.write_text(_safe_read(plan.src), encoding="utf-8")
        state[_pair_key(plan.src, plan.dst)] = {"last_sync": time.time()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--direction",
        choices=["vault-to-harness", "harness-to-vault", "both"],
        default="both",
    )
    ap.add_argument("--project", default=None)
    ap.add_argument(
        "--commit",
        action="store_true",
        help="Actually write. Default is dry-run.",
    )
    ap.add_argument("--vault-root", type=Path, default=DEFAULT_VAULT_ROOT)
    ap.add_argument("--harness-root", type=Path, default=DEFAULT_HARNESS_ROOT)
    args = ap.parse_args()

    if not args.vault_root.exists():
        print(f"vault root not found: {args.vault_root}", file=sys.stderr)
        return 2

    state = _load_state(args.vault_root)
    plans = []
    plans += _plan_actions_axis(
        args.vault_root, args.harness_root, args.project, args.direction
    )
    plans += _plan_project_axis(
        args.vault_root, args.harness_root, args.project, args.direction
    )

    conflicts = _detect_conflicts(plans, state)
    if conflicts:
        print("CONFLICTS — both sides modified since last sync:", file=sys.stderr)
        for c in conflicts:
            print(f"  {c.src} <-> {c.dst}", file=sys.stderr)
        return 1

    for p in plans:
        marker = "[WOULD]" if not args.commit else "[DO]"
        print(f"{marker} {p.direction} {p.action}: {p.src.name} -> {p.dst}")

    if args.commit:
        _apply(plans, state)
        _save_state(args.vault_root, state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
