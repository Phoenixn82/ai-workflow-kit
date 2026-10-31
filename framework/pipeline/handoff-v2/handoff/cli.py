"""Entry points. Hooks call these; none of them do network work in-process
except `drain`, which only ever runs detached (PLAN.md A).
"""
import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import brief, config as C, prefilter, redact, render, resolve, seal, store
# distill/migrate pull in urllib+ssl (~100 ms); imported only by the commands that need them.


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _log(msg: str) -> None:
    path = C.LOGS / "worker.log"
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except OSError:
        lines = []
    lines.append(f"{_now()} {msg}")
    C.atomic_write(path, "\n".join(lines[-500:]) + "\n")      # bounded log


def _hook_input() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _transcript_for(payload: dict) -> str | None:
    """Prefer the hook's transcript_path; fall back to discovery validated
    against the session id embedded in the file itself. The directory name is a
    lossy cwd encoding and the filename alone is not proof (PLAN.md C)."""
    tp = payload.get("transcript_path")
    if tp and Path(tp).exists():
        return tp
    sid = payload.get("session_id")
    if not sid or not C.CLAUDE_PROJECTS.is_dir():
        return None
    for d in C.CLAUDE_PROJECTS.iterdir():
        cand = d / f"{sid}.jsonl"
        if not cand.exists():
            continue
        try:
            recs = prefilter.read_records(cand, 0, 200_000, None)
        except OSError:
            continue
        if any(r.get("sessionId") == sid or r.get("session_id") == sid for r in recs):
            return str(cand)
    return None


# ------------------------------------------------------------------ commands

def cmd_seal(source: str) -> int:
    payload = _hook_input()
    tp = _transcript_for(payload)
    if not tp:
        return 0
    res = seal.seal(tp, payload.get("session_id", "unknown"), payload.get("cwd"), source)
    _log(f"seal({source}) {res}")
    # SessionEnd detaches a worker so the resume exists soon after the session
    # ends. PreCompact deliberately does not: the session is still running and
    # its later segments would queue behind a mid-session distillation.
    if source == "session-end" and res.get("status") == "sealed":
        _spawn_worker()
    return 0


def cmd_start(inject_only: bool = False) -> int:
    """Local only. Spawns the detached worker, then injects. No network here -
    vault-sync fetches can hang during an outage and this runs before the first
    prompt.

    inject_only is the SubagentStart path: subagents otherwise spend
    many vault-fetch tokens sweeping the vault to orient. Hand
    them the same pre-built brief the parent got, and nothing else - no seal,
    no worker, no diagnostic line.
    """
    payload = _hook_input()
    if inject_only:
        out = brief.inject(payload.get("cwd"), "startup")
        if out:
            print(out)
        return 0
    tp = _transcript_for(payload)
    if tp:
        seal.seal(tp, payload.get("session_id", "unknown"), payload.get("cwd"), "session-start")
    store.readmit_from_quarantine()
    if store.queue_depth():            # an idle project must not trigger a network pull
        _spawn_worker()
    out = brief.inject(payload.get("cwd"), payload.get("source", "startup"))
    print(brief.diagnostic())
    if out:
        print(out)
    return 0


def _spawn_worker() -> None:
    """Fully detached: new session AND all three fds redirected. setsid alone
    leaves inherited hook stdio open and the hook may not return cleanly."""
    log = open(C.LOGS / "worker.out", "ab", buffering=0)
    devnull = open("/dev/null", "rb")
    try:
        subprocess.Popen(
            [sys.executable, "-m", "handoff.cli", "drain"],
            cwd=str(C.ROOT), start_new_session=True,
            stdin=devnull, stdout=log, stderr=log,
        )
    except OSError as exc:
        _log(f"spawn failed: {exc}")
    finally:
        log.close()
        devnull.close()


def _trivial(result) -> bool:
    """Nothing to distil: no words from the user, no edits, (almost) no actions.
    Many archived jobs were this shape and v1 still rewrote RESUME.md for
    each one ("Done: (nothing recorded)")."""
    if result.lifecycle_only:
        return True
    said = any(c.kind == "human" and c.meta.get("said") for c in result.candidates.values())
    return not said and not result.edited_paths and len(result.actions) < 3 and not result.nominations


