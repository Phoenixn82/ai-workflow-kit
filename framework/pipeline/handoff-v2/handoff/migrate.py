"""One-shot, resumable migration of legacy vault and harness-memory notes.

This module deliberately keeps its own small journal: model answers are facts
only after they have landed in ``state/migrate``.  That makes an interrupted
run cheap to resume and, more importantly, means ``--apply`` never needs to
ask the model again.
"""
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from . import config as C
from . import distill, redact, store


SOURCE_FILES = ("decisions.md", "errors.md", "tokens.md", "prefs.md", "history.md")
LABELS = ("durable", "stale", "duplicate", "narrative")
BUCKETS = ("KEEP", "RETIRE", "MERGE_VAULT", "MERGE_ACTIONS")

ENTRY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "label": {"type": "string", "enum": list(LABELS)},
        "keep_text": {"type": "string", "maxLength": 400},
        "why": {"type": "string", "maxLength": 120},
        "cross_project": {"type": "boolean"},
    },
    "required": ["label", "keep_text", "why", "cross_project"],
}
MEMORY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "bucket": {"type": "string", "enum": list(BUCKETS)},
        "why": {"type": "string", "maxLength": 120},
        "keep_text": {"type": "string", "maxLength": 400},
    },
    "required": ["bucket", "why", "keep_text"],
}


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def split_entries(text: str) -> list[dict]:
    """Split at level-two/three headings, retaining a leading preamble.

    A preamble belongs to the following first entry; a heading-less file is one
    entry.  This avoids silently discarding prose before the first heading.
    """
    matches = list(re.finditer(r"^(#{2,3})\s+(.+?)\s*$", text, re.M))
    if not matches:
        return [{"heading": "(entire file)", "text": text}]
    out = []
    for i, match in enumerate(matches):
        level = len(match.group(1))
        end = len(text)
        for nxt in matches[i + 1:]:
            if len(nxt.group(1)) <= level:
                end = nxt.start()
                break
        start = match.start()
        if i == 0:
            start = 0
        out.append({"heading": match.group(2).strip(), "text": text[start:end]})
    return out


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_/-]{2,}", text.lower())


def _trigrams(text: str) -> set[tuple[str, str, str]]:
    words = _words(text)
    return {tuple(words[i:i + 3]) for i in range(max(0, len(words) - 2))}


def _duplicate_by_trigrams(text: str, reference_texts: list[str]) -> bool:
    grams = _trigrams(text)
    if not grams:
        return False
    for reference in reference_texts:
        overlap = len(grams & _trigrams(reference)) / len(grams)
        if overlap >= 0.60:
            return True
    return False


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def memory_root() -> Path:
    """Use the copied vault during proof/tests, the harness symlink normally."""
    if os.environ.get("HANDOFF_VAULT"):
        return C.VAULT / "harness-memory"
    return Path.home() / ".claude" / "projects" / "-home-you-AI-Projects" / "memory"


def _references() -> list[str]:
    roots = [memory_root(), C.VAULT / "Actions"]
    return [_read(p) for root in roots if root.is_dir() for p in root.glob("*.md")]


def _cache_path(slug: str, name: str) -> Path:
    return C.STATE / "migrate" / slug / f"{name}.jsonl"


def _cached(slug: str, name: str) -> dict[str, dict]:
    out = {}
    path = _cache_path(slug, name)
    if not path.exists():
        return out
    for line in _read(path).splitlines():
        try:
            row = json.loads(line)
            if row.get("hash"):
                out[row["hash"]] = row
        except json.JSONDecodeError:
            continue
    return out


def _cache(slug: str, name: str, row: dict) -> None:
    path = _cache_path(slug, name)
    with store.file_lock(f"migrate:{slug}:{name}"):
        existing = _cached(slug, name)
        if row["hash"] in existing:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def _valid_keep(text: object) -> str:
    text = str(text or "").strip()
    if len(text) < 20 or len(text) > 400:
        return ""
    # A lone path or URL is not a durable fact.
    if re.fullmatch(r"(?:https?://\S+|/?[\w.~/-]+(?:\.[\w-]+)?)", text):
        return ""
    return text


def _parse(raw: str) -> dict:
    return distill._parse_json(raw) or {}  # generate() is schema constrained; retain defensive parsing.


