#!/usr/bin/env python3
"""
aos_lock.py — cross-session file locks for Claude Code + Codex.

Single source of truth for "which session is editing which file right now",
plus stale-read detection so a session can't edit a file it last read before
another session changed it.

State lives at:
  ~/agentic-os/locks/locks.json           {abs_path: lock_record}
  ~/agentic-os/locks/sessions.json        {session_id: session_record}
  ~/agentic-os/locks/session_reads/<sid>.json   {abs_path: {sha256, read_at}}
  ~/agentic-os/locks/lock.log             append-only audit
  ~/agentic-os/locks/.mutex               sentinel for atomic JSON updates

Atomicity: short critical section + temp-file + os.replace. Retry on contention.

Exit codes:
  0  success / allowed
  2  blocked (PreToolUse contract: blocks the tool, stderr surfaces to LLM)
  3  malformed input
  4  internal error
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# -------- paths --------

ROOT = Path(os.environ.get("AOS_LOCK_DIR", str(Path.home() / "agentic-os" / "locks")))
LOCKS_FILE = ROOT / "locks.json"
SESSIONS_FILE = ROOT / "sessions.json"
READS_DIR = ROOT / "session_reads"
LOG_FILE = ROOT / "lock.log"
MUTEX_FILE = ROOT / ".mutex"

DEFAULT_TTL_SECONDS = 600     # 10 min, refreshed on every PostToolUse Edit/Write
SESSION_IDLE_SECONDS = 1800   # 30 min: session considered dead, locks reclaimable
MUTEX_TIMEOUT_SECONDS = 5
MUTEX_POLL = 0.02

# -------- utils --------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def _now_ts() -> float:
    return time.time()

def _iso_to_ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0

def _ensure_dirs() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    READS_DIR.mkdir(parents=True, exist_ok=True)
    for f in (LOCKS_FILE, SESSIONS_FILE):
        if not f.exists():
            f.write_text("{}", encoding="utf-8")

def _norm_path(p: str) -> str:
    if not p:
        return ""
    # Git Bash / MSYS style: /c/Users/... → C:/Users/...
    if os.name == "nt" and len(p) >= 3 and p[0] == "/" and p[2] == "/" and p[1].isalpha():
        p = f"{p[1].upper()}:/{p[3:]}"
    try:
        return str(Path(p).resolve())
    except Exception:
        return os.path.abspath(p)

def _sha256_file(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return None

def _log(event: str, **fields) -> None:
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": _now_iso(), "event": event, **fields}) + "\n")
    except Exception:
        pass

def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            STILL_ACTIVE = 259
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            return bool(ok) and code.value == STILL_ACTIVE
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False

# -------- mutex --------

class _Mutex:
    def __enter__(self):
        deadline = _now_ts() + MUTEX_TIMEOUT_SECONDS
        while _now_ts() < deadline:
            try:
                fd = os.open(str(MUTEX_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()}\n{_now_iso()}".encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    age = _now_ts() - MUTEX_FILE.stat().st_mtime
                    if age > 10:
                        MUTEX_FILE.unlink(missing_ok=True)
                except Exception:
                    pass
                time.sleep(MUTEX_POLL)
        raise TimeoutError("aos_lock mutex contention")

    def __exit__(self, exc_type, exc, tb):
        try:
            MUTEX_FILE.unlink(missing_ok=True)
        except Exception:
            pass

# -------- json io --------

def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8") or "{}")
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}

def _write_json(path: Path, data: dict) -> None:
    # Unique tmp name per writer: concurrent writers to the same target (e.g. the
    # read-fingerprint hook firing async on every Read) must not share one .tmp,
    # or they collide on Windows (WinError 32 / Errno 13). Retry os.replace on the
    # transient sharing-violation the docstring promises to tolerate.
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(data, indent=2, sort_keys=True)
    try:
        tmp.write_text(payload, encoding="utf-8")
        last_err: Exception | None = None
        for _ in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as e:  # Windows: dest briefly held by another process
                last_err = e
                time.sleep(0.02)
        raise last_err if last_err else OSError("aos_lock _write_json: replace failed")
    finally:
        try:
            tmp.unlink(missing_ok=True)  # no-op if replace succeeded
        except Exception:
            pass

# -------- session id --------

def _session_id(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    for key in ("AOS_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID"):
        v = os.environ.get(key)
        if v:
            return v
    return f"unattributed-{os.getpid()}"

def _agent_from_env(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    v = os.environ.get("AOS_AGENT")
    if v:
        return v
    if os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("CLAUDE_SESSION_ID"):
        return "claude"
    if os.environ.get("CODEX_SESSION_ID") or os.environ.get("CODEX_HOME"):
        return "codex"
    return "unknown"

# -------- session bookkeeping --------

def _touch_session(sessions: dict, session_id: str, agent: str, intent: str | None = None) -> None:
    rec = sessions.get(session_id, {})
    rec["session_id"] = session_id
    rec["agent"] = agent
    rec["pid"] = rec.get("pid") or os.getppid()
    rec["cwd"] = rec.get("cwd") or os.getcwd()
    rec.setdefault("started_at", _now_iso())
    rec["last_seen"] = _now_iso()
    if intent:
        rec["last_intent"] = intent[:200]
    sessions[session_id] = rec

def _gc_dead_sessions(sessions: dict, locks: dict) -> int:
    """Remove sessions whose last_seen is older than SESSION_IDLE_SECONDS.

    PID-based liveness is unreliable: every aos_lock invocation is a
    one-shot subprocess, so the parent PID recorded at session-start
    typically dies before the next heartbeat fires. We rely on:
      * lock TTL (10 min) auto-expiring stale locks on next acquire
      * session idle timeout (30 min) reclaiming the session record itself
    Both are refreshed on every PostToolUse Edit/Write via hook_postuse_edit.
    """
    now = _now_ts()
    freed = 0
    dead = []
    for sid, rec in sessions.items():
        age = now - _iso_to_ts(rec.get("last_seen", ""))
        if age > SESSION_IDLE_SECONDS:
            dead.append(sid)
    for sid in dead:
        for path, lock in list(locks.items()):
            if lock.get("session_id") == sid:
                del locks[path]
                freed += 1
        sessions.pop(sid, None)
        reads = READS_DIR / f"{sid}.json"
        try:
            reads.unlink(missing_ok=True)
        except Exception:
            pass
    return freed

def _gc_expired_locks(locks: dict) -> int:
    now = _now_ts()
    freed = 0
    for path, lock in list(locks.items()):
        if _iso_to_ts(lock.get("expires_at", "")) < now:
            del locks[path]
            freed += 1
    return freed

# -------- read fingerprints --------

def _reads_path(session_id: str) -> Path:
    return READS_DIR / f"{session_id}.json"

def _record_read(session_id: str, abs_path: str) -> None:
    fp = _sha256_file(abs_path)
    if fp is None:
        return
    p = _reads_path(session_id)
    data = _read_json(p)
    data[abs_path] = {"sha256": fp, "read_at": _now_iso()}
    _write_json(p, data)

def _get_known_hash(session_id: str, abs_path: str) -> str | None:
    data = _read_json(_reads_path(session_id))
    rec = data.get(abs_path)
    return rec.get("sha256") if rec else None

# -------- core verbs --------

def cmd_acquire(path: str, session_id: str, agent: str, intent: str = "", ttl: int = DEFAULT_TTL_SECONDS) -> tuple[int, str]:
    abs_path = _norm_path(path)
    with _Mutex():
        locks = _read_json(LOCKS_FILE)
        sessions = _read_json(SESSIONS_FILE)
        _gc_expired_locks(locks)
        _gc_dead_sessions(sessions, locks)

        held = locks.get(abs_path)
        if held and held.get("session_id") != session_id:
            other_agent = held.get("agent", "?")
            other_intent = held.get("intent", "")
            held_age = int(_now_ts() - _iso_to_ts(held.get("acquired_at", "")))
            msg = (
                f"[aos-lock] BLOCKED — '{abs_path}' is locked by session "
                f"{held.get('session_id','?')} ({other_agent}) for {held_age}s. "
                f"Their intent: {other_intent or '(none stated)'}. "
                f"Wait, work on a different file, or surface to user."
            )
            _log("acquire.blocked", path=abs_path, by=session_id, holder=held.get("session_id"))
            return 2, msg

        # Stale-read gate: only enforce if file currently exists AND session has a prior read.
        current_hash = _sha256_file(abs_path)
        if current_hash is not None:
            known = _get_known_hash(session_id, abs_path)
            if known is None:
                msg = (
                    f"[aos-lock] BLOCKED — session {session_id} has not Read "
                    f"'{abs_path}' yet. Read it first, then retry the edit."
                )
                _log("acquire.no_prior_read", path=abs_path, by=session_id)
                return 2, msg
            if known != current_hash:
                msg = (
                    f"[aos-lock] BLOCKED — '{abs_path}' has changed since session "
                    f"{session_id} last read it. Re-read the file before editing."
                )
                _log("acquire.stale_read", path=abs_path, by=session_id)
                return 2, msg

        now_ts = _now_ts()
        locks[abs_path] = {
            "session_id": session_id,
            "agent": agent,
            "intent": (intent or "")[:200],
            "acquired_at": _now_iso(),
            "expires_at": datetime.fromtimestamp(now_ts + ttl, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "pid": os.getppid(),
        }
        _touch_session(sessions, session_id, agent, intent)
        _write_json(LOCKS_FILE, locks)
        _write_json(SESSIONS_FILE, sessions)
        _log("acquire.ok", path=abs_path, by=session_id, agent=agent)
        return 0, f"[aos-lock] acquired {abs_path}"

def cmd_release(path: str, session_id: str) -> tuple[int, str]:
    abs_path = _norm_path(path)
    with _Mutex():
        locks = _read_json(LOCKS_FILE)
        held = locks.get(abs_path)
        if not held:
            return 0, f"[aos-lock] no lock on {abs_path}"
        if held.get("session_id") != session_id:
            return 0, f"[aos-lock] {abs_path} held by another session; not releasing"
        del locks[abs_path]
        _write_json(LOCKS_FILE, locks)
        _log("release.ok", path=abs_path, by=session_id)
        return 0, f"[aos-lock] released {abs_path}"

def cmd_release_all(session_id: str) -> tuple[int, str]:
    with _Mutex():
        locks = _read_json(LOCKS_FILE)
        sessions = _read_json(SESSIONS_FILE)
        freed = 0
        for path, lock in list(locks.items()):
            if lock.get("session_id") == session_id:
                del locks[path]
                freed += 1
        sessions.pop(session_id, None)
        _write_json(LOCKS_FILE, locks)
        _write_json(SESSIONS_FILE, sessions)
        try:
            _reads_path(session_id).unlink(missing_ok=True)
        except Exception:
            pass
        _log("release_all.ok", by=session_id, freed=freed)
        return 0, f"[aos-lock] released {freed} lock(s) for {session_id}"

def cmd_check(path: str) -> tuple[int, str]:
    abs_path = _norm_path(path)
    locks = _read_json(LOCKS_FILE)
    held = locks.get(abs_path)
    if not held:
        return 0, json.dumps({"path": abs_path, "held": False}, indent=2)
    return 0, json.dumps({"path": abs_path, "held": True, **held}, indent=2)

def cmd_list() -> tuple[int, str]:
    with _Mutex():
        locks = _read_json(LOCKS_FILE)
        sessions = _read_json(SESSIONS_FILE)
        _gc_expired_locks(locks)
        _gc_dead_sessions(sessions, locks)
        _write_json(LOCKS_FILE, locks)
        _write_json(SESSIONS_FILE, sessions)
    return 0, json.dumps({"locks": locks, "sessions": sessions}, indent=2)

def cmd_heartbeat(session_id: str, agent: str) -> tuple[int, str]:
    with _Mutex():
        locks = _read_json(LOCKS_FILE)
        sessions = _read_json(SESSIONS_FILE)
        _touch_session(sessions, session_id, agent)
        # refresh TTL for all locks held by this session
        bumped = 0
        new_expiry = datetime.fromtimestamp(_now_ts() + DEFAULT_TTL_SECONDS, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        for path, lock in locks.items():
            if lock.get("session_id") == session_id:
                lock["expires_at"] = new_expiry
                bumped += 1
        _write_json(LOCKS_FILE, locks)
        _write_json(SESSIONS_FILE, sessions)
        return 0, f"[aos-lock] heartbeat ok, refreshed {bumped} lock(s)"

def cmd_session_start(session_id: str, agent: str) -> tuple[int, str]:
    with _Mutex():
        sessions = _read_json(SESSIONS_FILE)
        locks = _read_json(LOCKS_FILE)
        _gc_expired_locks(locks)
        _gc_dead_sessions(sessions, locks)
        _touch_session(sessions, session_id, agent)
        _write_json(SESSIONS_FILE, sessions)
        _write_json(LOCKS_FILE, locks)
        _log("session.start", session_id=session_id, agent=agent)
        return 0, f"[aos-lock] session registered: {session_id} ({agent})"

def cmd_session_end(session_id: str) -> tuple[int, str]:
    return cmd_release_all(session_id)

def cmd_rescue(action: str, session_id: str | None = None, paths: list[str] | None = None) -> tuple[int, str]:
    """Operational rescue commands.

    Actions:
      fingerprint  — seed read fingerprints for given paths under session_id,
                     so blocks like "has not Read … yet" can be unstuck.
      orphans      — release locks whose session is gone from sessions.json
                     (or whose session has been idle past the threshold).
      nuke         — wipe everything: all locks, all sessions, all read files.
                     Use when state is hopelessly tangled. Active sessions
                     will re-register on their next hook call.
      status       — print a human report on stuck-looking locks.
    """
    if action == "fingerprint":
        if not session_id or not paths:
            return 3, "[aos-rescue] fingerprint needs --session and --paths"
        recorded = []
        skipped = []
        for p in paths:
            abs_p = _norm_path(p)
            before = _get_known_hash(session_id, abs_p)
            _record_read(session_id, abs_p)
            after = _get_known_hash(session_id, abs_p)
            if after and after != before:
                recorded.append(abs_p)
            elif after is None:
                skipped.append(f"{abs_p} (not a readable file)")
            else:
                recorded.append(f"{abs_p} (already current)")
        msg = f"[aos-rescue] fingerprinted {len(recorded)} path(s) for session {session_id}"
        if skipped:
            msg += "\n  skipped:\n    " + "\n    ".join(skipped)
        if recorded:
            msg += "\n  ok:\n    " + "\n    ".join(recorded)
        _log("rescue.fingerprint", session_id=session_id, count=len(recorded))
        return 0, msg

    if action == "orphans":
        with _Mutex():
            locks = _read_json(LOCKS_FILE)
            sessions = _read_json(SESSIONS_FILE)
            now = _now_ts()
            released = []
            for path, lock in list(locks.items()):
                sid = lock.get("session_id")
                sess = sessions.get(sid)
                lock_age = now - _iso_to_ts(lock.get("acquired_at", ""))
                expired = _iso_to_ts(lock.get("expires_at", "")) < now
                missing_session = sess is None
                sess_idle = sess and (now - _iso_to_ts(sess.get("last_seen", ""))) > SESSION_IDLE_SECONDS
                if expired or missing_session or sess_idle:
                    del locks[path]
                    released.append(f"{path}  (held by {sid}; age {int(lock_age)}s; reason: "
                                    f"{'expired' if expired else ('missing-session' if missing_session else 'idle-session')})")
            _gc_dead_sessions(sessions, locks)
            _write_json(LOCKS_FILE, locks)
            _write_json(SESSIONS_FILE, sessions)
        _log("rescue.orphans", released=len(released))
        if not released:
            return 0, "[aos-rescue] no orphan locks found"
        return 0, "[aos-rescue] released orphans:\n  " + "\n  ".join(released)

    if action == "nuke":
        with _Mutex():
            _write_json(LOCKS_FILE, {})
            _write_json(SESSIONS_FILE, {})
            for f in READS_DIR.glob("*.json"):
                try:
                    f.unlink()
                except Exception:
                    pass
        _log("rescue.nuke")
        return 0, "[aos-rescue] wiped all locks, sessions, and read fingerprints"

    if action == "status":
        locks = _read_json(LOCKS_FILE)
        sessions = _read_json(SESSIONS_FILE)
        now = _now_ts()
        lines = [f"=== {len(sessions)} session(s) ==="]
        for sid, rec in sessions.items():
            age = int(now - _iso_to_ts(rec.get("last_seen", "")))
            lines.append(f"  {sid}  agent={rec.get('agent','?')}  idle={age}s  cwd={rec.get('cwd','?')}")
        lines.append(f"\n=== {len(locks)} lock(s) ===")
        for path, lock in locks.items():
            age = int(now - _iso_to_ts(lock.get("acquired_at", "")))
            ttl = int(_iso_to_ts(lock.get("expires_at", "")) - now)
            sid = lock.get("session_id", "?")
            orphan = " [ORPHAN]" if sid not in sessions else ""
            stale_ttl = " [EXPIRED]" if ttl < 0 else ""
            lines.append(f"  {path}")
            lines.append(f"    by={sid} ({lock.get('agent','?')})  age={age}s  ttl={ttl}s{orphan}{stale_ttl}")
            lines.append(f"    intent: {lock.get('intent','(none)')}")
        return 0, "\n".join(lines)

    return 3, f"[aos-rescue] unknown action: {action}"

# -------- hook entrypoints (read tool payload from stdin) --------

def _read_hook_stdin() -> dict:
    """Claude Code passes a JSON payload on stdin for hooks."""
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}

def _extract_file_paths(tool_name: str, tool_input: dict) -> list[str]:
    paths = []
    if not isinstance(tool_input, dict):
        return paths
    if tool_name in ("Edit", "Write", "Read", "NotebookEdit"):
        p = tool_input.get("file_path") or tool_input.get("notebook_path")
        if p:
            paths.append(p)
    elif tool_name == "MultiEdit":
        p = tool_input.get("file_path")
        if p:
            paths.append(p)
        for edit in tool_input.get("edits", []) or []:
            ep = edit.get("file_path") if isinstance(edit, dict) else None
            if ep:
                paths.append(ep)
    return [p for p in paths if p]

def _read_intent_file(project_dir: str | None) -> str:
    if not project_dir:
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    candidate = Path(project_dir) / ".claude" / "last_prompt.txt"
    try:
        return candidate.read_text(encoding="utf-8", errors="ignore")[:200]
    except Exception:
        return ""

def hook_preuse_edit() -> int:
    """PreToolUse hook for Edit|Write|MultiEdit|NotebookEdit. Blocks if locked."""
    payload = _read_hook_stdin()
    tool_name = payload.get("tool_name") or payload.get("tool", "")
    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id") or _session_id()
    agent = _agent_from_env()
    intent = _read_intent_file(payload.get("cwd") or payload.get("project_dir"))

    paths = _extract_file_paths(tool_name, tool_input)
    if not paths:
        return 0

    for p in paths:
        rc, msg = cmd_acquire(p, session_id, agent, intent=intent)
        if rc != 0:
            print(msg, file=sys.stderr)
            return rc
    return 0

def hook_postuse_edit() -> int:
    """PostToolUse for Edit|Write|MultiEdit|NotebookEdit. Refresh TTL + update fingerprint."""
    payload = _read_hook_stdin()
    tool_name = payload.get("tool_name") or payload.get("tool", "")
    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id") or _session_id()
    agent = _agent_from_env()

    paths = _extract_file_paths(tool_name, tool_input)
    if not paths:
        return 0

    cmd_heartbeat(session_id, agent)
    # Refresh OUR fingerprint to the post-edit content so we don't block ourselves.
    for p in paths:
        _record_read(session_id, _norm_path(p))
    return 0

def hook_postuse_read() -> int:
    """PostToolUse for Read. Record the file fingerprint for stale-read detection."""
    payload = _read_hook_stdin()
    tool_name = payload.get("tool_name") or payload.get("tool", "")
    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id") or _session_id()

    paths = _extract_file_paths(tool_name, tool_input)
    for p in paths:
        _record_read(session_id, _norm_path(p))
    return 0

def hook_session_start() -> int:
    payload = _read_hook_stdin()
    session_id = payload.get("session_id") or _session_id()
    agent = _agent_from_env()
    cmd_session_start(session_id, agent)
    return 0

def hook_session_end() -> int:
    payload = _read_hook_stdin()
    session_id = payload.get("session_id") or _session_id()
    cmd_session_end(session_id)
    return 0

def hook_user_prompt() -> int:
    """UserPromptSubmit: stash the latest user prompt as 'intent' for this project."""
    payload = _read_hook_stdin()
    prompt = (payload.get("prompt") or "").strip()
    project_dir = payload.get("cwd") or payload.get("project_dir") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    if not prompt:
        return 0
    try:
        target = Path(project_dir) / ".claude"
        target.mkdir(parents=True, exist_ok=True)
        (target / "last_prompt.txt").write_text(prompt, encoding="utf-8")
    except Exception:
        pass
    return 0

# -------- main --------

USAGE = """\
aos_lock.py <verb> [args]

