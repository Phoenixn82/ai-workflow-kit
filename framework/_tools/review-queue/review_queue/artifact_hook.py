"""Claude Code PostToolUse hook (matcher "Artifact"): queue every published artifact.

A script, not a model, so a queued artifact costs zero tokens. It never blocks or
fails the tool call: any problem is swallowed and the hook exits 0.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

from . import queue

ARTIFACT_URL = re.compile(r"https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9_-]+")
TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def artifact_url(event: dict) -> str | None:
    """The artifact a publish created or updated; None for reads, lists, assets and other actions."""
    inp = event.get("tool_input") or {}
    if inp.get("action", "publish") != "publish" or inp.get("asset"):
        return None
    if inp.get("url") and ARTIFACT_URL.fullmatch(inp["url"]):
        return inp["url"]
    others = {inp.get("type_url"), inp.get("from_url")}
    response = json.dumps(event.get("tool_response"))
    return next((u for u in ARTIFACT_URL.findall(response) if u not in others), None)


def title_for(inp: dict) -> str:
    try:
        m = TITLE.search(Path(inp["file_path"]).read_text(errors="replace")[:20000])
        if m and m.group(1).strip():
            return m.group(1).strip()
    except (KeyError, OSError, TypeError):
        pass
    return inp.get("title") or "Claude artifact"


def main() -> None:
    try:
        event = json.load(sys.stdin)
        url = artifact_url(event)
        if url:
            inp = event.get("tool_input") or {}
            queue.push(url, title=title_for(inp), note=inp.get("description") or "")
            snapshot(url, inp)
    except Exception:  # noqa: BLE001 - a hook must never break the tool call
        pass


def snapshot(url: str, inp: dict) -> None:
    """Copy the published page next to the queue; the widget can't load claude.ai itself.
    ponytail: the page only, not `files` siblings or runtime capabilities; Enter opens the live one."""
    src = Path(inp.get("file_path") or "")
    if src.suffix.lower() in queue.HTML_SUFFIXES and src.is_file():
        dst = queue.artifact_snapshot(url)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


if __name__ == "__main__":
    main()
