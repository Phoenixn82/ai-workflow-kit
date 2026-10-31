"""`web cred` and `web login`: the client of the one-way credential courier (courier/webcred.py, spec §3.5).

The courier runs as the system user `webcred` behind /run/webcred/sock and never sends a secret back: this side
only ever sees ids, origins, usernames, timestamps and result words. `web login` hands the courier the identity
browser's loopback CDP port and the active tab's target id (webcred cannot read the 0700 identity profile, where
DevToolsActivePort lives); the courier checks the page's origin itself and types. On `origin-mismatch` login exits 5
and never retries anywhere else. An unreachable courier fails closed (exit 5).

`web cred intake` asks the courier for a one-time form on 127.0.0.1, publishes it under /web-cred on the tailnet
(this user is the tailscale operator; webcred is not), pings the phone, and blocks until the form is used, expires
(10 minutes) or this process is stopped; the tailscale path is removed on every way out.

`web cred sync-google` forwards the stored credentials to Google Password Manager. The identity browser must be
signed into Google (Phase C): myaccount.google.com must stay on myaccount.google.com, else exit 4 with the Phase C
hint. It opens passwords.google.com/options and hands the courier that tab; the courier runs the CSV import itself
and never returns a secret. `needs-human` (Google wants a re-auth) exits 4 and names the `web handoff` to run.
"""
import contextlib
import json
import re
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from . import act, bg, handoff, notify

SOCKET = Path("/run/webcred/sock")
MOUNT = "/web-cred"
LISTENER = handoff.LISTENER
INTAKE_WAIT = 630.0        # the courier ends an intake after 600 s
RESULTS = ("filled", "origin-mismatch", "no-credential", "error")
GOOGLE_BUTTONS = ("Sign in with Google", "Continue with Google", "Log in with Google", "Login with Google",
                  "Sign up with Google")
# Google's own button is drawn inside an iframe; a CSS click on the iframe does nothing, so click the button's
# snapshot ref (agent-browser's snapshot reaches into the frame).
GSI_FRAME = re.compile(r'Iframe "Sign in with Google')
SNAPSHOT_REF = re.compile(r'^\s*- button\b.*\[ref=(e\d+)')
GOOGLE_PAGE = "https://accounts.google.com/"
GOOGLE_ACCOUNT = "[data-identifier]"   # a row of Google's account chooser
GOOGLE_WAIT = 30.0
POLL_SEC = 1.0
ACCOUNT_PAGE = "https://myaccount.google.com/"        # signed out, Google sends this to www.google.com/account/about
PM_PAGE = "https://passwords.google.com/options"      # the import lives here
SYNC_WAIT = 240.0          # the courier caps the run itself: <= 2 x 8 s to find the file input + 120 s of import
SYNC_RESULTS = ("imported", "needs-human", "error")
# A hint that sends the agent to `web handoff` names an --until-url that is false where the handoff starts and true
# only once the user is through (a handoff refuses a condition that is already true).
PHASE_C = ("the identity browser isn't signed into Google. Phase C, once, with the user: "
           "`web act open https://accounts.google.com/` then "
           "`web handoff \"Google sign-in\" --until-url \"https://myaccount.google.com/*\" --background`, "
           "send the user the link, `web handoff --wait`; then run `web cred sync-google` again")
PM_HOST = "passwords.google.com"


def _reauth_hint(url: str) -> str:
    """The handoff for Google's re-auth, from where the import tab is now. On Google's sign-in page (the usual
    re-auth): cleared once Google sends the tab back to the Password Manager. On the Password Manager itself (a
    prompt on the import page): every --until-url there is already true, and no selector is known to appear only
    after a re-auth, so no --until-*: the default rule waits out the prompt's own gate signals."""
    try:
        on_pm = urllib.parse.urlsplit(url).hostname == PM_HOST
    except ValueError:
        on_pm = False
    until = "" if on_pm else f' --until-url "https://{PM_HOST}/*"'
    return ("Google wants the user to confirm it's them; run: "
            f"`web handoff \"Google re-auth for the password import\"{until} --background`, send the user the link, "
            "`web handoff --wait`; then `web cred sync-google` again")


def _tab_url(port: str, target: str) -> str:
    try:
        return next((str(t.get("url", "")) for t in handoff._page_targets(port) if t["id"] == target), "")
    except (OSError, ValueError, KeyError):
        return ""


def _google_hint(origin: str) -> str:
    """The handoff for a Google sign-in on `origin`: cleared once Google hands back to the site."""
    identity_flag = "" if act.IDENTITY == "personal" else f" --as {act.IDENTITY}"
    return (f'`web handoff{identity_flag} "Google sign-in" --until-url "{origin.rstrip("/")}/*" --background`, send the user the '
            "link, then `web handoff --wait`")


class CredError(Exception):
    def __init__(self, message: str, code: int = 5):
        super().__init__(message)
        self.code = code