def _entry_prompt(slug: str, filename: str, text: str, key_terms: str) -> str:
    return f"""Classify this old vault entry. Return JSON only.
Project: {slug}
File: {filename}
Labels: durable = still-true fact future agent needs and cannot re-derive from code/git;
stale = retired tools/paths/eras, closed loops, or superseded decisions;
duplicate = already in harness memory or Actions; narrative = chronology git records.
Set cross_project true only for durable facts with no project-specific path or component.
keep_text must be a rewrite, not a quote, in at most two sentences; it is empty unless durable.
Harness/Actions key terms for duplicate judgement: {key_terms[:1200]}

ENTRY:
{text}"""


def _classify_entry(slug: str, filename: str, entry: dict, refs: list[str]) -> tuple[dict, int, float]:
    digest = _hash(entry["text"])
    cached = _cached(slug, filename).get(digest)
    if cached:
        return cached, 0, 0.0
    redacted, _ = redact.redact(entry["text"])
    key_terms = " ".join(_words(entry["text"])[:100])
    raw, stats = distill.generate(_entry_prompt(slug, filename, redacted, key_terms),
                                  schema=ENTRY_SCHEMA, max_output=180, timeout=300)
    answer = _parse(raw)
    label = answer.get("label") if answer.get("label") in LABELS else "stale"
    if _duplicate_by_trigrams(entry["text"], refs):
        label = "duplicate"
    row = {
        "hash": digest, "slug": slug, "file": filename,
        "entry_index": entry.get("index", 0), "heading": entry["heading"],
        "bytes": len(entry["text"].encode("utf-8")),
        "label": label, "keep_text": _valid_keep(answer.get("keep_text")) if label == "durable" else "",
        "why": str(answer.get("why") or "")[:120],
        "cross_project": bool(answer.get("cross_project")) if label == "durable" else False,
        "duration_s": float(stats.get("duration_s") or 0),
    }
    _cache(slug, filename, row)
    return row, 1, float(stats.get("duration_s") or 0)


def _project_slugs(project: str | None) -> list[str]:
    if project:
        return [project] if (C.PROJECTS_DIR / project).is_dir() else []
    if not C.PROJECTS_DIR.is_dir():
        return []
    return sorted(p.name for p in C.PROJECTS_DIR.iterdir() if p.is_dir() and not p.name.startswith("_"))


def _git_clean() -> tuple[bool, bool]:
    """Return (can_apply, is_git); a non-git copy uses os.replace safely."""
    try:
        probe = subprocess.run(["git", "-C", str(C.VAULT), "rev-parse", "--is-inside-work-tree"],
                               capture_output=True, text=True, timeout=10)
        if probe.returncode:
            return True, False
        status = subprocess.run(["git", "-C", str(C.VAULT), "status", "--porcelain"],
                                capture_output=True, text=True, timeout=10)
        # The Codex shared-activity log is rewritten by a PostToolUse hook on
        # every tool call; it is transient by design and must not veto a
        # migration that spent an hour classifying.
        TRANSIENT = (".claude-codex-log.md",)
        dirty = [ln for ln in status.stdout.splitlines()
                 if ln.strip() and not ln[3:].strip().endswith(TRANSIENT)]
        if dirty:
            print("migrate: refusing --apply: vault has uncommitted changes:")
            for ln in dirty[:10]:
                print("   ", ln)
            return False, True
        return True, True
    except (OSError, subprocess.TimeoutExpired):
        return True, False


def _move(source: Path, destination: Path, is_git: bool) -> None:
    if not source.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    moved = False
    if is_git:
        try:
            moved = subprocess.run(["git", "-C", str(C.VAULT), "mv", str(source), str(destination)],
                                   capture_output=True, timeout=20).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            moved = False
    if not moved:
        os.replace(source, destination)


def _append_notes(path: Path, heading: str, items: list[tuple[str, str]]) -> None:
    if not items:
        return
    existing = _read(path)
    add = [f"- {text}  <!-- id:{ident} -->" for ident, text in items if ident not in existing]
    if not add:
        return
    prefix = "" if existing else f"# {path.parent.name} - notes\n\n"
    C.atomic_write(path, existing + prefix + f"## {heading}\n" + "\n".join(add) + "\n")