def _session_time(records: list[dict]) -> str | None:
    """When the segment happened, in local time, from its own records. A
    re-filed or backlogged job must be dated by the session, not by the day
    the queue got to it."""
    for rec in reversed(records):
        ts = rec.get("timestamp")
        if ts:
            try:
                return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().isoformat(timespec="seconds")
            except ValueError:
                continue
    return None


def distill_records(records: list[dict], job: dict, force_slug: str | None = None) -> dict:
    """v2: route by evidence, distil the whole segment, return the result record."""
    from . import distill
    result = prefilter.build(records)
    cwd = job.get("cwd") or resolve.cwd_from_records(records)
    base = {"job_id": job["job_id"], "at": job.get("at") or _session_time(records) or _now(),
            "session_id": job.get("session_id"),
            "cwd": cwd, "repo_fingerprint": job.get("repo_fingerprint", {}),
            "nominations": [{"text": c.text, "slug": c.meta.get("slug")} for c in result.nominations]}
    projects = resolve.candidates(result.signals)
    why = resolve.resolve(cwd, None, result.signals)[1]
    if _trivial(result):
        return {**base, "v": 2, "slug": projects[0] if projects else C.UNSORTED_SLUG,
                "skipped": "trivial", "slug_reason": why, "stats": {}}
    if not projects:
        projects = [distill.classify(result)]
        why = "model-classified (no path/host/keyword evidence)"
    resolved_slug = projects[0]
    if force_slug:                                   # eval: score content, not routing
        projects = [force_slug] + [p for p in projects if p != force_slug]
    touched, seen = [], set()
    NOISE = ("/tmp/", str(Path.home() / ".claude/"), "/dev/", "/proc/")
    for path in reversed(result.edited_paths):
        if path in seen or any(path.startswith(n) for n in NOISE) or resolve._match(path) != projects[0]:
            continue
        seen.add(path)
        try:
            size = Path(path).stat().st_size
        except OSError:
            size = None
        touched.append({"path": path, "bytes": size})
        if len(touched) >= 8:
            break
    entry, stats = distill.run_session(result, cwd, render.open_items(projects[0]), projects)
    stats.update({"redactions": result.redactions, "actions": len(result.actions),
                  "failures": len(result.failures)})
    model = (stats.get("synthesis") or {}).get("model")
    return {**base, "v": 2, "slug": projects[0], "resolved_slug": resolved_slug, "slug_reason": why,
            "entry": entry, "touched": touched, "stats": stats, "model": model}


def process_job(job: dict) -> dict:
    """One segment, end to end (v2). Raises on failure so the caller can defer it.
    Reads the spooled copy of the segment when there is one (store.segment_source)."""
    records = prefilter.read_records(*store.segment_source(job), job.get("record_source", "claude"))
    return distill_records(records, job)


def process_job_v1(job: dict) -> dict:
    """One segment, end to end. Raises on failure so the caller can defer it."""
    from . import distill
    tp = Path(job["transcript_path"])
    records = prefilter.read_records(tp, job["start_offset"], job["end_offset"], job["inode"],
                                     job.get("record_source", "claude"))
    result = prefilter.build(records)

    cwd = job.get("cwd") or resolve.cwd_from_records(records)
    slug, why = resolve.resolve(cwd, result.edited_paths)

    if result.lifecycle_only:
        # Bare resumes and compaction artifacts carry nothing. A DISCUSSION-only
        # session is NOT lifecycle-only and must still be distilled (PLAN.md G).
        return {"job_id": job["job_id"], "slug": slug, "at": _now(),
                "session_id": job.get("session_id"), "skipped": "lifecycle-only",
                "state": {"done": [], "next": [], "blockers": [], "resolves": []},
                "lessons": [], "nominations": [], "stats": {},
                "repo_fingerprint": job.get("repo_fingerprint", {})}

    # Deterministic pointers: the session-shape audit found orientation
    # reconstructs "which files were touched, how big, what git state" by hand
    # (tens of thousands of tokens). No model needed for any of it.
    touched, seen = [], set()
    # Scratch and harness paths are not project state; a pointer to /tmp is noise.
    NOISE = ("/tmp/", str(Path.home() / ".claude/"), "/dev/", "/proc/")
    for path in reversed(result.edited_paths):          # most recent first
        if path in seen or any(path.startswith(n) for n in NOISE):
            continue
        seen.add(path)
        try:
            size = Path(path).stat().st_size
        except OSError:
            size = None
        touched.append({"path": path, "bytes": size})
        if len(touched) >= 8:
            break

    state, stats_a = distill.run_pass_a(result, cwd, render.open_items(slug))
    lessons, stats_b = distill.run_pass_b(result)

    return {
        "job_id": job["job_id"], "slug": slug, "slug_reason": why, "at": _now(),
        "session_id": job.get("session_id"), "cwd": cwd,
        "state": state, "lessons": lessons,
        "nominations": [{"text": c.text, "slug": c.meta.get("slug")}
                        for c in result.nominations],
        "touched": touched,
        "stats": {"pass_a": stats_a, "pass_b": stats_b,
                  "redactions": result.redactions,
                  "corrections": len(result.corrections),
                  "failures": len(result.failures),
                  "actions": len(result.actions)},
        "repo_fingerprint": job.get("repo_fingerprint", {}),
    }


