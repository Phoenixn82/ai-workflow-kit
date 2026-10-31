"""Durable local state: locks, per-transcript cursors, and the segment queue.

The queue - not SessionEnd - is the durability guarantee (PLAN.md B). Three
properties this module exists to enforce:

1. Sealing holds a per-transcript lock across read-cursor -> boundary -> rename ->
   advance, because the single-consumer worker lock does not protect concurrent
   *enqueuers* (SessionEnd and PreCompact can fire together).
2. Per-transcript FIFO, head-of-line: only the earliest unacked segment of a
   transcript is eligible, and the acked cursor advances only through a
   contiguous run of successes.
3. Retention never deletes unacknowledged work; overflow is quarantined and
   re-admitted FIFO in age-preserving order.
4. (audit 2026-10-12, F1) A drain killed mid-job leaves a claim the next drain
   turns back into a queued job; outages defer without spending attempts; only
   MAX_ATTEMPTS counted failures move a job to FAILED, which is never re-admitted.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path

from . import config as C


# ---------------------------------------------------------------- locks

@contextlib.contextmanager
def file_lock(name: str, timeout: float = 30.0):
    """Exclusive lock keyed by name. Raises TimeoutError rather than hanging."""
    safe = hashlib.sha256(name.encode()).hexdigest()[:32]
    path = C.LOCKS / f"{safe}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"lock busy: {name}")
                time.sleep(0.05)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def seal_lock(transcript_key: str):
    return file_lock(f"seal:{transcript_key}")


def project_lock(slug: str):
    return file_lock(f"project:{slug}")


def consumer_lock():
    return file_lock("consumer", timeout=0.1)


def cursor_lock():
    """transcripts.json holds every transcript's cursor in one file, so the seal
    lock on one key did not stop a writer of another key from replacing the file
    with its stale copy (audit 2026-10-12: lost cursors). Always the innermost lock."""
    return file_lock("cursors")


# ---------------------------------------------------- per-transcript state

def _load_state() -> dict:
    return C.read_json(C.TRANSCRIPTS_STATE, default={}) or {}


def get_transcript_state(key: str) -> dict:
    return _load_state().get(key, {
        "inode": None,
        "acked_offset": 0,
        "sealed_offset": 0,
        "mtime_ns": 0,
        "prefix_hash": "",
        "completed": [],          # [[start, end], ...] acked but not yet contiguous
    })


def put_transcript_state(key: str, st: dict) -> None:
    """Caller must hold the seal lock for `key`."""
    with cursor_lock():
        all_state = _load_state()
        all_state[key] = st
        C.write_json(C.TRANSCRIPTS_STATE, all_state)


def all_transcript_states() -> dict:
    return _load_state()


# ------------------------------------------------------------- job ids

def job_id(transcript_key: str, inode: int, start: int, end: int) -> str:
    """Deterministic, so a replayed segment produces the same id and renderers
    stay idempotent against it (PLAN.md B, H)."""
    raw = f"{transcript_key}|{inode}|{start}|{end}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


# -------------------------------------------------------------- queue

# ponytail: lives here, not in config.py, while config.py has another editor.
MAX_ATTEMPTS = 6                       # counted failures (bad output, broken job, crashes)
FAILED = C.QUARANTINE / "failed"       # a subdir, so readmit_from_quarantine never sees it
SPOOL = C.STATE / "spool"              # sealed byte ranges, one file per queued job


def spool_path(jid: str) -> Path:
    return SPOOL / f"{jid}.jsonl"


def segment_source(job: dict) -> tuple[Path, int, int, int | None]:
    """(path, start, end, inode) to read a job's records from. The spool copy when
    it is whole; otherwise the live transcript (jobs sealed before the spool, or a
    spool that failed to write)."""
    size = job["end_offset"] - job["start_offset"]
    sp = spool_path(job["job_id"])
    if job.get("spool_bytes") == size and sp.is_file() and sp.stat().st_size == size:
        return sp, 0, size, None
    return Path(job["transcript_path"]), job["start_offset"], job["end_offset"], job.get("inode")


def _queue_files() -> list[Path]:
    return sorted(C.QUEUE.glob("*.json"))


def queue_depth() -> int:
    return len(_queue_files())


def enqueue(job: dict) -> tuple[bool, str]:
    """Admit a job, or quarantine it when the ceiling is reached.

    Returns (admitted, path). Quarantine preserves the seal timestamp in the
    filename so re-admission is age-preserving.
    """
    name = f"{job['sealed_ns']}-{job['job_id']}.json"
    # By job id, not filename: a reseal after a crash between enqueue and the
    # cursor save has a new sealed_ns but the same deterministic id.
    for d in (C.QUEUE, C.QUARANTINE, FAILED):
        hit = next(d.glob(f"*-{job['job_id']}.json"), None)
        if hit:
            return d is C.QUEUE, str(hit)
    if _result_exists(job["job_id"]):
        return True, str(C.QUEUE / name)        # already done
    target_dir = C.QUEUE if queue_depth() < C.QUEUE_ADMISSION_CEILING else C.QUARANTINE
    path = target_dir / name
    C.write_json(path, job)
    return target_dir is C.QUEUE, str(path)


def readmit_from_quarantine() -> int:
    """FIFO, age-preserving re-admission once capacity returns (PLAN.md I)."""
    moved = 0
    for path in sorted(C.QUARANTINE.glob("*.json")):
        if queue_depth() >= C.QUEUE_ADMISSION_CEILING:
            break
        job = C.read_json(path)
        if job is None:
            continue
        C.write_json(C.QUEUE / path.name, job)
        path.unlink(missing_ok=True)
        moved += 1
    return moved


def claim_next_job(now: float | None = None) -> dict | None:
    """Earliest eligible segment, respecting per-transcript head-of-line order.

    A transcript's later segments are never processed while an earlier one is
    still unacked - an out-of-order retry would corrupt RESUME ordering.
    """
    now = time.time() if now is None else now
    by_transcript: dict[str, dict] = {}
    for path in _queue_files():
        job = C.read_json(path)
        if job is None:
            continue
        job["_path"] = str(path)
        key = job["transcript_key"]
        head = by_transcript.get(key)
        if head is None or job["start_offset"] < head["start_offset"]:
            by_transcript[key] = job

    eligible = [j for j in by_transcript.values() if j.get("next_attempt_at", 0) <= now]
    if not eligible:
        return None
    eligible.sort(key=lambda j: j["sealed_ns"])
    return eligible[0]


def _record(job: dict) -> dict:
    return {k: v for k, v in job.items() if k not in ("_path", "claim")}


def _quarantine(path: Path, job: dict, why: str) -> None:
    job["quarantined"] = {"at": time.time(), "reason": why[:300]}
    FAILED.mkdir(parents=True, exist_ok=True)
    C.write_json(FAILED / path.name, job)        # the spool stays: the job can be requeued
    path.unlink(missing_ok=True)


def claim_job(job: dict) -> None:
    """Mark the job in progress. Only a drain holding consumer_lock() claims, so a
    claim seen by the next lock holder belongs to a process that died mid-job."""
    job["claim"] = {"pid": os.getpid(), "at": time.time()}
    C.write_json(Path(job["_path"]), {k: v for k, v in job.items() if k != "_path"})


def recover_claims() -> list[str]:
    """Crash reconcile: every claim back to queued. Call only while holding
    consumer_lock() (the drain does, first thing), which is what proves the
    claimant dead; the flock dies with its process, a PID can be reused. The
    attempt budget is untouched, but MAX_ATTEMPTS crashes on one job quarantine
    it, so a job that kills the drain cannot loop forever."""
    out = []
    for path in _queue_files():
        job = C.read_json(path)
        if not job or "claim" not in job:
            continue
        pid = job.pop("claim").get("pid")
        job["crashes"] = job.get("crashes", 0) + 1
        job["last_error"] = f"drain pid {pid} died mid-job"
        if job["crashes"] >= MAX_ATTEMPTS:
            _quarantine(path, job, f"{job['crashes']} drains died on this job")
            out.append(f"QUARANTINE {job['job_id']}: {job['crashes']} drains died on it")
        else:
            C.write_json(path, job)
            out.append(f"recover {job['job_id']}: drain pid {pid} died mid-job; queued again")
    return out


def defer_job(job: dict, error: str, outage: bool = False) -> str:
    """Record a failure with exponential backoff; never deletes.

    An outage (thor or Claude unreachable, usage limit, network, model timeout)
    is not the job's fault: it backs off without spending the attempt budget
    (a long outage would otherwise burn every attempt on timeouts). Any other failure
    counts, and the MAX_ATTEMPTS-th moves the job to FAILED. Returns
    "deferred" or "quarantined"."""
    path = Path(job["_path"])
    job = _record(job)
    counter = "deferrals" if outage else "attempts"
    job[counter] = job.get(counter, 0) + 1
    job.setdefault("attempts", 0)
    job["last_error"] = error[:500]
    job["last_error_class"] = "outage" if outage else "failure"
    job["next_attempt_at"] = time.time() + min(600, 5 * (2 ** min(job[counter], 7)))
    if not outage and job["attempts"] >= MAX_ATTEMPTS:
        _quarantine(path, job, error)
        return "quarantined"
    C.write_json(path, job)
    return "deferred"


def ack_job(job: dict) -> None:
    """Retire a completed segment and advance the contiguous acked cursor."""
    key = job["transcript_key"]
    with seal_lock(key):
        st = get_transcript_state(key)
        acked = st.get("acked_offset", 0)
        completed = {tuple(x) for x in st.get("completed", [])}
        if job["end_offset"] > acked:          # a replayed ack after a crash adds nothing
            completed.add((job["start_offset"], job["end_offset"]))
        advancing = True
        while advancing:                       # advance only through a contiguous run
            advancing = False
            for start, end in sorted(completed):
                if start == acked:
                    acked = end
                    completed.discard((start, end))
                    advancing = True
                    break
        st["acked_offset"] = acked
        st["completed"] = [list(x) for x in sorted(completed)]
        put_transcript_state(key, st)

    path = Path(job["_path"])
    if path.exists():
        C.write_json(C.ARCHIVE / path.name, _record(job))
        path.unlink(missing_ok=True)
    spool_path(job["job_id"]).unlink(missing_ok=True)


# ------------------------------------------------------- result records

def _result_path(jid: str) -> Path:
    return C.RESULTS / f"{jid}.json"


def _result_exists(jid: str) -> bool:
    return _result_path(jid).exists()


def commit_result(jid: str, result: dict) -> None:
    """The single transactional commit point (PLAN.md B).

    RESUME, journal, candidates and brief are separate files, so a crash between
    them would diverge. One validated record lands atomically first; every
    renderer is then idempotent against this job id.
    """
    C.write_json(_result_path(jid), result)


def load_result(jid: str) -> dict | None:
    return C.read_json(_result_path(jid))


def prune_acked(now: float | None = None) -> int:
    """Retention applies to ACKED work only. Unacknowledged jobs are never
    expired - a long thor outage must not become silent data loss (PLAN.md I).
    """
    now = time.time() if now is None else now
    cutoff = now - C.ACKED_RETENTION_DAYS * 86400
    removed = 0
    # ARCHIVE (small job metadata) is kept forever: `refile` rebuilds from it,
    # and pruning it loses the history of every job it removes.
    for d in (C.RESULTS,):
        for path in d.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                pass
    # A spool whose job is gone (sealed but enqueue found it done, or a crash
    # between the two). A day old, so a seal in flight is never swept.
    live = {p.stem.split("-", 1)[-1] for d in (C.QUEUE, C.QUARANTINE, FAILED) for p in d.glob("*.json")}
    for path in SPOOL.glob("*.jsonl"):
        try:
            if path.stem not in live and path.stat().st_mtime < now - 86400:
                path.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def queue_status(now: float | None = None) -> dict:
    """Counts and ages (from the seal) of queued / in-progress / deferred /
    quarantined jobs, plus admission overflow. Read-only."""
    now = time.time() if now is None else now
    jobs = {b: [] for b in ("queued", "in_progress", "deferred", "quarantined", "overflow")}

    def alive(pid) -> bool:
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, TypeError, ValueError):
            return False

    for bucket, d in (("queue", C.QUEUE), ("quarantined", FAILED), ("overflow", C.QUARANTINE)):
        for path in sorted(d.glob("*.json")) if d.is_dir() else []:
            j = C.read_json(path) or {}
            row = {"job_id": j.get("job_id", path.stem), "age_s": now - j.get("sealed_ns", 0) / 1e9,
                   "attempts": j.get("attempts", 0), "deferrals": j.get("deferrals", 0),
                   "crashes": j.get("crashes", 0), "error": j.get("last_error", ""),
                   "error_class": j.get("last_error_class", ""), "path": str(path)}
            if bucket != "queue":
                b = bucket
            elif "claim" in j:
                b = "in_progress"
                row.update(pid=j["claim"].get("pid"), alive=alive(j["claim"].get("pid")))
            elif row["attempts"] or row["deferrals"] or row["crashes"]:
                b = "deferred"
                row["retry_in_s"] = max(0.0, j.get("next_attempt_at", 0) - now)
            else:
                b = "queued"
            jobs[b].append(row)
    out = {b: {"count": len(rows), "oldest_s": max((r["age_s"] for r in rows), default=0),
               "newest_s": min((r["age_s"] for r in rows), default=0)} for b, rows in jobs.items()}
    out["jobs"] = jobs
    return out