def _print_part_a(rows: list[dict]) -> None:
    print("PROJECT                         entries durable stale duplicate narrative  bytes in  bytes kept")
    grouped = {}
    for row in rows:
        grouped.setdefault(row["slug"], []).append(row)
    totals = {label: 0 for label in LABELS}
    bytes_in = bytes_kept = 0
    for slug, items in grouped.items():
        counts = {label: sum(r["label"] == label for r in items) for label in LABELS}
        incoming = sum(r["bytes"] for r in items)
        kept = sum(len(r["keep_text"].encode()) for r in items if r["label"] == "durable")
        print(f"{slug:30} {len(items):7} {counts['durable']:7} {counts['stale']:5} {counts['duplicate']:9} {counts['narrative']:9} {incoming:9} {kept:11}")
        for row in [r for r in items if r["keep_text"]][:3]:
            print(f"  sample: {row['keep_text']}")
        for label in LABELS:
            totals[label] += counts[label]
        bytes_in += incoming
        bytes_kept += kept
    print(f"TOTAL: {len(rows)} entries, {totals['durable']}/{totals['stale']}/{totals['duplicate']}/{totals['narrative']}, {bytes_in} -> {bytes_kept} bytes")


def run_projects(apply: bool = False, project: str | None = None, resume: bool = False) -> tuple[int, list[dict], int, float]:
    del resume  # cached content hashes are always reused; the flag documents the intent.
    rows, calls, seconds = [], 0, 0.0
    refs = _references()
    unavailable = False
    for slug in _project_slugs(project):
        directory = C.PROJECTS_DIR / slug
        for filename in SOURCE_FILES:
            source = directory / filename
            if not source.exists():
                continue
            for index, entry in enumerate(split_entries(_read(source))):
                entry["index"] = index
                try:
                    row, used, elapsed = _classify_entry(slug, filename, entry, refs)
                except distill.ThorUnavailable:
                    unavailable = True
                    break
                rows.append(row); calls += used; seconds += elapsed
            if unavailable:
                break
        if unavailable:
            break
    _print_part_a(rows)
    if unavailable:
        total = sum(len(split_entries(_read(C.PROJECTS_DIR / slug / filename)))
                    for slug in _project_slugs(project) for filename in SOURCE_FILES
                    if (C.PROJECTS_DIR / slug / filename).exists())
        print(f"migrate: thor unavailable; {max(0, total - len(rows))} entries remain")
        return 0, rows, calls, seconds
    if apply:
        allowed, is_git = _git_clean()
        if not allowed:
            return 0, rows, calls, seconds
        # Labels may predate new source contents, so calculate coverage from this run.
        expected = sum(len(split_entries(_read((C.PROJECTS_DIR / s / f))))
                       for s in _project_slugs(project) for f in SOURCE_FILES
                       if (C.PROJECTS_DIR / s / f).exists())
        if expected and len(rows) / expected < .80:
            print("migrate: refusing --apply: fewer than 80% of entries are labelled")
            return 0, rows, calls, seconds
        for slug in _project_slugs(project):
            with store.project_lock(slug):
                for filename in SOURCE_FILES:
                    selected = [r for r in rows if r["slug"] == slug and r["file"] == filename and r["keep_text"]]
                    # An entry came FROM this project's file: it belongs here,
                    # whatever the model said about generality. An 8B model can flag
                    # nearly every entry cross_project, which would empty every
                    # per-project NOTES.md into one blob.
                    local = [("m-" + r["hash"][:8], r["keep_text"]) for r in selected]
                    _append_notes(C.PROJECTS_DIR / slug / "NOTES.md", f"Migrated from {filename} ({_today()})", local)
                    _move(C.PROJECTS_DIR / slug / filename, C.PROJECTS_DIR / slug / "_archive" / filename, is_git)
        # _shared gets a COPY of the generic subset only: the model's flag AND a
        # source type the audit found carries cross-project lessons (tokens.md,
        # prefs.md), AND no mention of the project's own name.
        def _generic(r):
            if not r["cross_project"] or r["file"] not in ("tokens.md", "prefs.md"):
                return False
            slug_tokens = {t for t in re.split(r"[-_ ]", r["slug"].lower()) if len(t) > 3}
            return not any(t in r["keep_text"].lower() for t in slug_tokens)
        shared = [("m-" + r["hash"][:8], r["keep_text"]) for r in rows if r["keep_text"] and _generic(r)]
        with store.project_lock("_shared"):
            _append_notes(C.PROJECTS_DIR / "_shared" / "NOTES.md", f"Migrated from project vaults ({_today()})", shared)
    return 0, rows, calls, seconds


