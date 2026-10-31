"""Stdlib-only taint state and exact file exemptions shared with session hooks.

State: ~/.local/state/web-taint/<agent>-<session_id>.json (directory 0700,
files 0600). Writes use a same-directory temporary file and os.replace.
Primary writes fall back to the runtime store. Readers and recovery use both.
Environment overrides are resolved at call time. Absent state is clean; corrupt
or unreadable state is tainted with SCANNER.
Exemptions: ~/.claude/hooks/web-taint/exempt.json, mapping exact realpaths to
sha256 hashes. An edited file stops matching. Tests patch STATE_DIR and
EXEMPT_FILE so they never access the user's state.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

STATE_DIR = Path.home() / ".local" / "state" / "web-taint"
FALLBACK_DIR = Path("/run/user") / str(os.getuid()) / "web-taint"
EXEMPT_FILE = Path.home() / ".claude" / "hooks" / "web-taint" / "exempt.json"


def state_dirs() -> tuple[Path, Path]:
    runtime = Path(os.environ['XDG_RUNTIME_DIR']) / 'web-taint' if 'XDG_RUNTIME_DIR' in os.environ else FALLBACK_DIR
    return (Path(os.environ.get('WEB_TAINT_STATE_DIR', STATE_DIR)),
            Path(os.environ.get('WEB_TAINT_FALLBACK_DIR', runtime)))


def _exempt_file() -> Path:
    return Path(os.environ.get('WEB_TAINT_EXEMPT_FILE', EXEMPT_FILE))


def _state_paths(agent: str, session_id: str) -> tuple[Path, ...]:
    name = state_path(agent, session_id).name
    return tuple(dict.fromkeys(directory / name for directory in state_dirs()))


def state_path(agent: str, session_id: str) -> Path:
    """Validate identifiers before using them as one filename."""
    if not re.fullmatch(r"[A-Za-z0-9_]+", agent) or not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        raise ValueError("invalid agent or session id")
    return state_dirs()[0] / f"{agent}-{session_id}.json"


def is_tainted(agent: str, session_id: str) -> dict | None:
    for path in _state_paths(agent, session_id):
        if state := _read_state(path, agent, session_id):
            return state
    return None


def _read_state(path: Path, agent: str, session_id: str) -> dict | None:
    try:
        info = path.parent.stat()
        if not stat.S_ISDIR(info.st_mode) or not info.st_mode & 0o444 or not info.st_mode & 0o111:
            raise ValueError("unreadable taint directory")
        with os.scandir(path.parent):
            pass

        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("invalid taint state file")
        state = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(state, dict) or not isinstance(state.get("rules"), list)
                or not state["rules"] or not all(isinstance(rule, str) and rule for rule in state["rules"])):
            raise ValueError("invalid taint state")
        return state
    except FileNotFoundError:
        try:
            path.lstat()  # A dangling link is unreadable state, not an absent record.
        except FileNotFoundError:
            if not path.parent.is_symlink() or path.parent.exists():
                return None
        except OSError:
            pass
    except (OSError, ValueError, RecursionError):
        pass
    return {"agent": agent, "session_id": session_id, "rules": ["SCANNER"]}


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            os.fchmod(file.fileno(), 0o600)
            json.dump(value, file, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def mark(agent: str, session_id: str, rules: list[str], source: str, cwd: str, *, detail: str = "",
         extra: dict | None = None) -> None:
    """detail names what was read (a path, URL or program); extra adds guarded-mode fields."""
    if not isinstance(rules, list) or not rules or not all(isinstance(rule, str) and rule for rule in rules):
        raise ValueError("taint rules must be nonempty")
    _write(agent, session_id, {"agent": agent, "session_id": session_id, "rules": rules, "source": source,
                               "cwd": cwd, "time": time.time(), "detail": detail, **(extra or {})})


def _write(agent: str, session_id: str, record: dict) -> None:
    for path in _state_paths(agent, session_id):
        try:
            _atomic_json(path, record)
            return
        except OSError:
            pass
    raise OSError("taint stores unavailable")


def update(agent: str, session_id: str, change) -> dict | None:
    """Read-modify-write under an exclusive lock: change(record or None) returns the new record, or None to clear."""
    path = state_path(agent, session_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path.parent / f".{path.name}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = change(is_tainted(agent, session_id))
        if record is None:
            clear(agent, session_id)
        else:
            _write(agent, session_id, record)
        return record


def clear(agent: str, session_id: str) -> None:
    error = None
    for path in _state_paths(agent, session_id):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            error = exc
    if error:
        raise error


def untaint(prefix: str | None = None, *, all_sessions: bool = False) -> int:
    """List state filenames, or clear a unique session-id/name prefix or all."""
    if prefix is not None and all_sessions:
        raise ValueError("choose a prefix or --all")
    records = {}
    for directory in dict.fromkeys(state_dirs()):
        try:
            for path in directory.iterdir():
                if path.name.endswith(".json"):
                    records.setdefault(path.stem, set()).add(path)
        except FileNotFoundError:
            if directory.is_symlink():
                raise OSError("unreadable taint directory") from None
    names = sorted(records)
    if prefix is None and not all_sessions:
        for name in names:
            print(name)
        if not names:
            print("web untaint: no tainted sessions")
        return 0
    if not all_sessions:
        if not prefix:
            raise ValueError("session prefix must be nonempty")
        names = [name for name in names if name.startswith(prefix)
                 or name.partition("-")[2].startswith(prefix)]
        if len(names) > 1:
            raise ValueError("ambiguous session prefix")
    count, error = 0, None
    for name in names:
        removed = False
        for path in records[name]:
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                pass
            except OSError as exc:
                error = exc
        count += int(removed)
    if error:
        raise error
    print(f"web untaint: cleared {count} session(s)")
    return count


def _realpath(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _sha256(path: Path) -> str:
    # O_NONBLOCK lets us reject a FIFO without waiting for a writer.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise ValueError("exemption requires an exact regular file")
        return hashlib.file_digest(file, "sha256").hexdigest()


def exempt_list() -> dict[str, str]:
    path = _exempt_file()
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("invalid exemption state file")
        entries = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not path.is_symlink():
            return {}
        raise ValueError("unreadable exemption state") from None
    except (OSError, ValueError, RecursionError):
        raise ValueError("unreadable exemption state") from None
    if (not isinstance(entries, dict) or not all(isinstance(path, str) and Path(path).is_absolute()
            and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
            for path, digest in entries.items())):
        raise ValueError("invalid exemption state")
    return entries


def exempt_add(path: str | Path) -> str:
    try:
        real = _realpath(path)
        digest = _sha256(real)
    except (OSError, ValueError, RuntimeError):
        raise ValueError("exemption requires an exact regular file") from None
    entries = exempt_list()
    entries[str(real)] = digest
    _atomic_json(_exempt_file(), entries)
    return digest


def exempt_rm(path: str | Path) -> bool:
    real = str(_realpath(path))
    entries = exempt_list()
    if real not in entries:
        return False
    del entries[real]
    _atomic_json(_exempt_file(), entries)
    return True


def exempt_match(paths: list[str]) -> bool:
    if not paths:
        return False
    try:
        entries = exempt_list()
        for path in paths:
            real = _realpath(path)
            if str(real) not in entries or _sha256(real) != entries[str(real)]:
                return False
        return True
    except (OSError, ValueError, RuntimeError):
        return False
