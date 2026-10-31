"""Manifest of the last N files pushed for review. Stdlib only, atomic writes."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

MAX_ENTRIES = 25
# the user, 2026-10-13: the widget shows things to look at, never text.
# the user, 2026-10-16: videos and PDFs are back.
# Markdown notes, reports, diffs and logs are refused at push and dropped on load.
IMAGE_SUFFIXES = {".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".jxl", ".png", ".svg", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".mkv", ".webm"}
HTML_SUFFIXES = {".html", ".htm"}
PDF_SUFFIXES = {".pdf"}


def is_url(target: str) -> bool:
    return target.startswith(("https://", "http://"))


def is_claude_artifact(target: str) -> bool:
    """claude.ai refuses WebKitGTK ("This browser isn't supported"), so these show a local snapshot."""
    return target.startswith(("https://claude.ai/artifact/", "https://claude.ai/code/artifact/"))


def artifact_snapshot(url: str) -> Path:
    """Where the artifact hook keeps a copy of the published HTML, so the widget renders it in place."""
    return state_dir() / "artifacts" / f"{url.rstrip('/').rsplit('/', 1)[-1]}.html"


def reviewable(target: str | Path) -> bool:
    target = str(target)
    return is_url(target) or Path(target).suffix.lower() in (
        IMAGE_SUFFIXES | VIDEO_SUFFIXES | HTML_SUFFIXES | PDF_SUFFIXES)


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "review-queue"


def manifest_path() -> Path:
    return state_dir() / "queue.json"


@dataclass
class Entry:
    path: str
    title: str
    pushed_at: str
    read: bool = False
    note: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load(path: Path | None = None) -> list[Entry]:
    p = path or manifest_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    out: list[Entry] = []
    for item in raw.get("entries", []) if isinstance(raw, dict) else []:
        if not isinstance(item, dict) or not item.get("path") or not reviewable(item["path"]):
            continue
        out.append(Entry(
            path=str(item["path"]),
            title=str(item.get("title") or Path(item["path"]).name),
            pushed_at=str(item.get("pushed_at") or ""),
            read=bool(item.get("read", False)),
            note=str(item.get("note") or ""),
        ))
    return out[:MAX_ENTRIES]


def save(entries: list[Entry], path: Path | None = None) -> None:
    p = path or manifest_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "entries": [asdict(e) for e in entries[:MAX_ENTRIES]]}
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".queue-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def push(file: str | Path, title: str | None = None, note: str = "", path: Path | None = None) -> list[Entry]:
    """Add a file or web URL to the front (newest first). Re-pushing moves it to the front, unread."""
    if is_url(str(file)):
        target = str(file)
    else:
        resolved = Path(file).expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"not a file: {resolved}")
        target = str(resolved)
    if not reviewable(target):
        raise ValueError(f"not reviewable (images, videos, PDFs, .html, or a URL only): {target}")
    entries = [e for e in load(path) if e.path != target]
    entries.insert(0, Entry(path=target, title=title or Path(target).name or target,
                            pushed_at=_now(), read=False, note=note))
    entries = entries[:MAX_ENTRIES]
    save(entries, path)
    return entries


def archive(target: str, path: Path | None = None) -> bool:
    """Hide an entry from the widget. The file stays on disk; archive.json keeps the
    record so `rq push <path>` can bring it back. Returns whether anything was archived."""
    p = path or manifest_path()
    entries = load(p)
    gone = [e for e in entries if e.path == target]
    if not gone:
        return False
    save([e for e in entries if e.path != target], p)
    log = p.parent / "archive.json"
    try:
        archived = json.loads(log.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        archived = []
    archived.insert(0, {**asdict(gone[0]), "archived_at": _now()})
    # ponytail: plain write, not atomic; it is a recovery log, the queue itself stays atomic.
    log.write_text(json.dumps(archived[:500], indent=1), encoding="utf-8")
    return True


def _archive_log(path: Path | None = None) -> Path:
    return (path or manifest_path()).parent / "archive.json"


def load_archive(path: Path | None = None) -> list[Entry]:
    """Archived entries, newest archive first, for the widget's Archived view."""
    try:
        raw = json.loads(_archive_log(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    fields = Entry.__dataclass_fields__
    return [Entry(**{k: v for k, v in item.items() if k in fields})
            for item in raw if isinstance(item, dict) and item.get("path") and reviewable(item["path"])]


def unarchive(target: str, path: Path | None = None) -> bool:
    """Put an archived entry back at the front of the queue. Returns whether it was found."""
    log = _archive_log(path)
    try:
        archived = json.loads(log.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    hit = next((a for a in archived if isinstance(a, dict) and a.get("path") == target), None)
    if hit is None:
        return False
    fields = Entry.__dataclass_fields__
    entry = Entry(**{k: v for k, v in hit.items() if k in fields})
    save([entry] + [e for e in load(path) if e.path != target], path)
    log.write_text(json.dumps([a for a in archived if a is not hit], indent=1), encoding="utf-8")
    return True


def mark_read(file_path: str, path: Path | None = None) -> None:
    entries = load(path)
    changed = False
    for e in entries:
        if e.path == file_path and not e.read:
            e.read = True
            changed = True
    if changed:
        save(entries, path)


def unread_count(path: Path | None = None) -> int:
    return sum(1 for e in load(path) if not e.read)


def newest_unread_index(entries: list[Entry]) -> int:
    for i, e in enumerate(entries):
        if not e.read:
            return i
    return 0
