"""The identity browser and `web act`, a thin pass-through to agent-browser.

Launch (Task 8 stealth spike; lanes.json act.identity_launch): web starts /usr/bin/chromium itself on a private Xvfb,
both as transient systemd user services, so they outlive this CLI until `web act close` (or the MAX_SEC cap, so a
forgotten browser can't run forever: a web-identity-cap[-<name>] timer stops both units cleanly, so the cap isn't
recorded as a failed unit; RuntimeMaxSec is only the backstop if that timer is missing). agent-browser only attaches over CDP (`--cdp <port>`) for each command. Why not
agent-browser's own launch: with no DISPLAY its automatic Xvfb uses -displayfd, which can take :0 and
replace a live Xwayland socket; and only a browser web owns can run with agent-browser detached, which bot checks
need (agent-browser's CDP session itself is what they flag). `web
handoff` relies on that: it detaches agent-browser for the human step.

Safety: every process gets an explicit env (runner.safe_env: no Wayland/Hyprland/D-Bus variables, no inherited
DISPLAY), a stable private XDG_RUNTIME_DIR and caller-specific session name (so later calls reach that caller's
agent-browser daemon), and an empty
XDG_CONFIG_HOME (so /usr/bin/chromium skips ~/.config/chromium-flags.conf, which forces --ozone-platform=wayland).
"""
import base64
import binascii
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect as ws_connect

from . import runner
from .config import LANES
from .fence import fence

STATE_DIR = Path.home() / ".local" / "share" / "web"
IDENTITY_DIR = STATE_DIR / "identity"   # the persistent profile (cookies, logins)
RT_DIR = STATE_DIR / "rt"               # private XDG_RUNTIME_DIR: agent-browser daemon socket, chromium's runtime files
CONFIG_DIR = STATE_DIR / "xdg-config"   # empty on purpose (see module doc)
IDENTITY = "personal"
OWNER: str | None = None
LOCK_FILE = "act.lock"
LEASE_IDLE = 900
SESSION = "web-identity"
NAMESPACE = "web"
DISPLAY_UNIT = "web-identity-display"
BROWSER_UNIT = "web-identity-browser"
MAX_SEC = 4 * 3600          # cap on one identity-browser run (relaunched on the next `web act`): the -cap timer
BROWSER_MAX_SEC = MAX_SEC + 600   # RuntimeMaxSec backstop, only reached when the cap timer didn't stop the browser
DISPLAY_MAX_SEC = BROWSER_MAX_SEC + 600   # the display outlives the browser's backstop, so its exit still has an X
START_WAIT = 20.0           # seconds for Xvfb / chromium to come up
QUIT_WAIT = 10.0            # seconds for chromium's own clean exit (ExecStop) before systemd signals what is left
SCREEN = "1920x1080x24"
CLOSE = ("close",)
READ_CMDS: tuple[str, ...] = ("snapshot", "get", "read", "eval", "console", "errors")
# The pass-through is an allowlist: the first argument must be one of these agent-browser commands (web supplies
# --cdp/--session itself, so no leading global flags at all).
# No `cookies` / `storage`: the identity profile's sessions (a Google sign-in) must not be read out or wiped.
ALLOWED_COMMANDS = {"open", "read", "click", "dblclick", "type", "fill", "press", "keyboard", "hover", "focus", "check",
                    "uncheck", "select", "drag", "upload", "download", "scroll", "scrollintoview", "wait", "screenshot",
                    "pdf", "snapshot", "eval", "back", "forward", "reload", "get", "is", "find", "tab", "console",
                    "errors", "close"}
# Flags after the command that launch, re-target or loosen trust for a browser (the last six re-target the session).
REFUSED_FLAGS = {"--headed", "--profile", "--executable-path", "--args", "--auto-connect", "--ignore-https-errors",
                 "--ca-cert", "--init-script", "--extension", "--state", "--restore", "--allow-file-access", "--proxy",
                 "--cdp", "--session", "--namespace", "--provider", "-p", "--config", "--engine",
                 # A3 review 2026-10-12: --session-name turns on restore persistence, which writes every cookie of
                 # the identity browser to ~/.agent-browser/sessions/*.json (0644); --restore-check-fn runs page JS.
                 "--session-name", "--restore-save", "--restore-check-url", "--restore-check-text",
                 "--restore-check-fn", "--enable", "--no-pin-tab", "--pin-tab"}