def _frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = re.search(r"^---\s*$", text[3:], re.M)
    if not end:
        return {}
    header = text[3:end.start() + 3]
    values, section = {}, ""
    for line in header.splitlines():
        if re.match(r"^metadata:\s*$", line):
            section = "metadata"; continue
        match = re.match(r"^(\s*)([\w_-]+):\s*(.*)$", line)
        if match:
            key, value = match.group(2), match.group(3).strip().strip('"')
            values[(section + "." if match.group(1) else "") + key] = value
    return values


def _memory_slug(path: Path, text: str) -> str:
    candidates = {p.name for p in C.PROJECTS_DIR.iterdir() if p.is_dir()} if C.PROJECTS_DIR.is_dir() else set()
    base = re.sub(r"^feedback_", "", path.stem).replace("-", "_")
    if base in candidates:
        return base
    for slug in sorted(candidates, key=len, reverse=True):
        if base.startswith(slug) or slug.replace("_", "-") in path.stem:
            return slug
    match = re.search(r"Projects/([a-z0-9_-]+)/", text)
    return match.group(1) if match and match.group(1) in candidates else "_shared"


def _claude_headings() -> list[str]:
    return re.findall(r"^#{1,6}\s+(.+)$", _read(Path.home() / ".claude" / "CLAUDE.md"), re.M)


def _memory_prompt(path: Path, text: str, meta: dict, slug: str, actions: list[str]) -> str:
    redacted, _ = redact.redact(text)
    return f"""Classify this harness-memory file. Return JSON only.
Bucket meanings: KEEP remains in memory; RETIRE is obsolete; MERGE_VAULT is a durable project fact;
MERGE_ACTIONS is a durable operational action. keep_text is a rewrite in at most two sentences only for MERGE_*.
Hard rule: frontmatter type feedback MUST be KEEP.
File: {path.name}; frontmatter type: {meta.get('metadata.type', '')}; inferred project: {slug}; project exists: {(C.PROJECTS_DIR / slug).is_dir()}.
Actions files: {', '.join(actions)}
CLAUDE.md section headings: {' | '.join(_claude_headings())}

FILE BODY:
{redacted}"""


def _classify_memory(path: Path) -> tuple[dict, int, float]:
    text = _read(path); digest = _hash(text); name = path.name
    cached = _cached("memory", name).get(digest)
    if cached:
        return cached, 0, 0.0
    meta = _frontmatter(text); slug = _memory_slug(path, text)
    actions_dir = C.VAULT / "Actions"
    actions = sorted(p.name for p in actions_dir.glob("*.md")) if actions_dir.is_dir() else []
    raw, stats = distill.generate(_memory_prompt(path, text, meta, slug, actions), schema=MEMORY_SCHEMA,
                                  max_output=180, timeout=300)
    answer = _parse(raw)
    bucket = answer.get("bucket") if answer.get("bucket") in BUCKETS else "KEEP"
    if meta.get("metadata.type") == "feedback":
        bucket = "KEEP"
    row = {"hash": digest, "file": name, "title": meta.get("name", path.stem),
           "description": meta.get("description", ""), "type": meta.get("metadata.type", ""),
           "slug": slug, "bytes": len(text.encode()), "bucket": bucket,
           "why": str(answer.get("why") or "")[:120],
           "duration_s": float(stats.get("duration_s") or 0),
           "keep_text": _valid_keep(answer.get("keep_text")) if bucket.startswith("MERGE_") else ""}
    _cache("memory", name, row)
    return row, 1, float(stats.get("duration_s") or 0)


def _write_triage(rows: list[dict]) -> None:
    # This audit artifact is deliberately in the working repository, not the vault.
    C.atomic_write(C.ROOT / "specs" / "memory-index-triage.json", json.dumps(rows, indent=2, sort_keys=True) + "\n")