def _connect(timeout: float) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(SOCKET))
    except OSError as e:
        s.close()
        raise CredError(f"the credential courier is unreachable at {SOCKET} ({e.strerror or type(e).__name__}); "
                        f"is webcred.socket installed (courier/install.sh)?") from None
    return s


def _line(f) -> dict:
    raw = f.readline()
    if not raw:
        raise CredError("the courier closed the connection without a reply")
    try:
        obj = json.loads(raw)
    except ValueError:
        raise CredError("the courier sent an unreadable reply") from None
    if not isinstance(obj, dict):
        raise CredError("the courier sent an unreadable reply")
    return obj


def call(req: dict, timeout: float = 60.0) -> dict:
    """One request line to the courier, one reply line back."""
    try:
        with _connect(timeout) as s, s.makefile("rb") as f:
            s.sendall(json.dumps(req).encode() + b"\n")
            return _line(f)
    except (TimeoutError, ConnectionError) as e:
        raise CredError(f"the courier did not answer ({type(e).__name__})") from None


# --- web login ---

def login(origin: str, user_sel: str | None = None, pass_sel: str | None = None, google: bool = False,
          cred_id: str | None = None, username: str | None = None, submit: bool = True) -> tuple[str, int]:
    from . import identities
    with identities.operation():
        return _login(origin, user_sel, pass_sel, google, cred_id, username, submit)


def _login(origin, user_sel, pass_sel, google, cred_id, username, submit) -> tuple[str, int]:
    """Returns (result word, exit code)."""
    if google and (user_sel or pass_sel or cred_id or username):
        raise CredError("--google takes no selectors, --id or --username", 2)
    if not google and not (user_sel or pass_sel):
        if cred_id or username:
            raise CredError("--id/--username pick a stored password: give --user-sel and/or --pass-sel too", 2)
        google = True   # the user's default: Sign in with Google; a stored password only when selectors are given
    from . import identities
    act.acquire()
    if identities.handoff_busy():   # the page is the user's until the handoff ends
        raise CredError(act.HANDOFF_BUSY, 2)
    port = act.browser_port()
    if port is None:
        raise CredError("the identity browser isn't running; open the login page with `web act open <url>`", 2)
    identities.activate(port)
    if google:
        return _google(port, origin)
    try:
        target = handoff._active_target(port)
    except handoff.HandoffError as e:
        raise CredError(str(e)) from None
    req = {"op": "fill", **({"id": cred_id} if cred_id else {"origin": origin}),
           "cdp": f"http://127.0.0.1:{port}", "target": target}
    if username:
        req["username"] = username
    if user_sel:
        req["user_sel"] = user_sel
    if pass_sel:
        req["pass_sel"] = pass_sel
        act.remember_pass_selector(pass_sel)   # `web act get value` / `eval` won't read this field back
    req["submit"] = submit
    reply = call(req)
    word = reply.get("result") if reply.get("result") in RESULTS else "error"
    if word == "error" and isinstance(reply.get("why"), str):
        print(f"web login: courier: {reply['why'][:80]}", file=sys.stderr)
    return word, 0 if word == "filled" else 5


def _google_pages(port: str) -> list[dict]:
    try:
        return [t for t in handoff._page_targets(port) if str(t.get("url", "")).startswith(GOOGLE_PAGE)]
    except (OSError, ValueError):
        return []


def _poll(check, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        if (v := check()) is not None:
            return v
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_SEC)


def _gsi_ref(port: str) -> str | None:
    """The snapshot ref of the button inside Google's Sign in with Google iframe, if the page has one."""
    try:
        out = act.agent_browser(["snapshot", "-i"], port=port, capture=True, timeout=30).stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if GSI_FRAME.search(line):
            for child in lines[i + 1:i + 4]:
                if m := SNAPSHOT_REF.match(child):
                    return m.group(1)
    return None


def _google(port: str, origin: str) -> tuple[str, int]:
    """Sign in with Google using the identity profile's Google session (Phase C signs it in once): click the
    site's button, pick the account in Google's chooser, wait for Google to hand back. Anything else Google asks
    (a password, 2FA, consent) is a human step: `handoff-needed`, exit 4."""
    # Google's own iframe button first: a site's text match can be a hidden fallback copy that does nothing.
    ref = _gsi_ref(port)
    if not (ref and act.run(["click", f"@{ref}"]) == 0) \
            and not any(act.run(["find", "text", t, "click"]) == 0 for t in GOOGLE_BUTTONS):
        print("web login: no Sign in with Google button on this page", file=sys.stderr)
        return "error", 5
    chooser = _poll(lambda: (_google_pages(port) or [None])[0], GOOGLE_WAIT)
    if chooser is None:
        print("web login: no Google sign-in page appeared", file=sys.stderr)
        return "error", 5
    if act.run(["tab", chooser["id"]]) != 0 or act.run(["wait", GOOGLE_ACCOUNT]) != 0:
        print(f"web login: Google wants more than an account pick; run: {_google_hint(origin)}", file=sys.stderr)
        return "handoff-needed", 4
    if act.run(["click", GOOGLE_ACCOUNT]) != 0:
        return "error", 5
    if _poll(lambda: True if not _google_pages(port) else None, GOOGLE_WAIT):
        return "signed-in", 0
    print(f"web login: Google is still asking; run: {_google_hint(origin)}", file=sys.stderr)
    return "handoff-needed", 4