HANDOFF_BUSY = "a handoff is in progress; finish it on the phone or run `web act close`"
READBACK = "password fields can't be read back"
STORAGE_READBACK = "cookies and site storage can't be read back"
# An eval that reads the session out of the page: document.cookie (dotted or bracketed), Web Storage, IndexedDB and
# the Cookie Store API. Mentioning a cookie banner (`#cookie-accept`) is fine.
STORAGE_JS = re.compile(r"document\s*(?:\.\s*cookie\b|\[\s*['\"`]cookie['\"`]\s*\])"
                        r"|\b(?:localStorage|sessionStorage|indexedDB|cookieStore)\b", re.I)
# Page JavaScript (eval, `wait --fn`, a javascript: URL) runs with the signed-in session in reach, and no source-text
# filter can stop it reading cookies (audit 2026-10-12: document['coo'+'kie'] passes STORAGE_JS). So it needs an
# explicit --allow-eval, and every allowed script is appended to EVAL_LOG. ponytail: an audit trail and a speed bump,
# not a boundary: same-user code can reach the profile and CDP port anyway; OS-user separation is the real fix.
ALLOW_EVAL = "--allow-eval"
EVAL_LOG = "eval.log"
EVAL_REFUSED = (f"page JavaScript runs inside the signed-in identity browser; add {ALLOW_EVAL} to run it "
                f"(every allowed script is logged to ~/.local/share/web/{EVAL_LOG})")
PASS_SELECTORS = "pass-selectors.json"   # the --pass-sel values `web login` used (not secret): read-back guard
MAX_PASS_SELECTORS = 50
# Chromium's password manager stays off in the identity profile: the courier's secrets must never be saved into a
# store you can decrypt (--password-store=basic).
NO_PASSWORD_SAVING = {"credentials_enable_service": False, "profile": {"password_manager_enabled": False}}


class ActError(Exception):
    def __init__(self, message: str, code: int = 5):
        super().__init__(message)
        self.code = code


def select(name: str) -> None:
    from . import identities
    identities.select(name)


def caller() -> str:
    from . import identities
    return identities.caller()


def acquire() -> None:
    from . import identities
    identities.acquire()


def identity_env(display: str | None = None) -> dict:
    env = runner.safe_env(str(RT_DIR), display)
    env.update(XDG_CONFIG_HOME=str(CONFIG_DIR), AGENT_BROWSER_NAMESPACE=NAMESPACE,
               AGENT_BROWSER_NO_XVFB="1", AGENT_BROWSER_PIN_TAB="1")
    # Short session names and this root keep even the longest identity below the 103-byte socket limit.
    env["AGENT_BROWSER_SOCKET_DIR"] = str(RT_DIR / "named")
    return env


def agent_browser(args: list[str], port: str | None = None, capture: bool = False,
                  timeout: float | None = None, stdin_text: str | None = None) -> subprocess.CompletedProcess:
    from . import identities
    cmd = ["agent-browser", "--session", identities.session_name(), *(["--cdp", port] if port else []), *args]
    extra = {} if stdin_text is None else {"input": stdin_text}   # `eval --stdin`: web read it to check it first
    return subprocess.run(cmd, env=identity_env(), capture_output=capture, text=True, timeout=timeout, **extra)


def detach() -> None:
    """agent-browser lets go of the browser (its daemon exits; the browser is web's and keeps running). The next
    `web act` call reattaches with --cdp."""
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        agent_browser(["close"], capture=True, timeout=30)