def _outage(exc: Exception) -> bool:
    """True when the model never got to answer: thor or Claude unreachable, a usage
    or rate limit, the network, a 5xx/429, a model timeout, a busy lock. These
    defer without spending the attempt budget (audit 2026-10-12 F1; a long
    outage would otherwise burn every attempt on timeouts). Unparseable or invalid output and
    broken jobs count, and MAX_ATTEMPTS of them quarantine the job."""
    from . import distill
    msg = str(exc)
    if isinstance(exc, distill.ThorUnavailable):          # ClaudeUnavailable included
        # ponytail: claude-only mode reports a twice-bad answer as ClaudeUnavailable;
        # matched by its message until distill.py raises it as BadOutput.
        return not msg.startswith("bad output twice")
    if isinstance(exc, distill.BadOutput):
        return msg.startswith("timeout after") or bool(re.match(r"HTTP (5\d\d|429)\b", msg))
    return isinstance(exc, OSError) and not isinstance(exc, FileNotFoundError)


# Drain to empty: a drain only spawns on SessionStart, so a cap of 5 let Codex's
# many short segments outrun it and the queue sat for hours between sessions.
# ponytail: one drain can hold thor's single 27B slot for a long queue; add a
# time budget if another 27B caller starves.
def cmd_drain(max_jobs: int = 200) -> int:
    from . import distill
    # Operator brake: every SessionStart spawns a drain, so stopping one by hand
    # does not stop the next. Touch state/PAUSED to hold the queue (it stays durable).
    if (C.STATE / "PAUSED").exists():
        _log("drain skipped: state/PAUSED present")
        return 0
    ok, msg = redact.self_check()
    if not ok:
        _log(f"ABORT: redactor unusable: {msg}")      # never transmit unredacted
        return 1
    quarantined = []                                          # error classes, for one alert per drain
    try:
        with store.consumer_lock():
            recovered = store.recover_claims()                # crash reconcile: a drain died mid-job
            # Counted before any _log: a full or unwritable log raising on an earlier
            # line must not drop a quarantine from the alert (Astra round 3 #4 review).
            quarantined += ["Crash" for line in recovered if line.startswith("QUARANTINE ")]
            for line in recovered:
                _log(line)
            _sync("pull")
            done = 0
            for _ in range(max_jobs):
                job = store.claim_next_job()
                if job is None:
                    break
                jid = job["job_id"]
                try:
                    store.claim_job(job)
                    cached = store.load_result(jid)
                    if cached and cached.get("v") != 2:
                        cached = None                        # v1 result: misrouted and thin, redo it
                    result = cached if cached else process_job(job)
                    if not cached:
                        store.commit_result(jid, result)     # transactional point
                    out = render.write_all(result)
                    for slug in out.get("projects") or ([result["slug"]] if not result.get("skipped") else []):
                        if slug != C.UNSORTED_SLUG:
                            brief.build(slug, result.get("repo_fingerprint", {}) if slug == result["slug"] else {})
                    store.ack_job(job)
                    done += 1
                    _log(f"ok {jid} -> {result['slug']} {result.get('skipped') or ''} "
                         f"projects={out.get('projects')} gen={out['generation']} "
                         f"written={len(out['written'])} model={result.get('model')}")
                except Exception as exc:                      # noqa: BLE001
                    outage = _outage(exc)
                    err = f"{type(exc).__name__}: {exc}"
                    fate = store.defer_job(job, err, outage)
                    if fate == "quarantined":                 # before _log, as above
                        quarantined.append(_error_class(exc))
                    _log(f"{'defer' if fate == 'deferred' else 'QUARANTINE'} {jid} "
                         f"({'outage, not counted' if outage else 'attempt %d' % (job.get('attempts', 0) + 1)}"
                         f"): {err[:300]}")
                    if outage:
                        break                                 # no point trying more
            if done:
                _sync("push")
            if store.queue_depth() == 0:
                distill.unload()                      # idle thor holds no model memory
            store.prune_acked()
    except TimeoutError:
        return 0                                              # another worker has it
    finally:
        _quarantine_alert(quarantined)                        # after the lock: never holds up the next drain
    return 0