Direct verbs:
  acquire <path> [--session ID] [--agent NAME] [--intent TXT] [--ttl SECS]
  release <path> [--session ID]
  release-all [--session ID]
  check <path>
  list
  heartbeat [--session ID] [--agent NAME]
  session-start [--session ID] [--agent NAME]
  session-end [--session ID]

Rescue verbs (operational recovery):
  rescue status                                     human report on locks + sessions
  rescue fingerprint --session ID --paths "p1;p2"   seed read fingerprints (semicolon-separated)
  rescue orphans                                    release locks whose session is dead
  rescue nuke                                       wipe ALL state (locks/sessions/reads)

Hook entrypoints (read JSON payload from stdin):
  hook preuse-edit
  hook postuse-edit
  hook postuse-read
  hook session-start
  hook session-end
  hook user-prompt
"""

def _parse_kv(args: list[str]) -> dict:
    out = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--") and i + 1 < len(args):
            out[a[2:]] = args[i + 1]
            i += 2
        else:
            i += 1
    return out

def main(argv: list[str]) -> int:
    _ensure_dirs()

    if len(argv) < 2:
        print(USAGE, file=sys.stderr)
        return 3

    verb = argv[1]

    if verb == "hook":
        if len(argv) < 3:
            print(USAGE, file=sys.stderr)
            return 3
        sub = argv[2]
        try:
            if sub == "preuse-edit":  return hook_preuse_edit()
            if sub == "postuse-edit": return hook_postuse_edit()
            if sub == "postuse-read": return hook_postuse_read()
            if sub == "session-start": return hook_session_start()
            if sub == "session-end":   return hook_session_end()
            if sub == "user-prompt":   return hook_user_prompt()
        except Exception as e:
            print(f"[aos-lock] hook error: {e}", file=sys.stderr)
            return 0  # never block on internal hook errors
        print(f"[aos-lock] unknown hook: {sub}", file=sys.stderr)
        return 3

    kv = _parse_kv(argv[2:])
    session_id = _session_id(kv.get("session"))
    agent = _agent_from_env(kv.get("agent"))
    rc, msg = 0, ""

    try:
        if verb == "acquire":
            path = argv[2] if len(argv) > 2 and not argv[2].startswith("--") else kv.get("path", "")
            ttl = int(kv.get("ttl") or DEFAULT_TTL_SECONDS)
            rc, msg = cmd_acquire(path, session_id, agent, intent=kv.get("intent", ""), ttl=ttl)
        elif verb == "release":
            path = argv[2] if len(argv) > 2 and not argv[2].startswith("--") else kv.get("path", "")
            rc, msg = cmd_release(path, session_id)
        elif verb == "release-all":
            rc, msg = cmd_release_all(session_id)
        elif verb == "check":
            path = argv[2] if len(argv) > 2 and not argv[2].startswith("--") else kv.get("path", "")
            rc, msg = cmd_check(path)
        elif verb == "list":
            rc, msg = cmd_list()
        elif verb == "heartbeat":
            rc, msg = cmd_heartbeat(session_id, agent)
        elif verb == "session-start":
            rc, msg = cmd_session_start(session_id, agent)
        elif verb == "session-end":
            rc, msg = cmd_session_end(session_id)
        elif verb == "rescue":
            if len(argv) < 3:
                print(USAGE, file=sys.stderr)
                return 3
            action = argv[2]
            paths_raw = kv.get("paths") or kv.get("path", "")
            paths = [p.strip() for p in paths_raw.split(";") if p.strip()] if paths_raw else []
            rc, msg = cmd_rescue(action, session_id=session_id if kv.get("session") else None, paths=paths)
        else:
            print(USAGE, file=sys.stderr)
            return 3
    except Exception as e:
        print(f"[aos-lock] error: {e}", file=sys.stderr)
        return 4

    if msg:
        print(msg, file=sys.stderr if rc else sys.stdout)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
