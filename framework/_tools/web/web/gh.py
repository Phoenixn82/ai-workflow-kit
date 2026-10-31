"""`web gh`: GitHub repo intel via `gh api` (already authenticated; this module never reads or prints a token).
"""
import base64
import json
import subprocess
from urllib.parse import quote

from .clean import clean_markdown

TIMEOUT = 20


class GhError(Exception):
    """`gh` itself failed (anything but a 404 on releases/latest). `stderr` is gh's stderr, tail-printed by the CLI."""

    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


def _api(path: str) -> dict | list:
    try:
        out = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=TIMEOUT)
    except FileNotFoundError:
        raise GhError("gh is not installed") from None
    except subprocess.TimeoutExpired:
        raise GhError(f"gh api {path} timed out after {TIMEOUT}s") from None
    if out.returncode != 0:
        raise GhError(f"gh api {path} failed", stderr=out.stderr)
    return json.loads(out.stdout)


def _row(item: dict) -> dict:
    license_ = item.get("license") or {}
    return {
        "full_name": item.get("full_name"),
        "stars": item.get("stargazers_count"),
        "license": license_.get("spdx_id") or license_.get("key"),
        "pushed_at": item.get("pushed_at"),
        "description": item.get("description"),
        "archived": bool(item.get("archived")),
    }


def search(query: str, n: int = 10) -> list[dict]:
    """GitHub repo search, ranked by stars (as the API returns them)."""
    path = f"search/repositories?q={quote(query)}&sort=stars&order=desc&per_page={n}"
    data = _api(path)
    return [_row(item) for item in data.get("items", [])]


def _latest_release(owner_name: str) -> dict | None:
    try:
        return _api(f"repos/{owner_name}/releases/latest")
    except GhError as e:
        if "404" in (e.stderr or ""):
            return None
        raise


def _readme(owner_name: str) -> str | None:
    try:
        data = _api(f"repos/{owner_name}/readme")
    except GhError as e:
        if "404" in (e.stderr or ""):
            return None
        raise
    raw = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    return clean_markdown(raw)


def repo(owner_name: str) -> dict:
    """Repo metadata, latest release (None if there isn't one), and the cleaned README markdown (None if the
    repo has no README)."""
    data = _api(f"repos/{owner_name}")
    release = _latest_release(owner_name)
    readme = _readme(owner_name)
    row = _row(data)
    row["latest_release"] = release.get("tag_name") if release else None
    row["readme"] = readme
    return row


def format_verdict(row: dict) -> str:
    bits = [f"★{row['stars']}", row["license"] or "no license", f"pushed {row['pushed_at']}",
            f"latest {row['latest_release']}" if row.get("latest_release") else "no releases"]
    if row.get("archived"):
        bits.append("ARCHIVED")
    return f"{row['full_name']} — " + " · ".join(bits)


def format_repo_markdown(row: dict, with_readme: bool) -> str:
    lines = [format_verdict(row)]
    if with_readme:
        lines += ["", row.get("readme") or "(no README)"]
    return "\n".join(lines)


def format_search_markdown(rows: list[dict]) -> str:
    if not rows:
        return "(no results)"
    lines = []
    for row in rows:
        bits = [f"★{row['stars']}", row["license"] or "no license", f"pushed {row['pushed_at']}"]
        if row.get("archived"):
            bits.append("ARCHIVED")
        line = f"{row['full_name']} — " + " · ".join(bits)
        if row.get("description"):
            line += f"\n  {row['description']}"
        lines.append(line)
    return "\n".join(lines)
