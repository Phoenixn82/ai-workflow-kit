"""Run one read engine on one URL inside its own systemd scope.

The scope (`systemd-run --user --scope`) owns the engine's whole process tree, including Playwright browsers that
detach into their own sessions: at the timeout web stops the scope itself (a requested stop, so a slow page's timeout
is reported to the caller as `error: timeout` without systemd recording a failed unit), and leftovers are stopped as
soon as the engine exits. RuntimeMaxSec=<timeout + BACKSTOP> is only the backstop for a web that was killed outright. systemd-run keeps the caller's env (it must reach the user manager); the engine runs under
`env -i` with an explicit safe env: no Wayland/Hyprland/D-Bus variables, a private 0700 XDG_RUNTIME_DIR (also TMPDIR
and cwd, removed afterwards), and a DISPLAY only for a headed engine, only on an Xvfb this process started.

Engine stdout contract (Phase A): the last line that parses as a JSON object is the record; lines split on "\\n" only.
Output goes to unlinked temp files, not pipes, so a leftover holding stdout can't stall the read.
Inside WEB_NETJAIL, engines and Xvfb inherit the outer scope and namespaces.
Timeouts and a child subreaper replace manager launches that would escape it.
"""
import contextlib
import ctypes
import json
import math
import os
import shutil
import signal
import subprocess
import tempfile
import time
import threading
import uuid
from pathlib import Path

MAX_OUT = 40 * 1024 * 1024
STOP_GRACE = 5    # scope TimeoutStopSec: SIGTERM first, SIGKILL this many seconds later
BACKSTOP = 15     # RuntimeMaxSec slack past the timeout: systemd ends the scope itself only if web didn't
KEYS = ("status", "final_url", "html", "markdown", "headers", "error", "version", "rung")
# The only parent variables an engine sees (plus LC_*). Everything else (WAYLAND_*, HYPRLAND_*, DBUS_*, DISPLAY,
# tokens) is dropped. They travel on the `env -i` argv, so nothing that can carry a secret belongs here (that is why
# proxy URLs, which may embed credentials, are not passed).
PASS_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TZ", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
            "XDG_DATA_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR", "PLAYWRIGHT_BROWSERS_PATH", "UV_CACHE_DIR",
            "UV_PYTHON_INSTALL_DIR")

UNIT_PREFIX = "web-read"   # tests use web-test-read, so fixture scopes stay apart from real reads in the journal
OWNED_DISPLAYS: set[str] = set()   # displays xvfb() started in this process: the only ones a headed engine may use


def _blank(**kw) -> dict:
    r = dict.fromkeys(KEYS)
    r.update(html="", headers={})
    r.update(kw)
    return r


def _last_json(out: bytes) -> dict | None:
    text = out[:MAX_OUT].decode("utf-8", "replace")
    for line in reversed(text.split("\n")):   # "\n" only: U+2028 etc. may sit inside a JSON string
        line = line.rstrip("\r").strip()
        if line.startswith("{"):
            try:
                v = json.loads(line)
            except ValueError:
                continue
            if isinstance(v, dict):
                return v
    return None


def safe_env(work: str, display: str | None) -> dict:
    env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
    env.update({k: v for k, v in os.environ.items() if k.startswith("LC_")})
    # A private runtime dir holds no wayland-*/bus socket, so a browser forced to Wayland (this host's
    # ~/.config/chromium-flags.conf) or probing $XDG_RUNTIME_DIR/bus finds nothing of the live session.
    env.update(XDG_RUNTIME_DIR=work, TMPDIR=work, XDG_SESSION_TYPE="x11", GDK_BACKEND="x11", QT_QPA_PLATFORM="xcb",
               MOZ_ENABLE_WAYLAND="0")
    if display:
        env["DISPLAY"] = display
    return env


def _scope(unit: str, max_sec: float) -> list[str]:
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}",
            "-p", f"RuntimeMaxSec={max(1, math.ceil(max_sec))}", "-p", f"TimeoutStopSec={STOP_GRACE}", "--"]