@contextlib.contextmanager
def _lock(filename: str | None = None, *, blocking: bool = True):
    """Lock identity metadata, a caller's commands, or launches across identities."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / (filename or LOCK_FILE), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)


def _active(unit: str) -> bool:
    return _systemctl("is-active", "--quiet", f"{unit}.service").returncode == 0


def _stop_units() -> None:
    # The browser unit's ExecStop (stop_browser) quits chromium cleanly first; the display stops after it (After=).
    _systemctl("stop", f"{BROWSER_UNIT}.service", f"{DISPLAY_UNIT}.service")


def _unit(name: str, argv: list[str], env: dict, *props: str, max_sec: int = BROWSER_MAX_SEC) -> None:
    # The env travels on argv (`env -i K=V ...`) like runner's engines: nothing secret is in it.
    cmd = ["systemd-run", "--user", "--quiet", "--collect", f"--unit={name}", "-p", f"RuntimeMaxSec={max_sec}",
           *props, "--", "env", "-i", *(f"{k}={v}" for k, v in env.items()), *argv]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        raise ActError(f"systemd-run {name} failed: {p.stderr.strip()[-300:]}")


def _display_up(n: int) -> bool:
    """True once Xvfb :n is up and its lock file names the display unit's process (not a concurrent web's)."""
    lock, sock = Path(f"/tmp/.X{n}-lock"), Path(f"/tmp/.X11-unix/X{n}")
    deadline = time.monotonic() + START_WAIT
    while time.monotonic() < deadline:
        if not _active(DISPLAY_UNIT):
            return False
        pid = _systemctl("show", "-p", "MainPID", "--value", f"{DISPLAY_UNIT}.service").stdout.strip()
        with contextlib.suppress(OSError, ValueError):
            if sock.exists() and int(lock.read_text().strip()) == int(pid):
                return True
        time.sleep(0.05)
    return False


def _start_display() -> str:
    # An explicit :N >= 99 (never -displayfd, which starts at :0): Xvfb then refuses a display whose lock is live.
    for n in range(99, 200):
        if runner._taken(n):
            continue
        _unit(DISPLAY_UNIT, ["Xvfb", f":{n}", "-screen", "0", SCREEN, "-nolisten", "tcp"], identity_env(),
              max_sec=DISPLAY_MAX_SEC)
        if _display_up(n):
            return f":{n}"
        _stop_units()
        if not runner._taken(n):   # nobody else holds :n, so this was a start failure, not a race for the number
            raise ActError(f"Xvfb :{n} did not start")
    raise ActError("no free X display in :99-:199")


def _port_file(identity_dir: Path | None = None) -> str | None:
    with contextlib.suppress(OSError, IndexError):
        port = ((identity_dir or IDENTITY_DIR) / "DevToolsActivePort").read_text().split()[0]
        return port if port.isdigit() else None
    return None


def _cdp_alive(port: str) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3) as r:
            return "Browser" in json.loads(r.read())
    except (OSError, ValueError):
        return False


def browser_port() -> str | None:
    """The running identity browser's CDP port, or None."""
    if not _active(BROWSER_UNIT):
        return None
    port = _port_file()
    return port if port and _cdp_alive(port) else None


def _send_browser_close(port: str) -> None:
    with contextlib.suppress(OSError, ValueError, KeyError, WebSocketException, TimeoutError):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3) as r:
            url = json.loads(r.read())["webSocketDebuggerUrl"]
        with ws_connect(url, open_timeout=5, close_timeout=2, max_size=None) as ws:
            ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
            ws.recv(timeout=5)


def cdp(port: str, method: str, **params) -> dict:
    """One browser-level CDP request; never read profile/session data."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3) as r:
            url = json.loads(r.read())["webSocketDebuggerUrl"]
        with ws_connect(url, open_timeout=5, close_timeout=2, max_size=None) as ws:
            ws.send(json.dumps({"id": 1, "method": method, "params": params}))
            while True:
                msg = json.loads(ws.recv(timeout=5))
                if msg.get("id") == 1:
                    if "error" in msg:
                        raise ActError(f"browser CDP {method} failed")
                    return msg["result"]
    except (OSError, ValueError, KeyError, WebSocketException, TimeoutError) as e:
        raise ActError(f"browser CDP {method} unavailable ({type(e).__name__})") from None


def _pid_alive(pid: int) -> bool:
    try:   # a zombie (exited, not yet reaped by systemd) counts as gone
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def proc_start(pid: int) -> int | None:
    """The process's start time (/proc/<pid>/stat field 22, clock ticks since boot): with the pid, it names one
    process for good (a pid can be reused, a pid + start time can't)."""
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def alive(pid: int, start: int | None = None) -> bool:
    """pid is running (not a zombie) and, when a start time was recorded, is still that same process."""
    return _pid_alive(pid) and (start is None or proc_start(pid) == start)


def marker(kind: str) -> Path:
    """While a `web handoff` / `web cred intake` runs it holds STATE_DIR/<kind>.active ("<pid> <start time>"). A
    handoff's keeps agent-browser detached until it ends; an intake's keeps a second intake and `web act close` off
    its tailnet path."""
    return STATE_DIR / f"{kind}.active"


def marker_owner(kind: str) -> tuple[int, int | None] | None:
    """(pid, start time) of the live run holding the marker, or None (no marker, or a stale one: its process is gone
    or the pid now belongs to another process). A marker without a start time (older web) carries None."""
    try:
        parts = marker(kind).read_text().split()
        pid, start = int(parts[0]), (int(parts[1]) if len(parts) > 1 else None)
    except (OSError, ValueError, IndexError):
        return None
    return (pid, start) if pid > 0 and alive(pid, start) else None


def marker_pid(kind: str) -> int | None:
    """The pid of a running handoff / intake, or None."""
    owner = marker_owner(kind)
    return owner[0] if owner else None


@contextlib.contextmanager
def hold(kind: str):
    """Hold <kind>.active for this process; on the way out remove it, unless a newer run has taken it over. Callers
    take it through bg.claim, which checks for a live run first, under the kind's lock."""
    path, me = marker(kind), f"{os.getpid()} {proc_start(os.getpid())}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(me)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            if path.read_text().strip() == me:
                path.unlink()


def handoff_marker() -> Path:
    return marker("handoff")


def handoff_pid() -> int | None:
    return marker_pid("handoff")


def stop_browser(identity_dir: Path, pid: int | None) -> None:
    """The browser unit's ExecStop, so it runs on EVERY stop path (`web act close`, a dead-CDP restart,
    RuntimeMaxSec, the display going away): CDP Browser.close, chromium's own clean exit, which writes the cookie
    store; then wait for chromium to leave. A plain SIGTERM can lose recently set cookies
    (chromium flushes them on a timer), i.e. a fresh sign-in. systemd signals whatever is left afterwards."""
    port = _port_file(identity_dir)
    if pid and port:   # no MAINPID: chromium is already gone, and its old port may now be someone else's
        _send_browser_close(port)
    deadline = time.monotonic() + QUIT_WAIT
    while pid and _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)