ALERT_EVERY_S = 3600          # a repeating failure pings at most once an hour
ALERT_TIMEOUT_S = 20          # odin's tg rides ssh to heimdall; never wait longer than this
SYSTEMIC_24H = 3


def _num(obj, *keys) -> float | None:
    """obj[k1][k2]... when every level is a dict and the leaf a number, else None."""
    for k in keys:
        obj = obj.get(k) if isinstance(obj, dict) else None
    return obj if isinstance(obj, (int, float)) and not isinstance(obj, bool) else None


# The only words about a failure that leave odin. Exception text can carry an HTTP
# body or model output echoing session prose; it stays in worker.log (Astra round 3 #3).
ALERT_CLASSES = ("BadOutput", "ClaudeUnavailable", "ThorUnavailable", "Timeout", "Crash", "Other")


def _error_class(exc: BaseException) -> str:
    """A fixed ALERT_CLASSES name from the exception TYPE, never its message."""
    from . import distill
    for cls, name in ((distill.BadOutput, "BadOutput"), (distill.ClaudeUnavailable, "ClaudeUnavailable"),
                      (distill.ThorUnavailable, "ThorUnavailable"),
                      ((TimeoutError, subprocess.TimeoutExpired), "Timeout")):
        if isinstance(exc, cls):
            return name
    return "Other"


