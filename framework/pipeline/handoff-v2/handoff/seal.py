"""Seal a transcript segment and enqueue it. Runs inside a hook, so milliseconds.

The whole read-cursor -> boundary -> write -> rename -> advance sequence is held
under a per-transcript lock. The worker's single-consumer lock protects the
*consumer*, not concurrent enqueuers, and SessionEnd + PreCompact can fire
together on the same transcript (PLAN.md B).
"""
import os
import subprocess
import time
from pathlib import Path

from . import config as C
from . import prefilter, resolve, store


def transcript_key(path: str) -> str:
    p = Path(path)
    return f"{p.parent.name}/{p.stem}"


def repo_fingerprint(cwd: str | None) -> dict:
    """HEAD plus index/worktree hashes, captured at SEAL time.

    Captured later it would bless source changes made after the segment, and a
    boolean dirty flag cannot tell one dirty tree from another (PLAN.md B/K).
    On any failure this records `unknown`, which makes the brief withhold rather
    than present itself as fresh.
    """
    if not cwd or not Path(cwd).is_dir():
        return {"state": "unknown", "reason": "no cwd"}

    def git(*args, timeout=5):
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                              text=True, timeout=timeout)
    try:
        head = git("rev-parse", "HEAD")
        if head.returncode != 0:
            return {"state": "unknown", "reason": "not a git repo"}
        status = git("status", "--porcelain")
        diff = git("diff", "HEAD")
        import hashlib
        tree_hash = hashlib.sha256(
            (status.stdout + "\0" + diff.stdout).encode(errors="replace")
        ).hexdigest()[:24]
        branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "?"
        return {
            "state": "ok",
            "head": head.stdout.strip(),
            "branch": branch,
            "tree_hash": tree_hash,
            "dirty": bool(status.stdout.strip()),
        }
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"state": "unknown", "reason": str(exc)[:120]}


def _spool(tp: Path, start: int, end: int, jid: str) -> bool:
    """Copy [start, end) out of the live transcript before the job is visible, so
    the drain reads bytes that cannot change: a transcript rewritten, truncated
    or pruned after the seal no longer tears or loses the segment (audit
    2026-10-12, F1). copy_file_range shares extents on btrfs, so this stays
    cheap inside a hook. On failure the job reads the transcript, as before."""
    dest = store.spool_path(jid)
    tmp = dest.with_name(f".tmp-{dest.name}")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(tp, "rb") as src, open(tmp, "wb") as dst:
            off, left = start, end - start
            while left > 0:
                n = os.copy_file_range(src.fileno(), dst.fileno(), left, off)
                if n <= 0:
                    raise OSError(f"short copy at {off}")
                off, left = off + n, left - n
            os.fsync(dst.fileno())
        os.replace(tmp, dest)
        return True
    except OSError:
        tmp.unlink(missing_ok=True)
        return False


def seal(transcript_path: str, session_id: str, cwd: str | None, source: str,
         record_source: str | None = None) -> dict:
    """Seal [sealed_offset, last_complete_record) and enqueue it.

    Returns a status dict; never raises into the hook.
    """
    record_source = record_source or ("codex" if source == "codex" else "claude")
    if record_source not in ("claude", "codex"):
        return {"status": "invalid-source", "source": record_source}
    tp = Path(transcript_path)
    if not tp.exists():
        return {"status": "no-transcript", "path": str(tp)}

    key = transcript_key(str(tp))
    try:
        with store.seal_lock(key):
            st = store.get_transcript_state(key)
            try:
                inode = os.stat(tp).st_ino
            except OSError as exc:
                return {"status": "stat-failed", "error": str(exc)}

            if st.get("inode") not in (None, inode):
                # Rotation or replacement: restart the cursor rather than reading
                # across two different files.
                st = {"inode": inode, "acked_offset": 0, "sealed_offset": 0,
                      "mtime_ns": 0, "prefix_hash": "", "completed": []}

            start = int(st.get("sealed_offset") or 0)
            size = os.path.getsize(tp)
            end = prefilter.last_record_boundary(tp, start, size)
            if end <= start:
                return {"status": "nothing-new", "offset": start}

            jid = store.job_id(key, inode, start, end)
            spooled = _spool(tp, start, end, jid)
            job = {
                "job_id": jid,
                "transcript_key": key,
                "transcript_path": str(tp),
                "session_id": session_id,
                "cwd": cwd,
                "inode": inode,
                "start_offset": start,
                "end_offset": end,
                "sealed_ns": time.time_ns(),
                "source": source,
                "record_source": record_source,
                "attempts": 0,
                "next_attempt_at": 0,
                "repo_fingerprint": repo_fingerprint(cwd),
                "slug_hint": resolve.resolve(cwd, [])[0],
                **({"spool_bytes": end - start} if spooled else {}),
            }
            admitted, path = store.enqueue(job)

            st["inode"] = inode
            st["sealed_offset"] = end
            st["mtime_ns"] = os.stat(tp).st_mtime_ns
            store.put_transcript_state(key, st)

        return {"status": "sealed" if admitted else "quarantined",
                "job_id": jid, "bytes": end - start, "path": path}
    except TimeoutError as exc:
        return {"status": "lock-busy", "error": str(exc)}


def reconcile() -> list[dict]:
    """Enqueue any transcript that has grown beyond its sealed cursor.

    This - not SessionEnd - is what recovers a SIGKILLed session, so it compares
    offsets and identity, never mtime (PLAN.md B).
    """
    out = []
    if not C.CLAUDE_PROJECTS.is_dir():
        return out
    for d in C.CLAUDE_PROJECTS.iterdir():
        if not d.is_dir() or d.name.startswith("_"):
            continue
        for tp in d.glob("*.jsonl"):
            key = transcript_key(str(tp))
            st = store.get_transcript_state(key)
            try:
                size = os.path.getsize(tp)
                inode = os.stat(tp).st_ino
            except OSError:
                continue
            if st.get("inode") == inode and size <= int(st.get("sealed_offset") or 0):
                continue
            records = prefilter.read_records(tp, 0, min(size, 200_000), None)
            cwd = resolve.cwd_from_records(records)
            # Backfilling history: a transcript from outside AI_Projects with no
            # catalog match (home dir, scratch) is noise - skip it rather than
            # spend thor time. A session STARTED at ~/AI_Projects is in scope:
            # its project is decided later from what it touched.
            if not resolve.in_scope(cwd):
                continue
            res = seal(str(tp), tp.stem, cwd, source="reconcile")
            if res.get("status") in ("sealed", "quarantined"):
                out.append({"transcript": key, **res})
    return out