def _write_private(path: Path, text: str) -> None:
    """Atomic 0600 write: a temp file next to `path`, fsync, rename."""
    tmp = path.with_name(f".{path.name}.web-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def disable_password_saving(identity_dir: Path | None = None) -> None:
    """Merge NO_PASSWORD_SAVING into the profile's Default/Preferences (created if missing; a corrupt file is
    replaced, as Chromium would). Called before every launch, while no browser holds the file."""
    default = (identity_dir or IDENTITY_DIR) / "Default"
    default.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = default / "Preferences"
    try:
        prefs = json.loads(path.read_text())
    except FileNotFoundError:
        prefs = {}
    except ValueError:
        prefs = {}
    if not isinstance(prefs, dict):
        prefs = {}
    if not isinstance(prefs.get("profile"), dict):
        prefs["profile"] = {}
    if prefs.get("credentials_enable_service") is False and prefs["profile"].get("password_manager_enabled") is False:
        return
    prefs["credentials_enable_service"] = False
    prefs["profile"]["password_manager_enabled"] = False
    _write_private(path, json.dumps(prefs))


def pass_selectors() -> list[str]:
    try:
        sels = json.loads((STATE_DIR / PASS_SELECTORS).read_text())
    except (OSError, ValueError):
        return []
    return [x for x in sels if isinstance(x, str) and x] if isinstance(sels, list) else []


def remember_pass_selector(sel: str) -> None:
    """`web login` records the password selector it filled, so `web act` can refuse to read that field back."""
    sels = [x for x in pass_selectors() if x != sel] + [sel]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _write_private(STATE_DIR / PASS_SELECTORS, json.dumps(sels[-MAX_PASS_SELECTORS:]))


def _mentions_password(text: str) -> bool:
    t = text.lower()
    return "password" in t or any(s.lower() in t for s in pass_selectors())


def _eval_script(args: list[str]) -> str | None:
    """The script an `eval` pass-through runs (base64-decoded for -b/--base64), or None for --stdin."""
    rest = args[1:]
    if "--stdin" in rest:
        return None
    script = " ".join(a for a in rest if not a.startswith("-"))
    if "-b" in rest or "--base64" in rest:
        try:
            return base64.b64decode(script, validate=False).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            return script
    return script


def _is_password_field(port: str, selector: str) -> bool:
    """Ask agent-browser for the element's type attribute (works for CSS selectors and snapshot refs)."""
    from . import handoff   # imported here: handoff imports this module
    data = handoff._ab_data(port, "get", "attr", selector, "type")
    return isinstance(data, dict) and str(data.get("value") or "").lower() == "password"


def readback_refused(args: list[str], port: str, stdin_script: str | None = None) -> str | None:
    """Why this read-back is refused, or None. `get value` on a password field and `eval` of a script that names a
    password field: a filled secret must not come back out through `web act` (the courier types it so the agent
    never sees it). `eval` reading cookies or site storage: the profile's signed-in sessions stay in it."""
    if args[:2] == ["get", "value"] and len(args) >= 3:
        return READBACK if _mentions_password(args[2]) or _is_password_field(port, args[2]) else None
    if args[:1] == ["eval"]:
        script = (stdin_script if "--stdin" in args[1:] else _eval_script(args)) or ""
        if _mentions_password(script):
            return READBACK
        return STORAGE_READBACK if STORAGE_JS.search(script) else None
    return None


def _url_scheme_text(a: str) -> str:
    """`a` as Chromium's URL parser sees it: tab/CR/LF removed anywhere, leading C0 controls and spaces stripped
    (A3 review: `tab new "java<TAB>script:..."` ran JS past a plain startswith check)."""
    return a.translate({9: None, 10: None, 13: None}).lstrip("".join(map(chr, range(33)))).lower()


def runs_js(args: list[str]) -> bool:
    return (args[0] == "eval" or (args[0] == "wait" and "--fn" in args[1:])
            or any(_url_scheme_text(a).startswith("javascript:") for a in args[1:]))


def log_eval(args: list[str], stdin_script: str | None) -> None:
    """Append one allowed script to EVAL_LOG (0600, JSON lines)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    script = stdin_script if stdin_script is not None else (_eval_script(args) if args[0] == "eval" else " ".join(args))
    line = json.dumps({"ts": int(time.time()), "cmd": args[0], "script": script}) + "\n"
    fd = os.open(STATE_DIR / EVAL_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(line)


def ensure_browser() -> str:
    """Start the identity browser (and its display) unless it is already running; return its CDP port."""
    port = browser_port()
    if port:
        return port
    # Serialize new launches across identities: the cap and display allocation are one operation.
    with _lock("act-launch.lock"):
        if port := browser_port():
            return port
        from . import identities
        identities.guard_launch()
        return _launch_browser()


def _launch_browser(*, prefix: tuple[str, ...] = ()) -> str:
    _stop_units()   # leftovers: a crashed browser, a display without a browser
    for d in (IDENTITY_DIR, RT_DIR, CONFIG_DIR):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)
    disable_password_saving()
    display = _start_display()
    (IDENTITY_DIR / "DevToolsActivePort").unlink(missing_ok=True)
    launch = LANES["act"]["identity_launch"]
    argv = [launch["executable"], "--remote-debugging-port=0", f"--user-data-dir={IDENTITY_DIR}", *launch["args"],
            "about:blank"]
    # ExecStop runs this module with the interpreter web runs on (it has websockets); MAINPID comes from systemd.
    exec_stop = f"ExecStop={sys.executable} -m web.act stop-browser {IDENTITY_DIR}"
    # KillMode=mixed: after ExecStop, SIGTERM to chromium alone; SIGKILL to anything left once it is gone.
    _unit(BROWSER_UNIT, [*prefix, *argv], identity_env(display), "-p", exec_stop, "-p", "KillMode=mixed",
          "-p", f"TimeoutStopSec={int(QUIT_WAIT) + 10}",
          "-p", f"BindsTo={DISPLAY_UNIT}.service", "-p", f"After={DISPLAY_UNIT}.service")
    _start_cap()
    deadline = time.monotonic() + START_WAIT
    while time.monotonic() < deadline:
        port = _port_file()
        if port and _cdp_alive(port):
            return port
        time.sleep(0.1)
    _stop_units()
    raise ActError("the identity browser did not start (no DevTools port)")


def _start_cap() -> None:
    """MAX_SEC after launch, stop the browser (ExecStop first) and its display: a requested stop, so systemd records
    the capped run as stopped, not failed (RuntimeMaxSec's 'timeout' result). The timer is bound to the browser, so
    any earlier stop (close, relaunch) removes it. Best effort: without it the RuntimeMaxSec backstop still applies."""
    # web-identity-cap[-<name>]: outside the web-identity-browser* pattern identities.running() lists
    cap = BROWSER_UNIT.replace("-browser", "-cap", 1)
    cmd = ["systemd-run", "--user", "--quiet", "--collect", f"--unit={cap}", f"--on-active={MAX_SEC}",
           f"--timer-property=BindsTo={BROWSER_UNIT}.service", f"--timer-property=After={BROWSER_UNIT}.service", "--",
           "systemctl", "--user", "--no-block", "stop", f"{BROWSER_UNIT}.service", f"{DISPLAY_UNIT}.service"]
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        if (p := subprocess.run(cmd, capture_output=True, text=True, timeout=30)).returncode == 0:
            return
        print(f"web act: no cap timer ({p.stderr.strip()[-200:]}); RuntimeMaxSec backstop applies", file=sys.stderr)


def refused(args: list[str]) -> str | None:
    """Why this pass-through is refused, or None. `web act` drives the identity browser only: an allowlisted command
    first, and nothing after it that launches, re-targets or loosens trust for a browser."""
    if not args or args[0] not in ALLOWED_COMMANDS:
        first = args[0] if args else ""
        why = ("no leading flags: web supplies --cdp/--session itself" if first.startswith("-")
               else "not an allowed agent-browser command")
        return f"`{first}`: {why} (allowed: {', '.join(sorted(ALLOWED_COMMANDS))})"
    for a in args[1:]:
        flag = a.split("=", 1)[0]
        if flag in REFUSED_FLAGS:
            return f"{flag} is not allowed: web act drives the identity browser that web launches"
    return None


def close() -> int:
    from . import identities
    with identities.operation(release=True):
        detach()
        with _lock(f"browser-{IDENTITY}.lock"):
            with _lock():
                port = browser_port()
                identities.close_locked(port)
                others = identities.records()
            return _close(stop=not others)


def _close(*, stop: bool = True) -> int:
    from . import bg, cred, handoff   # imported here: all three import this module
    if stop:
        _stop_units()   # ExecStop quits chromium cleanly before anything is signalled
    from . import identities
    with bg._locked("handoff"):   # don't let a new handoff claim race the stale-path sweep
        if not bg.live_pid("handoff") or identities.handoff_busy():
            handoff._serve_off(sweep=True)
    if busy := bg.live_pid("intake"):   # an intake doesn't use the browser: its phone form stays up
        print(f"web act: an intake is running (pid {busy}); its /web-cred path stays", file=sys.stderr)
    else:
        cred._serve_off(sweep=True)
    print("identity browser closed" if stop else "session window closed", flush=True)
    return 0


def run(args: list[str], *, output_hidden: list[int] | None = None) -> int:
    """agent-browser's exit code; fence page reads and diagnostics at the CLI boundary."""
    from . import identities
    if args == ["release"]:
        identities.release()
        print(f"identity {IDENTITY} released", flush=True)
        return 0
    if why := refused(args):
        raise ActError(why, code=2)
    if args[0] in CLOSE:
        return close()
    with identities.operation() as renew:
        return _run(args, renew, output_hidden=output_hidden)


def _run(args: list[str], renew, *, output_hidden: list[int] | None = None) -> int:
    from . import identities
    js = runs_js(args)
    if js and ALLOW_EVAL not in args:
        raise ActError(EVAL_REFUSED, code=2)
    args = [a for a in args if a != ALLOW_EVAL]
    if identities.handoff_busy():   # reattaching agent-browser now would put its CDP session on the page the user is signing in on
        raise ActError(HANDOFF_BUSY, code=2)
    port = ensure_browser()
    stdin_script = sys.stdin.read() if args[0] == "eval" and "--stdin" in args[1:] else None
    renew()
    identities.activate(port)
    if why := readback_refused(args, port, stdin_script):
        raise ActError(why, code=2)
    if js:
        log_eval(args, stdin_script)
    if args[0] == "tab":
        result = identities.tab(port, args)
    else:
        # Selection and execution share the per-caller lock. Strict pinning prevents fallback if the tab closes.
        result = agent_browser(args, port=port, capture=True, stdin_text=stdin_script)
    return emit_result(args, result, output_hidden=output_hidden)


def emit_result(args: list[str], result: subprocess.CompletedProcess, *, output_hidden=None) -> int:
    """The same Odin output boundary for identity and anonymous browser results."""
    from . import output_scan
    output_scan.serialized(result.stdout or "")
    output_scan.serialized(result.stderr or "")
    hidden = 0
    if "--json" not in args and (args[0] in READ_CMDS or result.returncode != 0):
        text = result.stdout or ""
        output = fence(text[:2000] if result.returncode else text)
        sys.stdout.write(output.text)
        hidden += output.hidden
    else:
        sys.stdout.write(result.stdout or "")
    if result.stderr:
        diagnostic = fence(result.stderr[:2000])
        sys.stderr.write(diagnostic.text)
        hidden += diagnostic.hidden
    if output_hidden is not None and (args[0] in READ_CMDS or result.stderr or result.returncode):
        output_hidden.append(hidden)
    return result.returncode


if __name__ == "__main__" and sys.argv[1:2] == ["stop-browser"]:   # the browser unit's ExecStop
    stop_browser(Path(sys.argv[2]), int(os.environ.get("MAINPID") or 0) or None)