def _memory_index(rows: list[dict], header: str, existing: str = "") -> str:
    """KEEP lines reuse the existing MEMORY.md entry verbatim when one exists.

    A hand-compressed index has short lines; regenerating them from
    frontmatter descriptions produces longer lines and eats most of the
    saving. Reuse first, regenerate only for new files."""
    current = {}
    for line in existing.splitlines():
        mt = re.match(r"^- \[[^\]]*\]\(([^)]+)\)", line)
        if mt:
            current[mt.group(1)] = line
    lines = [header]
    for row in rows:
        if row["bucket"] != "KEEP":
            continue
        if row["file"] in current:
            lines.append(current[row["file"]])
            continue
        hook = str(row["description"] or "").replace("\n", " ").strip()[:90]
        lines.append(f"- [{row['title']}]({row['file']}) — {hook}")
    return "\n".join(lines) + "\n"


def run_memory(apply: bool = False, resume: bool = False) -> tuple[int, list[dict], int, float]:
    del resume
    root = memory_root(); rows, calls, seconds = [], 0, 0.0
    unavailable = False
    for path in sorted(root.glob("*.md")) if root.is_dir() else []:
        if path.name == "MEMORY.md":
            continue
        try:
            row, used, elapsed = _classify_memory(path)
        except distill.ThorUnavailable:
            unavailable = True; break
        rows.append(row); calls += used; seconds += elapsed
    counts = {bucket: sum(r["bucket"] == bucket for r in rows) for bucket in BUCKETS}
    existing_index = _read(root / "MEMORY.md") if (root / "MEMORY.md").exists() else ""
    header = existing_index.splitlines()[0] if existing_index else "# Memory index"
    projected = len(_memory_index(rows, header, existing_index).encode()) // 4
    print("MEMORY: KEEP RETIRE MERGE_VAULT MERGE_ACTIONS projected MEMORY.md tokens")
    print(f"        {counts['KEEP']:4} {counts['RETIRE']:6} {counts['MERGE_VAULT']:11} {counts['MERGE_ACTIONS']:13} {projected:26}")
    _write_triage(rows)
    if unavailable:
        print("migrate: thor unavailable; cached classifications were preserved")
        return 0, rows, calls, seconds
    if apply:
        allowed, is_git = _git_clean()
        if not allowed:
            return 0, rows, calls, seconds
        expected = len([p for p in root.glob("*.md") if p.name != "MEMORY.md"])
        if expected and len(rows) / expected < .80:
            print("migrate: refusing --apply: fewer than 80% of memory files are labelled")
            return 0, rows, calls, seconds
        for row in rows:
            source = root / row["file"]
            if row["bucket"] == "KEEP":
                continue
            if row["bucket"] == "MERGE_VAULT":
                # Project-state memories carry IDs, ports and resume commands.
                # A two-sentence rewrite drops exactly that, so the FULL body
                # moves into NOTES.md (never auto-loaded, so size is free).
                slug = row["slug"]
                body = _read(source)
                body = body.split("---", 2)[-1].strip() if body.startswith("---") else body.strip()
                with store.project_lock(slug):
                    _append_notes(C.PROJECTS_DIR / slug / "NOTES.md",
                                  f"{row['title']} (from harness memory, {_today()})",
                                  [("hm-" + row["hash"][:8], body.replace("\n", "\n  "))])
            elif row["bucket"] == "MERGE_ACTIONS" and row["keep_text"]:
                target = C.VAULT / "Actions" / f"{row['slug']}.md"
                if not target.exists():
                    target = C.VAULT / "Actions" / "_from-memory.md"
                _append_notes(target, f"Migrated from harness memory ({_today()})",
                              [("hm-" + row["hash"][:8], row["keep_text"])])
            _move(source, root / "_archive" / row["file"], is_git)
        C.atomic_write(root / "MEMORY.md", _memory_index(rows, header, existing_index))
    return 0, rows, calls, seconds


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    resume = "--resume" in argv
    memory = "--memory" in argv
    project = None
    if "--project" in argv:
        idx = argv.index("--project")
        if idx + 1 >= len(argv):
            print("migrate: --project requires a slug")
            return 2
        project = argv[idx + 1]
    if memory and project:
        print("migrate: --memory and --project cannot be combined")
        return 2
    code, _rows, calls, seconds = (run_memory(apply, resume) if memory
                                   else run_projects(apply, project, resume))
    print(f"THOR CALLS: {calls}, {seconds:.1f} seconds")
    return code