# --- web cred sync-google ---

def _landed(port: str, url: str) -> str:
    """Open url in the identity browser's current tab; the URL it ended on ('' if unknown)."""
    handoff._ab_data(port, "open", url)
    return str((handoff._ab_data(port, "get", "url") or {}).get("url") or "")


def _signed_in(url: str) -> bool:
    """myaccount.google.com kept us (signed out, Google redirects to its marketing page or to sign-in)."""
    try:
        p = urllib.parse.urlsplit(url)
        return p.scheme == "https" and p.hostname == "myaccount.google.com" and p.port is None
    except ValueError:
        return False


def sync_google() -> tuple[str, int]:
    from . import identities
    with identities.operation():
        return _sync_google()


def _sync_google() -> tuple[str, int]:
    """Returns (result word, exit code): imported → 0; not-signed-in / needs-human → 4; error → 5."""
    if act.IDENTITY != "personal":
        raise CredError("sync-google only supports --as personal", 2)
    from . import identities
    act.acquire()
    if identities.handoff_busy():   # the page is the user's until the handoff ends
        raise CredError(act.HANDOFF_BUSY, 2)
    port = act.ensure_browser()
    identities.activate(port)
    if not _signed_in(_landed(port, ACCOUNT_PAGE)):
        print(f"web cred sync-google: {PHASE_C}", file=sys.stderr)
        return "not-signed-in", 4
    _landed(port, PM_PAGE)
    try:
        target = handoff._active_target(port)
    except handoff.HandoffError as e:
        raise CredError(str(e)) from None
    reply = call({"op": "sync-google", "cdp": f"http://127.0.0.1:{port}", "target": target}, timeout=SYNC_WAIT)
    word = reply.get("result") if reply.get("result") in SYNC_RESULTS else "error"
    if word == "imported":
        print(f"web cred sync-google: {reply.get('count')} credential(s) handed to Google Password Manager",
              file=sys.stderr)
        return word, 0
    if word == "needs-human":
        hint = PHASE_C if reply.get("why") == "signed-out" else _reauth_hint(_tab_url(port, target))
        print(f"web cred sync-google: {hint}", file=sys.stderr)
        return word, 4
    if isinstance(reply.get("why"), str):
        print(f"web cred sync-google: courier: {reply['why'][:80]}", file=sys.stderr)
    return "error", 5


# --- web cred ---

def _tailscale(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tailscale", *args], capture_output=True, text=True, timeout=30)


def _serve_off(sweep: bool = False) -> None:
    try:
        failed = _tailscale("serve", *LISTENER, "--set-path", MOUNT, "off").returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        failed = True
    if failed and not sweep:
        print(f"web cred: could not remove the tailscale path {MOUNT}; run: "
              f"tailscale serve {' '.join(LISTENER)} --set-path {MOUNT} off", file=sys.stderr)


def intake() -> tuple[str, int]:
    """Publish the one-time phone form, ping the phone, wait. Returns (result, exit code): saved → 0, else 4."""
    with contextlib.ExitStack() as stack:
        try:   # one intake at a time: a live one's /web-cred path must not be swept
            stack.enter_context(bg.claim("intake"))
        except bg.Busy as e:
            raise CredError(f"an intake is already running (pid {e.pid}); `web cred intake --status` shows its "
                            "link", 2) from None
        try:
            host = handoff._tailnet_name()
        except handoff.HandoffError as e:
            raise CredError(str(e)) from None
        s = stack.enter_context(_connect(INTAKE_WAIT))
        f = stack.enter_context(s.makefile("rb"))
        _serve_off(sweep=True)   # a hard-killed intake can leave its path behind
        s.sendall(json.dumps({"op": "intake-url", "host": host}).encode() + b"\n")
        first = _line(f)
        if not isinstance(first.get("url"), str) or not isinstance(first.get("port"), int):
            raise CredError(f"the courier refused the intake ({first.get('why', 'error')})")
        try:
            p = _tailscale("serve", "--bg", *LISTENER, "--set-path", MOUNT, f"http://127.0.0.1:{first['port']}")
            if p.returncode != 0:
                raise CredError(f"tailscale serve failed: {p.stderr.strip()[-300:]}")
            link = first["url"]
            bg.announce(f"intake: {link}  (one use; expires in 10 minutes)")
            notify.ping(f"web cred: add a login\n{link}")
            try:
                result = _line(f).get("result")
            except TimeoutError:
                result = "expired"
        finally:
            _serve_off()
    result = result if isinstance(result, str) else "error"
    return result, 0 if result == "saved" else 4