def _quarantine_alert(reasons: list[str], now: float | None = None) -> str:
    """One silent Telegram line (`tg fleet`) when this drain moved jobs to FAILED.
    A systemic failure quarantines one job per drain until the queue is gone, and
    the brief line alone told nobody (audit 2026-10-12 b5). `reasons` are
    ALERT_CLASSES names; anything else is sent as "Other", so no job content can
    reach Telegram. Best-effort to the last line: a missing, failing or hung tg, a
    full disk or an unwritable log is swallowed, never raised into cmd_drain's
    `finally` (Astra round 3 #4). Returns what happened, for tests."""
    if not reasons:
        return "none"
    try:
        now = time.time() if now is None else now
        total = 0
        recent = 0
        for path in store.FAILED.glob("*.json"):
            total += 1
            at = _num(C.read_json(path), "quarantined", "at")
            recent += at is not None and now - at < 86400
        stamp = C.STATE / "quarantine-alert.json"
        # A stamp that is not a past number (hand edit, clock jump) must not mute every alert after it.
        last = _num(C.read_json(stamp), "last_attempt")
        if last is not None and 0 <= now - last < ALERT_EVERY_S:
            _log(f"quarantine alert suppressed (last {int(now - last)}s ago): {len(reasons)} this drain")
            return "suppressed"
        C.write_json(stamp, {"last_attempt": now})            # before sending: a broken tg costs one timeout an hour
        classes = [r if r in ALERT_CLASSES else "Other" for r in reasons]
        kinds = ", ".join(f"{c} x{classes.count(c)}" for c in ALERT_CLASSES if c in classes)
        text = (f"handoff-v2: {len(reasons)} job(s) quarantined this drain, {total} in quarantine"
                f"{' - systemic?' if recent >= SYSTEMIC_24H else ''}. "
                f"Errors: {kinds}. See: python3 -m handoff.cli queue status (pipeline/handoff-v2).")
        tg = os.environ.get("HANDOFF_TG") or shutil.which("tg") or str(Path.home() / ".local" / "bin" / "tg")
        # No pipes and its own process group: a hung child cannot hold wait() open, and
        # the group kill reaches it. odin's tg wraps ssh in GNU timeout, which takes its
        # own group, so that ssh can linger past us until tg's own 60 s KILL.
        proc = subprocess.Popen([tg, "fleet", text], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            rc = proc.wait(timeout=ALERT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise
        _log(f"quarantine alert {'sent' if rc == 0 else f'tg exit {rc}'}: {text}")
        return "sent" if rc == 0 else "failed"
    except Exception as exc:                                  # noqa: BLE001
        try:
            _log(f"quarantine alert failed (non-fatal): {type(exc).__name__}: {exc}")
        except Exception:                                     # noqa: BLE001  (log disk full too)
            pass
        return "failed"


def _sync(direction: str) -> None:
    """Replication is best-effort: a failed pull must never block local
    distillation or rendering (PLAN.md N)."""
    exe = Path.home() / ".local" / "bin" / "vault-sync"
    if not exe.exists():
        return
    try:
        subprocess.run([str(exe), direction], capture_output=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError) as exc:
        _log(f"sync {direction} failed (non-fatal): {exc}")


def cmd_lessons() -> int:
    """Reviewable lesson candidates across every project (the v1 quarantine in
    state/candidates was machine-local and never read by anyone)."""
    n = 0
    for path in C.lesson_files():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("- [candidate]"):
                print(f"{C.lesson_project(path):22} {line[14:].split(' <!--')[0][:220]}")
                n += 1
    print(f"[handoff] {n} candidate lesson(s). Promote: harness feedback_* stub + a line in "
          f"Actions/_archive/corrections-log.md, then mark it [promoted] or [rejected].")
    return 0


MISFILE_BUCKETS = {"workflow", C.UNSORTED_SLUG, "vault-redesign"}


def _seed_resume(slug: str, original: Path) -> None:
    """Carry a project's pre-refile Next/Blocked/Noted items into the rebuilt
    RESUME (some journaled jobs had no archive entry
    left to rebuild from). Not for the v1 misfile buckets, whose items belonged
    to other projects. The full original is also kept in the vault archive."""
    src = original / slug / "RESUME.md"
    if not src.exists():
        return
    text = src.read_text(encoding="utf-8", errors="replace")
    dest = C.PROJECTS_DIR / slug / "_archive"
    dest.mkdir(parents=True, exist_ok=True)
    C.atomic_write(dest / "pre-refile-2026-09-28-RESUME.md", text)
    j = original / slug / "journal.ndjson"
    if j.exists():
        C.atomic_write(dest / "pre-refile-2026-09-28-journal.ndjson", j.read_text(encoding="utf-8", errors="replace"))
    if slug in MISFILE_BUCKETS:
        return
    keep, section = [], None
    for line in text.splitlines():
        low = line.strip().lower()
        if low.startswith("## next") or low.startswith("## blocked") or low.startswith("## noted"):
            section = line
            keep.append("")
            keep.append(line)
            continue
        if low.startswith("##"):
            section = None
            continue
        if section and render.ITEM_RE.match(line):
            keep.append(line)
    if keep:
        C.atomic_write(render.resume_path(slug), f"---\ntype: resume\nproject: {slug}\ngeneration: 0\n---\n"
                       + "\n".join(keep) + "\n")


def cmd_refile(apply: bool, seed: Path | None = None) -> int:
    """Re-resolve every archived job with the v2 resolver and, with --apply,
    re-distil it and rebuild the pipeline-written files of every affected
    project from scratch, oldest session first (so RESUME ends on the newest).
    Pipeline-owned files only: RESUME.md, _brief.md, journal.ndjson. The
    Claude-written NOTES/decisions/errors/history content is appended to, never
    rebuilt. A rebuild re-applies each cached result's own routing, so it undoes
    later `tidy` closes and moves and `rehome`s; re-run those after it."""
    import glob
    jobs = []
    for f in glob.glob(str(C.ARCHIVE / "*.json")):
        j = C.read_json(Path(f))
        if j and Path(j.get("transcript_path", "")).exists():
            jobs.append(j)
    jobs.sort(key=lambda j: j["sealed_ns"])
    plan = []
    for j in jobs:
        recs = prefilter.read_records(Path(j["transcript_path"]), j["start_offset"], j["end_offset"], None,
                                      j.get("record_source", "claude"))
        r = prefilter.build(recs)
        old = (store.load_result(j["job_id"]) or {}).get("slug", j.get("slug_hint"))
        new = "SKIP" if _trivial(r) else (resolve.candidates(r.signals) or ["(classify)"])
        plan.append((j, old, new))
        print(f"{j['job_id']} {old:>22} -> {new}")
    if not apply:
        print(f"[handoff] dry run: {len(plan)} archived jobs; "
              f"{sum(1 for _, _, n in plan if n == 'SKIP')} trivial. Re-run with --apply.")
        return 0
    try:
        lock = store.consumer_lock()
        lock.__enter__()                  # no drain may interleave with the rebuild
    except TimeoutError:
        print("[handoff] a drain is running; try again when it finishes")
        return 1
    backup = C.STATE / "refile-backup" / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    for rp in C.PROJECTS_DIR.glob("*/RESUME.md"):
        slug = rp.parent.name
        (backup / slug).mkdir(parents=True, exist_ok=True)
        for name in ("RESUME.md", "_brief.md", "journal.ndjson"):
            src = rp.parent / name
            if src.exists():
                src.replace(backup / slug / name)
    _log(f"refile: backed up pipeline files to {backup}")
    original = seed or backup
    for d in original.iterdir() if original.is_dir() else []:
        _seed_resume(d.name, original)
    from . import distill
    for j, old, new in plan:
        if new == "SKIP":
            continue
        try:
            cached = store.load_result(j["job_id"])
            result = cached if cached and cached.get("v") == 2 else process_job({**j, "inode": None})
            store.commit_result(j["job_id"], result)
            out = render.write_all(result)
            for slug in out.get("projects") or []:
                if slug != C.UNSORTED_SLUG:
                    brief.build(slug, result.get("repo_fingerprint", {}) if slug == result["slug"] else {})
            _log(f"refile {j['job_id']} {old} -> {out.get('projects')} model={result.get('model')}")
        except distill.ThorUnavailable as exc:
            _log(f"refile stopped: thor unavailable: {exc}")
            return 1
        except Exception as exc:                          # noqa: BLE001
            _log(f"refile {j['job_id']} FAILED {type(exc).__name__}: {exc}")
    distill.unload()
    lock.__exit__(None, None, None)
    return 0


def cmd_tidy(plan_path: Path, apply: bool) -> int:
    """Apply a reviewed RESUME cleanup plan, JSON {"close": [{"slug", "id", "why"}],
    "move": [{"slug", "id", "to"}]}: closed and moved items leave RESUME for
    _archive/resume-closed.md, a moved item joins the same list on its new project,
    each touched RESUME is de-duped and its brief rebuilt. Dry run without --apply."""
    plan = C.read_json(plan_path) or {}
    closes, moves, add, skipped, named = {}, {}, {}, 0, set()
    ops = (plan.get("close") or []) + (plan.get("move") or [])
    # An id named by two ops is refused outright, whatever RESUMEs hold now, so a replay
    # cannot run the op the first run skipped (A:X->B, B:X->C).
    count = {}
    for op in ops:
        count[op.get("id")] = count.get(op.get("id"), 0) + 1
    for op in ops:
        slug, to = op.get("slug"), op.get("to")
        named |= {s for s in (slug, to) if s and (C.PROJECTS_DIR / s).is_dir()}
        hit = next(((sec, i) for sec in ("next", "blockers")
                    for i in render.parse_resume(slug)[sec] if i["id"] == op.get("id")), None) \
            if slug and (C.PROJECTS_DIR / slug).is_dir() else None
        if not hit or ("to" in op and not (to and to != slug and (C.PROJECTS_DIR / to).is_dir())) \
                or count[op["id"]] > 1:
            print(f"SKIP (no such item, bad target or id listed twice): {json.dumps(op)[:200]}")
            skipped += 1
            continue
        sec, item = hit
        why = f"moved to {to}" if to else f"closed: {op.get('why') or 'reviewed'}"
        (moves if to else closes).setdefault(slug, {})[op["id"]] = why
        if to:
            add.setdefault(to, {}).setdefault(sec, []).append(item)
        print(f"{slug:24} {op['id']} {why[:70]} | {item['text'][:90]}")
    print(f"[handoff] tidy: {sum(map(len, [*closes.values(), *moves.values()]))} item(s) leave RESUME, "
          f"{sum(len(v) for a in add.values() for v in a.values())} move, {skipped} skipped")
    if not apply:
        print("[handoff] dry run; re-run with --apply")
        return 0
    # Closes first, so a full RESUME has room before items move in (adding first sent a project's
    # own items to overflow, closes included, 2026-10-30); then each move adds before it drops:
    # a crash duplicates, never loses.
    for edits in ({s: {"drop": d} for s, d in closes.items()}, {s: {"add": a} for s, a in add.items()},
                  {s: {"drop": d} for s, d in moves.items()}):
        for slug in sorted(edits):
            for w in render.edit_resume(slug, **edits[slug]):
                print(w)
    for slug in sorted(named):                      # every project the plan names: a replay after a crash
        if slug != C.UNSORTED_SLUG:                 # (all ops now skip) still refreshes the briefs
            brief.build(slug, {})
    return 0


def cmd_rehome(src: str, dst: str, prefixes: list[str], apply: bool) -> int:
    """Re-file whole sessions, by job id or a unique prefix, from Projects/<src> to
    Projects/<dst>, oldest first (render.move_sessions). Dry run without --apply."""
    journal = C.PROJECTS_DIR / src / "journal.ndjson"
    rows = [json.loads(ln) for ln in journal.read_text(encoding="utf-8").splitlines() if ln.strip()] \
        if journal.exists() else []
    if src == dst or dst not in resolve.allowlist():
        print(f"[handoff] rehome: bad target {dst!r} (not in project-map.json or Projects/)", file=sys.stderr)
        return 2
    picked, bad = [], 0
    for p in prefixes:
        hits = [r for r in rows if r.get("job_id", "").startswith(p)]
        if len(hits) != 1:
            print(f"SKIP {p}: {len(hits)} sessions in {src} match")
            bad += 1
        elif hits[0] not in picked:
            picked.append(hits[0])
    picked.sort(key=lambda r: r.get("at") or "")
    for r in picked:
        print(f"{src} -> {dst}  {r['job_id'][:8]}  {(r.get('at') or '')[:10]}  {(r.get('title') or '')[:80]}")
    print(f"[handoff] rehome: {len(picked)} session(s), {bad} skipped")
    if not apply:
        print("[handoff] dry run; re-run with --apply")
        return 0
    written, kept = render.move_sessions(src, dst, [r["job_id"] for r in picked])
    for w in written:
        print(w)
    for jid in kept:
        print(f"KEPT in {src}: {jid} ({dst} already holds another part of that session under its marker)")
    for slug in (src, dst):
        if slug != C.UNSORTED_SLUG and render.resume_path(slug).exists():
            brief.build(slug, {})
    return 0


def cmd_reconcile() -> int:
    for r in seal.reconcile():
        _log(f"reconcile {r}")
    return 0


def _age(s: float) -> str:
    s = int(s)
    return f"{s}s" if s < 120 else f"{s // 60}m" if s < 7200 else f"{s // 3600}h" if s < 172800 else f"{s // 86400}d"


def cmd_queue_status() -> int:
    """`handoff queue status`: queued / in-progress / deferred / quarantined counts
    with ages since the seal, then one line per job that is not simply queued."""
    st = store.queue_status()
    names = (("queued", "queued"), ("in_progress", "in-progress"), ("deferred", "deferred"),
             ("quarantined", "quarantined"), ("overflow", "overflow"))
    print("[handoff] queue: " + " ".join(
        f"{label}={st[b]['count']}" + (f" (oldest {_age(st[b]['oldest_s'])})" if st[b]["count"] else "")
        for b, label in names))
    for r in st["jobs"]["in_progress"]:
        print(f"  in-progress {r['job_id']} pid {r['pid']} {'alive' if r['alive'] else 'GONE (next drain requeues it)'}"
              f" age {_age(r['age_s'])}")
    for r in st["jobs"]["deferred"]:
        print(f"  deferred    {r['job_id']} {r['error_class'] or 'failure'} attempts={r['attempts']}"
              f" deferrals={r['deferrals']} crashes={r['crashes']} retry in {_age(r['retry_in_s'])}"
              f" age {_age(r['age_s'])}: {r['error'][:100]}")
    for r in st["jobs"]["quarantined"]:
        print(f"  quarantined {r['job_id']} attempts={r['attempts']} age {_age(r['age_s'])}: {r['error'][:100]}")
    if st["quarantined"]["count"]:
        print(f"  requeue one: set attempts to 0 and move it from {store.FAILED} to {C.QUEUE}")
    return 0


def cmd_status() -> int:
    print(brief.diagnostic())
    cmd_queue_status()
    ok, msg = redact.self_check()
    print(f"[handoff] redactor: {'ok' if ok else 'BROKEN - ' + msg}")
    try:
        import urllib.request
        with urllib.request.urlopen(C.OLLAMA_URL + "/api/tags", timeout=5):
            print(f"[handoff] thor: reachable ({C.MODEL})")
    except Exception:                                          # noqa: BLE001
        print(f"[handoff] thor: UNREACHABLE at {C.OLLAMA_URL} - jobs will queue" if C.OLLAMA_URL else
              f"[handoff] thor: {C.OLLAMA_UNSET} - jobs on the Ollama path will queue")
    for path in sorted(C.QUEUE.glob("*.json"))[:5]:
        job = C.read_json(path) or {}
        print(f"           queued {job.get('job_id','?')} attempts={job.get('attempts',0)} "
              f"{job.get('last_error','')[:60]}")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "seal":
        return cmd_seal(sys.argv[2] if len(sys.argv) > 2 else "session-end")
    if cmd == "start":
        return cmd_start()
    if cmd == "subagent-start":
        return cmd_start(inject_only=True)
    if cmd == "drain":
        return cmd_drain()
    if cmd == "reconcile":
        return cmd_reconcile()
    if cmd == "status":
        return cmd_status()
    if cmd == "queue":
        sub = sys.argv[2] if len(sys.argv) > 2 else "status"
        if sub == "status":
            return cmd_queue_status()
        print(f"unknown queue command: {sub}", file=sys.stderr)
        return 2
    if cmd == "lessons":
        return cmd_lessons()
    if cmd == "refile":
        args = sys.argv[2:]
        seed = Path(args[args.index("--seed") + 1]) if "--seed" in args else None
        return cmd_refile("--apply" in args, seed)
    if cmd == "compact-resume":     # one-off: cap RESUMEs written before the Next/Blocked window
        slugs = sys.argv[2:] or sorted(p.parent.name for p in C.PROJECTS_DIR.glob("*/RESUME.md"))
        for slug in slugs:
            for w in render.compact_resume(slug):
                print(w)
        return 0
    if cmd == "dedupe-resume":      # collapse reworded duplicates already on RESUMEs, rebuild their briefs
        slugs = sys.argv[2:] or sorted(p.parent.name for p in C.PROJECTS_DIR.glob("*/RESUME.md"))
        for slug in slugs:
            for w in render.edit_resume(slug):
                print(w)
            if slug != C.UNSORTED_SLUG:
                brief.build(slug, {})       # always: a crash between RESUME and brief must not leave it stale
        return 0
    if cmd == "rehome":
        args = [a for a in sys.argv[2:] if a != "--apply"]
        if len(args) < 3:
            print("usage: rehome <from-slug> <to-slug> <job-id-prefix>... [--apply]", file=sys.stderr)
            return 2
        return cmd_rehome(args[0], args[1], args[2:], "--apply" in sys.argv[2:])
    if cmd == "tidy":
        args = sys.argv[2:]
        plan = next((a for a in args if not a.startswith("--")), None)
        if not plan:
            print("usage: tidy <plan.json> [--apply]", file=sys.stderr)
            return 2
        return cmd_tidy(Path(plan), "--apply" in args)
    if cmd == "migrate":
        from . import migrate
        return migrate.main(sys.argv[2:])
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
