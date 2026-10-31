"""The link line and background mode for `web handoff` and `web cred intake`.

Agents call web under tool timeouts (about two minutes) while a handoff can wait up to 15 minutes, so the link must
reach the caller at once and the waiting must be able to move out of the caller's way:

- announce(): the `handoff:` / `intake:` line goes to stdout AND stderr (flushed) and to STATE_DIR/last-link (0600),
  so a caller that only sees one stream, or only a file, still gets it;
- spawn(): `--background`. A run of the same kind already live (its <kind>.active marker, or a background record
  still running) is refused here, in the parent, before anything forks or is written. Otherwise the process forks;
  the child starts its own session (setsid), points its stdio at a 0600 log under STATE_DIR/logs and closes every
  other inherited descriptor (an agent's Bash tool waits for its pipes to close, not for the process), then runs the
  command. The parent waits only for the link line (a pipe from the child), prints it, and exits 0. A child that
  fails before it has a link exits with its own code, which the parent passes on with the child's log;
- status() / wait(): `--status` / `--wait [SEC]` read STATE_DIR/<kind>.bg.json (pid, status, link, log, code). The
  parent writes it as it forks (under <kind>.bg.lock, with the live check, so two racing runs can't both start);
  the child only updates its own record (its pid), when it has the link and when it ends. One file per kind: the
  latest background run;
- cancel(): `--cancel` sends SIGTERM to the live run (marker or record) once /proc proves it is that run (the start
  time recorded beside the pid, this uid, argv running web with the subcommand), then waits for its teardown
  (tailnet path off, marker gone).
"""
import contextlib
import fcntl
import json
import os
import select
import signal
import sys
import time
import threading
from pathlib import Path

from . import act

LAST_LINK = "last-link"
START_WAIT = 90.0   # seconds the parent waits for the child's link line (the handoff's setup is a few seconds)
WAIT_SEC = 100.0    # `--wait` default: under an agent tool's two-minute timeout
LOG_DAYS = 7        # background logs older than this are pruned when the next one starts
CANCEL_WAIT = 20.0  # a handoff's teardown (tailnet path off, relay joined) after SIGTERM takes a few seconds
_ready_fd: int | None = None   # in a background child: the pipe the link line goes up to the parent
_kind: str | None = None       # in a background child: whose state file announce() updates


def _dir():
    return act.STATE_DIR


def _state_path(kind: str):
    return _dir() / f"{kind}.bg.json"


def read_state(kind: str) -> dict | None:
    try:
        st = json.loads(_state_path(kind).read_text())
    except (OSError, ValueError):
        return None
    return st if isinstance(st, dict) else None


def _cmd(kind: str) -> str:
    return "cred intake" if kind == "intake" else kind


