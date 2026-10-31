"""Atomic port assignment from dev-registry/ports.md.

Spec: PHASE_5_DISPATCH.md § 1.3 (provenance only; not shipped)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

DEFAULT_VAULT_ROOT = Path(
    os.environ.get(
        "AI_PROJECTS_ROOT", __import__('os').path.expanduser("~/AI_Projects")
    )
) / "_system" / "second-brain"

PORTS_FILE = "dev-registry/ports.md"

# Footer hints. The file phrases these as e.g. "**Next free frontend (3000s):** 3090"
# (NOT "...frontend port:"), so match flexibly on the label stem + first number.
NEXT_FRONTEND_RE = re.compile(
    r"\*\*Next free frontend[^:]*:\*\*\s*(\d+)", re.MULTILINE
)
NEXT_API_RE = re.compile(
    r"\*\*Next free API[^:]*:\*\*\s*(\d+)", re.MULTILINE
)


def _split_table_row(line: str) -> list[str] | None:
    """Split a markdown table row into cells, honoring escaped pipes (\\|).

    The old single regex dropped rows whose project slug contained '/'
    (example_scraper/example-scraper-cloud, .../example-scraper-ui, .../example-scraper)
    and rows whose notes contained an escaped pipe (8770's `PORT \\|\\| 8770`).
    """
    if not line.startswith("|"):
        return None
    parts = re.split(r"(?<!\\)\|", line)
    # outer pipes produce empty leading/trailing fields
    cells = [c.strip().replace("\\|", "|") for c in parts[1:-1]]
    return cells if cells else None


def _parse_ports(content: str) -> dict:
    rows = []
    in_table = False
    for line in content.splitlines():
        if line.startswith("|") and "Port" in line and "Project" in line:
            in_table = True
            continue
        if in_table and line.startswith("|---"):
            continue
        if in_table:
            cells = _split_table_row(line)
            if cells and len(cells) >= 5 and cells[0].isdigit():
                rows.append(
                    {
                        "port": int(cells[0]),
                        "project": cells[1],
                        "service": cells[2],
                        "source": cells[3],
                        "notes": cells[4],
                    }
                )
            elif line.strip() == "" or not line.startswith("|"):
                in_table = False
    fe_match = NEXT_FRONTEND_RE.search(content)
    api_match = NEXT_API_RE.search(content)
    return {
        "rows": rows,
        "next_frontend": int(fe_match.group(1)) if fe_match else 3080,
        "next_api": int(api_match.group(1)) if api_match else 3081,
    }


def _project_has_port(rows: list[dict], project: str, service_hint: str) -> int | None:
    for r in rows:
        if r["project"].lower() == project.lower():
            s = r["service"].lower()
            if service_hint == "frontend" and ("frontend" in s or "web ui" in s or "ui" in s):
                return r["port"]
            if service_hint == "api" and "api" in s:
                return r["port"]
            if service_hint == "other":
                return r["port"]
    return None


def _next_free(used_ports: set, start: int, step: int = 10) -> int:
    p = start
    while p in used_ports:
        p += step
    return p


def _write_back(
    file_path: Path,
    parsed: dict,
    new_rows: list[dict],
    new_next_fe: int,
    new_next_api: int,
) -> None:
    content = file_path.read_text(encoding="utf-8")
    last_pipe_idx = max(i for i, line in enumerate(content.splitlines()) if line.startswith("|"))
    lines = content.splitlines()
    insert_at = last_pipe_idx + 1
    for r in new_rows:
        row = f"| {r['port']} | {r['project']} | {r['service']} | {r['source']} | {r['notes']} |"
        lines.insert(insert_at, row)
        insert_at += 1
    new_content = "\n".join(lines)
    new_content = NEXT_FRONTEND_RE.sub(
        f"- **Next free frontend port:** {new_next_fe}", new_content
    )
    new_content = NEXT_API_RE.sub(
        f"- **Next free API port:** {new_next_api}", new_content
    )

    tmp = file_path.with_suffix(".md.tmp")
    tmp.write_text(new_content, encoding="utf-8")
    tmp.replace(file_path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True)
    ap.add_argument(
        "--service", required=True, choices=["frontend", "api", "other"]
    )
    ap.add_argument("--paired", action="store_true")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--vault-root", type=Path, default=DEFAULT_VAULT_ROOT)
    args = ap.parse_args()

    ports_file = args.vault_root / PORTS_FILE
    if not ports_file.exists():
        print(f"ports file not found: {ports_file}", file=sys.stderr)
        return 2

    content = ports_file.read_text(encoding="utf-8")
    parsed = _parse_ports(content)
    used = {r["port"] for r in parsed["rows"]}

    existing = _project_has_port(parsed["rows"], args.project, args.service)
    if existing is not None:
        print(
            f"project {args.project} already has {args.service} port {existing}",
            file=sys.stderr,
        )
        return 3

    if args.paired:
        fe = _next_free(used, parsed["next_frontend"])
        used.add(fe)
        api = _next_free(used, fe + 1)
        new_rows = [
            {
                "port": fe,
                "project": args.project,
                "service": "Frontend",
                "source": "assigned",
                "notes": "",
            },
            {
                "port": api,
                "project": args.project,
                "service": "API",
                "source": "assigned",
                "notes": f"Paired with {fe}",
            },
        ]
        new_next_fe = _next_free(used | {api}, fe + 10)
        new_next_api = _next_free(used | {api, new_next_fe}, api + 10)
        print(f"{fe} {api}")
    else:
        start = parsed["next_frontend"] if args.service == "frontend" else parsed["next_api"]
        port = _next_free(used, start)
        service_label = {
            "frontend": "Frontend",
            "api": "API",
            "other": "Service",
        }[args.service]
        new_rows = [
            {
                "port": port,
                "project": args.project,
                "service": service_label,
                "source": "assigned",
                "notes": "",
            }
        ]
        if args.service == "frontend":
            new_next_fe = _next_free(used | {port}, port + 10)
            new_next_api = parsed["next_api"]
        elif args.service == "api":
            new_next_fe = parsed["next_frontend"]
            new_next_api = _next_free(used | {port}, port + 10)
        else:
            new_next_fe = parsed["next_frontend"]
            new_next_api = parsed["next_api"]
        print(port)

    if args.commit:
        _write_back(ports_file, parsed, new_rows, new_next_fe, new_next_api)

    return 0


if __name__ == "__main__":
    sys.exit(main())