def _systemctl(*args: str) -> None:
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(["systemctl", "--user", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=STOP_GRACE + 10)


def _stop_scope(unit: str, kill: bool = False) -> None:
    """Stop the scope (SIGTERM, SIGKILL after STOP_GRACE) and wait for it; a scope that already ended is a no-op."""
    if kill:
        _systemctl("kill", "--signal=SIGKILL", f"{unit}.scope")
    _systemctl("stop", f"{unit}.scope")


def _tail(f, n: int = 500) -> str:
    f.seek(0, os.SEEK_END)
    f.seek(max(0, f.tell() - n))
    return f.read().decode("utf-8", "replace").strip()


def _children(pid: int) -> set[int]:
    with contextlib.suppress(OSError, ValueError):
        return {int(p) for p in Path(f"/proc/{pid}/task/{pid}/children").read_text().split()}
    return set()


@contextlib.contextmanager
def _inherited_tree():
    """Adopt and reap even browsers that fork and detach from their engine.

    Existing children (the read's Xvfb) belong to the caller and are preserved.
    Only jailed read processes use this, never the identity-browser runner.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) or libc.prctl(36, 1, 0, 0, 0):
        raise OSError(ctypes.get_errno(), "cannot supervise jailed engine")
    existing = _children(os.getpid())
    try:
        yield
    finally:
        # Killed children can reparent their descendants to this subreaper.
        # Repeat until every new child is reaped, including detached sessions.
        deadline = time.monotonic() + STOP_GRACE
        while time.monotonic() < deadline:
            roots = _children(os.getpid()) - existing
            if not roots:
                break
            pending, owned = list(roots), set()
            while pending:
                pid = pending.pop()
                if pid in owned:
                    continue
                owned.add(pid)
                pending.extend(_children(pid))
            for pid in owned:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            for pid in roots:
                with contextlib.suppress(ChildProcessError):
                    os.waitpid(pid, os.WNOHANG)
            time.sleep(0.01)
        libc.prctl(36, previous.value, 0, 0, 0)


def _kill_inherited(proc):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def run_engine(argv: list[str], url: str, timeout: float, display: str | None = None, headed: bool = False) -> dict:
    """Run `argv + [url]` in the inherited jail or a fresh scope; engine failures become `error`."""
    if headed and display not in OWNED_DISPLAYS:
        return _blank(error=f"refused: headed engine needs a display web started (got {display!r})", ms=0)
    work = tempfile.mkdtemp(prefix="web-engine-")   # 0700
    unit = f"{UNIT_PREFIX}-{uuid.uuid4().hex[:12]}"
    env = [f"{k}={v}" for k, v in safe_env(work, display if headed else None).items()]
    jailed = os.environ.get("WEB_NETJAIL") == "1"
    cmd = ([] if jailed else _scope(unit, timeout + BACKSTOP)) + ["env", "-i", *env, *argv, url]
    t = time.monotonic()
    try:
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err, (
                _inherited_tree() if jailed else contextlib.nullcontext()):
            try:
                p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=err, cwd=work,
                                     start_new_session=jailed)
            except OSError as e:
                return _blank(error=f"spawn: {e}", ms=0)
            try:
                p.wait(timeout)
            except subprocess.TimeoutExpired:
                if jailed:
                    _kill_inherited(p)
                else:
                    _stop_scope(unit)   # SIGTERM, SIGKILL after STOP_GRACE: ends cleanly unless the engine resists
                    try:
                        p.wait(STOP_GRACE)
                    except subprocess.TimeoutExpired:   # the user manager didn't end it; make sure
                        _stop_scope(unit, kill=True)
                        p.kill()
                        p.wait()
            finally:
                if jailed:
                    _kill_inherited(p)
                else:
                    _stop_scope(unit)   # reap whatever the engine left behind (detached browsers)
            ms = int((time.monotonic() - t) * 1000)
            out.seek(0)
            rec = _last_json(out.read(MAX_OUT))
            if rec is None:
                if ms >= timeout * 1000:
                    return _blank(error=f"timeout {timeout:g}s", ms=ms)
                return _blank(error="bad output: " + _tail(err), ms=ms)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    r = _blank(**{k: rec.get(k) for k in KEYS if k in rec})
    r["html"] = r["html"] or ""
    r["headers"] = r["headers"] or {}
    r["ms"] = ms
    return r


def _taken(n: int) -> bool:
    """A lock names a live holder; a dead pid's lock is stale and Xvfb replaces it, so :n is free."""
    try:
        pid = int(Path(f"/tmp/.X{n}-lock").read_text().strip())
    except FileNotFoundError:
        return Path(f"/tmp/.X11-unix/X{n}").exists()
    except (OSError, ValueError):
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass
    return True


def _owns(proc: subprocess.Popen, n: int, wait: float = 5.0) -> bool:
    """True once Xvfb :n is up and its lock file names our process (not a concurrent web's or anyone else's)."""
    lock, sock = Path(f"/tmp/.X{n}-lock"), Path(f"/tmp/.X11-unix/X{n}")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        with contextlib.suppress(OSError, ValueError):
            if sock.exists() and int(lock.read_text().strip()) == proc.pid:
                return True
        time.sleep(0.05)
    return False


@contextlib.contextmanager
def xvfb(width: int = 1920, height: int = 1080, max_sec: float = 900):
    """Start a private Xvfb on the first free :N (N >= 99) in its own scope (it can't outlive max_sec even if web is
    killed); yield ':N', registered in OWNED_DISPLAYS for the headed guard; always stop it."""
    if not shutil.which("Xvfb"):
        raise RuntimeError("Xvfb missing: sudo pacman -S --needed xorg-server-xvfb")
    jailed = os.environ.get("WEB_NETJAIL") == "1"
    if jailed:
        Path("/tmp/.X11-unix").mkdir(mode=0o1777, exist_ok=True)
    for n in range(99, 200):
        if _taken(n):
            continue
        unit = f"web-xvfb-{uuid.uuid4().hex[:12]}"
        with tempfile.TemporaryFile() as err:   # Xvfb keeps its own fd; ours is only for a startup failure
            proc = subprocess.Popen(([] if jailed else _scope(unit, max_sec)) + ["Xvfb", f":{n}", "-screen", "0", f"{width}x{height}x24",
                                                             "-nolisten", "tcp",
                                                             *(["-extension", "GLX"] if jailed else [])],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                    start_new_session=jailed)
            if _owns(proc, n):
                break
            if jailed:
                _kill_inherited(proc)
            else:
                _stop_scope(unit)
                proc.wait()
            if not _taken(n):   # nobody else holds :n, so this was a start failure, not a race for the number
                raise RuntimeError(f"Xvfb :{n} did not start: {_tail(err)}")
    else:
        raise RuntimeError("no free X display in :99-:199")
    disp = f":{n}"
    OWNED_DISPLAYS.add(disp)
    timer = threading.Timer(max_sec, _kill_inherited, args=(proc,)) if jailed else None
    if timer:
        timer.daemon = True
        timer.start()
    try:
        yield disp
    finally:
        OWNED_DISPLAYS.discard(disp)
        if jailed:
            timer.cancel()
            _kill_inherited(proc)
        else:
            _stop_scope(unit)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(STOP_GRACE + 5)