@contextlib.contextmanager
def _locked(kind: str):
    """One writer of a kind's record at a time: the parent's check-and-record, then its child's updates."""
    _dir().mkdir(parents=True, exist_ok=True)
    with open(_dir() / f"{kind}.bg.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield   # closing the file releases the lock


def _save(kind: str, st: dict) -> None:
    act._write_private(_state_path(kind), json.dumps(st))


def _update(kind: str, pid: int, **fields) -> None:
    """A background child's update of its own record (the one its parent wrote with its pid), never anyone else's."""
    with contextlib.suppress(OSError), _locked(kind):
        st = read_state(kind)
        if st is not None and st.get("pid") == pid:
            st.update(fields)
            _save(kind, st)


def _start_of(st: dict) -> int | None:
    start = st.get("start")
    return start if type(start) is int else None


def _record_owner(kind: str) -> tuple[int, int | None] | None:
    """(pid, start time) of a background run whose record says running and whose process is still that process."""
    st = read_state(kind) or {}
    pid = st.get("pid")
    if st.get("status") == "running" and type(pid) is int and pid > 0 and act.alive(pid, _start_of(st)):
        return pid, _start_of(st)
    return None


def _live_owner(kind: str) -> tuple[int, int | None] | None:
    """A live run of this kind: whoever holds its marker (a foreground or background run, while it works), or a
    background run still starting (its record says running; it may not have reached the marker yet)."""
    return act.marker_owner(kind) or _record_owner(kind)


def live_pid(kind: str) -> int | None:
    owner = _live_owner(kind)
    return owner[0] if owner else None


class Busy(Exception):
    def __init__(self, pid: int):
        super().__init__(f"already running (pid {pid})")
        self.pid = pid


@contextlib.contextmanager
def claim(kind: str):
    """Become the one live run of this kind: under the kind's lock, raise Busy while another run (marker or record,
    not this process) is live, else take the marker (act.hold) until the block ends."""
    with contextlib.ExitStack() as stack:
        with _locked(kind):
            if (busy := live_pid(kind)) and busy != os.getpid():
                raise Busy(busy)
            stack.enter_context(act.hold(kind))
            if kind == "handoff":
                from . import identities
                stack.enter_context(identities.hold_handoff_identity())
        yield


def announce(line: str) -> None:
    """Publish the link line: stdout and stderr (flushed), last-link (0600) and, in a background child, the
    parent's readiness pipe and the state file."""
    global _ready_fd
    print(line, flush=True)
    print(line, file=sys.stderr, flush=True)
    with contextlib.suppress(OSError):
        _dir().mkdir(parents=True, exist_ok=True)
        act._write_private(_dir() / LAST_LINK, line + "\n")
    if _kind:
        _update(_kind, os.getpid(), link=line)
    if _ready_fd is not None:
        with contextlib.suppress(OSError):
            os.write(_ready_fd, (line + "\n").encode())
            os.close(_ready_fd)
        _ready_fd = None


def _prune_logs(logs) -> None:
    cutoff = time.time() - LOG_DAYS * 86400
    for p in logs.glob("*.log"):
        with contextlib.suppress(OSError):
            if p.stat().st_mtime < cutoff:
                p.unlink()


def _child(kind: str, log, w: int, work) -> None:
    """The background child: detach, run work() -> (word, code), record it, exit. Never returns."""
    global _ready_fd, _kind
    code, word = 5, "error"
    try:
        os.setsid()
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        # Codex capture replaces Python streams with StringIO before the fork.
        sys.stdout = os.fdopen(1, "w", buffering=1, encoding="utf-8", errors="replace", closefd=False)
        sys.stderr = os.fdopen(2, "w", buffering=1, encoding="utf-8", errors="replace", closefd=False)
        keep = sorted({0, 1, 2, w})
        for lo, hi in zip(keep, [*keep[1:], os.sysconf("SC_OPEN_MAX")], strict=True):
            os.closerange(lo + 1, hi)   # nothing else inherited (an agent's pipes) stays open in the child
        _ready_fd, _kind = w, kind   # its record is the parent's until announce() adds the link
        try:
            word, code = work()
        except SystemExit:
            word, code = "interrupted", 5
        except BaseException:   # noqa: BLE001 - recorded, never raised past the fork
            word, code = "error", 5
    finally:
        with contextlib.suppress(BaseException):
            _update(kind, os.getpid(), status=word, code=code, ended=int(time.time()))
            sys.stdout.flush()
            sys.stderr.flush()
        os._exit(code)


def _read_line(r: int, timeout: float) -> str | None:
    """The first line the child sends; "" if it closed the pipe (it ended), None if the time ran out."""
    buf, deadline = b"", time.monotonic() + timeout
    while b"\n" not in buf:
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([r], [], [], left)[0]:
            return None
        chunk = os.read(r, 4096)
        if not chunk:
            return ""
        buf += chunk
    return buf.split(b"\n", 1)[0].decode("utf-8", "replace")


def spawn(kind: str, work) -> int:
    """`--background`: run work() -> (word, code) in a detached child; print its link line here and return 0, or the
    child's own exit code (with its log) when it ended without one."""
    cmd = _cmd(kind)
    with _locked(kind):   # the live check and the new record are one step
        if busy := live_pid(kind):   # refused here: the live run's record, marker and link stay as they are
            print(f"web {cmd}: already running (pid {busy}); `web {cmd} --status` shows its link, "
                  f"`web {cmd} --cancel` stops it", file=sys.stderr)
            return 2
        if threading.active_count() > 1:
            print("web background: spawn requires a single-threaded caller", file=sys.stderr)
            return 5
        logs = _dir() / "logs"
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        _prune_logs(logs)
        log = logs / f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.log"
        r, w = os.pipe()
        sys.stdout.flush()
        sys.stderr.flush()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            _child(kind, log, w, work)
        os.close(w)
        _save(kind, {"pid": pid, "start": act.proc_start(pid), "status": "running", "log": str(log),
                     "started": int(time.time()), "link": None, "code": None})
    try:
        line = _read_line(r, START_WAIT)
    finally:
        os.close(r)
    if line:
        print(line, flush=True)
        print(line, file=sys.stderr, flush=True)
        print(f"web {kind}: waiting in the background (pid {pid}, log {log}); "
              f"`web {cmd} --wait` (or --status) reports how it ended", file=sys.stderr, flush=True)
        return 0
    done, status = 0, 0
    for _ in range(100 if line == "" else 1):   # the pipe closed as the child exited: a moment to make it reapable
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        time.sleep(0.05)
    if not done:   # no link yet and still running: leave it alone, say where to look
        print(f"web {kind}: no link after {START_WAIT:g}s (pid {pid}, log {log}); the {kind} may still publish its "
              f"link — check `web {cmd} --status`", file=sys.stderr)
        return 5
    with contextlib.suppress(OSError):
        for row in log.read_text(errors="replace").splitlines():
            if not row.startswith("web: host="):   # the child's footer; this process prints its own
                print(row, file=sys.stderr)
    return os.waitstatus_to_exitcode(status) if os.WIFEXITED(status) else 5


def _web_run_of(argv: list[bytes], kind: str) -> bool:
    """argv runs the web entry point (a word whose basename is `web`, or `-m web`) with `kind` after it."""
    for i, word in enumerate(argv):
        entry = word.rsplit(b"/", 1)[-1] == b"web" or (word == b"web" and i > 0 and argv[i - 1] == b"-m")
        if entry:
            return kind.encode() in argv[i + 1:]
    return False


def _ours(pid: int, start: int | None, kind: str) -> bool:
    """Proof, not a guess, that pid is the run that was recorded: the start time recorded with it (a reused pid has
    another), this user's process, and an argv that runs web's entry point with the subcommand word."""
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        uid = Path(f"/proc/{pid}").stat().st_uid
    except OSError:
        return False
    return start is not None and act.proc_start(pid) == start and uid == os.getuid() and _web_run_of(argv, kind)


def cancel(kind: str) -> tuple[str, int | None]:
    """(word, pid): none | cancelled | refused (not provably ours) | still-running (after CANCEL_WAIT)."""
    owner = _live_owner(kind)
    if owner is None:
        return "none", None
    pid, start = owner
    if not _ours(pid, start, kind):
        return "refused", pid
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)   # the run's own SIGTERM path tears down and records "interrupted"
    deadline = time.monotonic() + CANCEL_WAIT
    while act.alive(pid, start) and time.monotonic() < deadline:
        time.sleep(0.1)
    return ("still-running" if act.alive(pid, start) else "cancelled"), pid


def status(kind: str) -> tuple[str, dict | None]:
    """(word, state): none | running | died (its process is gone without a record) | how it ended."""
    st = read_state(kind)
    if st is None:
        return "none", None
    word = st.get("status") if isinstance(st.get("status"), str) else "error"
    pid = st.get("pid")
    if word == "running" and not (type(pid) is int and pid > 0 and act.alive(pid, _start_of(st))):
        return "died", st
    return word, st


def wait(kind: str, seconds: float) -> tuple[str, dict | None]:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        word, st = status(kind)
        if word != "running" or time.monotonic() >= deadline:
            return word, st
        time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))


def exit_code(word: str, st: dict | None) -> int:
    """running → 4 (still needs the human); none → 2; died → 5; an ended run → its own exit code."""
    if word == "none":
        return 2
    if word == "running":
        return 4
    code = (st or {}).get("code")
    return code if isinstance(code, int) and not isinstance(code, bool) and word != "died" else 5
