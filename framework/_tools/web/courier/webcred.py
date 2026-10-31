"""webcred: the one-way credential courier (spec §3.5).

Runs as the system user `webcred`, started by webcred.socket on the first connection to /run/webcred/sock (group
you, 0660), and exits after IDLE_SEC without work. It owns /var/lib/webcred (0700): store/<id>.age (one
age-encrypted JSON record per credential: origin, username, secret, created), the age identity (0600), the public
recipient and audit.log. Plaintext only ever moves through `age` on stdin/stdout, never argv or env.

Socket protocol: one JSON request line, one JSON reply line. No reply ever carries a secret.
  {"op":"list"}                                   -> {"items":[{"id","origin","username","created"}]}
  {"op":"gen","origin","username","length":24}   -> {"result":"ok","id"} | {"result":"exists"} | error
  {"op":"fill","id"|"origin",["username"],"cdp":"http://127.0.0.1:<port>","target":"<page targetId>",
   "user_sel","pass_sel",["submit":true]}         -> {"result":"filled"|"origin-mismatch"|"no-credential"|"error"}
  {"op":"rm","id"}                                -> {"result":"removed"|"no-credential"}
  {"op":"audit","n":20}                           -> {"items":[audit lines]}
  {"op":"intake-url","host":"<tailnet name>"}     -> {"url","port","expires"}, then, on the same connection,
                                                     {"result":"saved"|"expired"|"cancelled"|"replaced"|...}
  {"op":"sync-google","cdp":"http://127.0.0.1:<port>","target":"<page targetId>"}
                                                  -> {"result":"imported"|"needs-human"|"error",["why"],"count"}
Errors are {"result":"error","why":<fixed word>}.

Fill: the courier opens the page target's CDP socket itself (ws://127.0.0.1:<port>/devtools/page/<target>, built
here, never taken from the caller). Browser side: Target.getTargetInfo must say type "page" and a URL whose origin
is exactly (scheme, host, port) the STORED origin. Renderer side, in a fresh isolated world (page script can't
reach or tamper with its built-ins): location.origin must match again; then, per field, ONE function call re-checks
the origin, checks the element (an <input>; type password for the secret), focuses it and inserts the text with
document.execCommand("insertText") in the same JS task, so focus can't move between the check and the insert. The
text travels as a call argument, never in JS source. A post-check confirms origin and focus before Enter submits.
No Runtime.enable: nothing for a page to detect.

Sync-google forwards every stored credential to Google Password Manager by CSV import (Google has no API for it),
on the identity browser's tab that the client opened at passwords.google.com/options. The same checks first:
Target.getTargetInfo says a page at exactly https://passwords.google.com (its /intro landing means signed out, and
accounts.google.com means Google wants a sign-in: both are `needs-human`, for `web handoff`), then the isolated
world agrees. File-chooser interception is on (no native dialog can open). The courier clicks Import and finds the
page's <input type=file>. Only then does the CSV exist: IMPORT_DIR/passwords.csv, 0640 in the browser user's group
(the dir is setgid), the one plaintext copy webcred ever writes. DOM.setFileInputFiles hands it over; the
dialog's Import is clicked; `imported` needs that click AND a positive "N passwords imported" that appeared after
it (failure phrasings, checked first, end the run as an error; so do a re-auth prompt and the cap); then
`shred -u`. A timer shreds it IMPORT_SEC after it was written whatever the page does; a killed
daemon's leftover is shredded at the next start and by the unit's ExecStopPost. No secret travels over CDP.

Every operation appends {time, op, origin, caller_pid, caller_uid, result} (fill also cdp_port, target) to
audit.log, from the peer's SO_PEERCRED. Peers other than root and ALLOWED_USER are refused. Nothing here logs a
request body, a form field or a secret; errors log only an exception type name. The daemon is not dumpable
(PR_SET_DUMPABLE 0; the unit sets LimitCORE=0), so a crash can't write decrypted memory to a core file.
"""
import contextlib
import csv
import hmac
import html
import io
import ipaddress
import json
import os
import pwd
import re
import select
import secrets
import socket
import stat
import string
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import namedtuple
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from websockets.sync.client import connect as ws_connect

ROOT = Path("/var/lib/webcred")
AGE = "/usr/bin/age"
AGE_ENV = {"PATH": "/usr/bin", "LANG": "C.UTF-8"}
IDLE_SEC = 60.0
INTAKE_SEC = 600.0        # an intake link lives ten minutes
INTAKE_MOUNT = "/web-cred"
INTAKE_MAX_BODY = 8192
INTAKE_MAX_BAD = 5        # rejected posts before the link dies
MAX_LINE = 64 * 1024
CONN_TIMEOUT = 15.0       # one request line must arrive within this
MAX_CONNS = 8             # concurrent connections (an open intake holds one)
INTAKE_REQ_TIMEOUT = 15.0 # one HTTP request to the form must complete within this
SUPERSEDE_SEC = 30 * 86400   # a replaced record stays (still encrypted) in superseded/ this long
ALLOWED_USER = "you"   # with root, the only peers served (the socket is group you 0660 too)
CDP_TIMEOUT = 10.0
GEN_MIN, GEN_MAX, GEN_DEFAULT = 12, 128, 24
SYMBOLS = "-_.!@#%+="
ALPHABET = string.ascii_letters + string.digits + SYMBOLS
MAX_SEL = 300
MAX_USER = 256
MAX_SECRET = 1024
ID_RE = re.compile(r"[0-9a-f]{12}")
CDP_RE = re.compile(r"http://127\.0\.0\.1:([0-9]{1,5})")
TARGET_RE = re.compile(r"[A-Za-z0-9]{1,64}")
HOST_RE = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")
PAGE_WORDS = {"ok", "origin-mismatch", "no-field", "not-an-input", "wrong-field-type", "field-disabled", "no-focus",
              "bad-selector", "insert-failed", "moved"}
Caller = namedtuple("Caller", "pid uid")
NOBODY = Caller(None, None)

# Google Password Manager sync (CSV import)
PM_ORIGIN = "https://passwords.google.com"
SIGNIN_ORIGIN = "https://accounts.google.com"
IMPORT_DIR = Path("/run/webcred/import")   # 2750 webcred:you, setgid: made by webcred.socket's ExecStartPost
IMPORT_NAME = "passwords.csv"
IMPORT_SEC = 120.0        # the plaintext CSV lives at most this long
IMPORT_STAGE_SEC = 8.0    # after a click, how long the page's file input may take to appear
IMPORT_POLL = 0.5
SHRED = "/usr/bin/shred"
CSV_HEADER = ("name", "url", "username", "password")   # Chrome's password CSV
# Page vocabulary (lowercase): exact button labels; phrases matched in the page text at a word start (so
# "10 passwords imported" holds no "0 passwords imported"); done = a positive, non-zero count only (JS regex
# sources). Failure phrases are checked before done ones. Unverified against the live page until Phase C signs the
# identity profile in: a miss fails closed (no-file-input / no-confirmation).
OPEN_LABELS = (["import passwords", "import"], ["select file", "choose file", "browse"])
CONFIRM_LABELS = ["import"]
DONE_PATTERNS = [r"(?<![a-z0-9])[1-9][0-9]* passwords? (?:were |was )?imported",
                 r"(?<![a-z0-9])imported [1-9][0-9]* passwords?"]
FAILED_WORDS = ["couldn't import", "could not import", "can't import", "cannot import", "import failed",
                "something went wrong", "not imported", "couldn't be imported", "could not be imported",
                "weren't imported", "were not imported", "wasn't imported", "0 passwords imported",
                "0 password imported"]
REAUTH_WORDS = ["verify it's you", "confirm it's you", "re-enter your password"]


# --- origins ---

def normalize_origin(url) -> str | None:
    """scheme://host[:port] (default port omitted, host lowercased), or None if not a plain http(s) origin.
    Path, query and fragment are ignored; userinfo is refused; a non-ASCII host is refused (IDN hosts must be
    given as punycode A-labels, xn--..., the form a browser's location.origin reports)."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        return None
    try:
        p = urllib.parse.urlsplit(url.strip())
        port = p.port
    except ValueError:
        return None
    scheme = p.scheme.lower()
    if scheme not in ("http", "https") or p.username is not None or p.password is not None or not p.hostname:
        return None
    host = p.hostname
    if not host.isascii():
        return None
    if ":" in host:   # IPv6 literal
        try:
            host = f"[{ipaddress.IPv6Address(host).compressed}]"
        except ValueError:
            return None
    elif not HOST_RE.fullmatch(host):
        return None
    default = 443 if scheme == "https" else 80
    if port is not None and not 0 < port < 65536:
        return None
    return f"{scheme}://{host}" + ("" if port in (None, default) else f":{port}")


def origins_match(stored, actual) -> bool:
    """Exact scheme + host + port. A lookalike (accounts.google.com.evil.io), another scheme, a subdomain or another
    port is a mismatch."""
    a, s = normalize_origin(actual), normalize_origin(stored)
    return a is not None and a == s


# --- the store ---

class StoreError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, root: Path = ROOT):
        self.root = Path(root)
        self.dir = self.root / "store"
        self.superseded = self.root / "superseded"
        self.identity = self.root / "identity"
        self.recipient_file = self.root / "recipient"
        self.audit_file = self.root / "audit.log"
        self.lock = threading.RLock()

    def _age(self, args: list[str], data: bytes) -> bytes:
        # The plaintext travels on stdin (encrypt) or stdout (decrypt) only; argv carries a recipient or a path.
        p = subprocess.run([AGE, *args], input=data, capture_output=True, timeout=30, env=AGE_ENV)
        if p.returncode != 0:
            raise StoreError("age failed")
        return p.stdout

    def _recipient(self) -> str:
        r = self.recipient_file.read_text().strip()
        if not r.startswith("age1"):
            raise StoreError("bad recipient")
        return r

    def _path(self, cid: str) -> Path:
        if not isinstance(cid, str) or not ID_RE.fullmatch(cid):
            raise StoreError("bad id")
        return self.dir / f"{cid}.age"

    def ids(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.age") if ID_RE.fullmatch(p.stem))

    def load(self, cid: str) -> dict:
        rec = json.loads(self._age(["-d", "-i", str(self.identity)], self._path(cid).read_bytes()))
        if not isinstance(rec, dict) or not all(isinstance(rec.get(k), str) for k in ("origin", "username", "secret")):
            raise StoreError("bad record")
        return rec

    def exists(self, cid: str) -> bool:
        try:
            return self._path(cid).exists()
        except StoreError:
            return False

    def records(self) -> list[tuple[str, dict]]:
        out = []
        for cid in self.ids():
            try:
                out.append((cid, self.load(cid)))
            except (OSError, ValueError, StoreError):
                continue   # an unreadable record is skipped, never fatal
        return out

    def items(self) -> list[dict]:
        return [{"id": cid, "origin": r["origin"], "username": r["username"], "created": r.get("created", "")}
                for cid, r in self.records()]

    def find(self, origin: str, username: str | None = None) -> list[tuple[str, dict]]:
        return [(cid, r) for cid, r in self.records()
                if r["origin"] == origin and (username is None or r["username"] == username)]

    def save(self, origin: str, username: str, secret: str) -> str:
        """Encrypt to the public recipient and write store/<id>.age atomically (0600)."""
        with self.lock:
            record = {"origin": origin, "username": username, "secret": secret, "created": _now()}
            blob = self._age(["-r", self._recipient()], json.dumps(record).encode())
            cid = secrets.token_hex(6)
            tmp = self.dir / f".{cid}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(blob)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self._path(cid))
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            return cid

    def supersede(self, cid: str) -> None:
        """Move a replaced record, ciphertext unchanged, to superseded/ (kept SUPERSEDE_SEC, then pruned)."""
        with self.lock:
            src = self._path(cid)
            self.superseded.mkdir(mode=0o700, exist_ok=True)
            dst = self.superseded / src.name
            os.replace(src, dst)
            os.utime(dst)   # retention counts from now

    def prune_superseded(self, max_age: float | None = None) -> list[str]:
        cutoff = time.time() - (SUPERSEDE_SEC if max_age is None else max_age)
        gone = []
        with self.lock:
            for p in self.superseded.glob("*.age"):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                        gone.append(p.stem)
                except OSError:
                    continue
        return gone

    def remove(self, cid: str) -> bool:
        with self.lock:
            p = self._path(cid)
            if not p.exists():
                return False
            p.unlink()
            return True

    def audit(self, op: str, origin: str | None, caller: Caller, result: str, **extra) -> None:
        line = json.dumps({"time": _now(), "op": op, "origin": origin, "caller_pid": caller.pid,
                           "caller_uid": caller.uid, "result": result, **extra})
        fd = os.open(self.audit_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (line + "\n").encode())
        finally:
            os.close(fd)

    def audit_tail(self, n: int) -> list[dict]:
        try:
            lines = self.audit_file.read_text().splitlines()[-n:]
        except FileNotFoundError:
            return []
        out = []
        for x in lines:
            try:
                out.append(json.loads(x))
            except ValueError:
                continue
        return out


def gen_password(length: int) -> str:
    """Random over ALPHABET with at least one lowercase, uppercase, digit and symbol."""
    while True:
        pw = "".join(secrets.choice(ALPHABET) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)
                and any(c in SYMBOLS for c in pw)):
            return pw


# --- CDP (a minimal JSON-RPC client over one page target's websocket) ---

class CdpError(Exception):
    pass


class Cdp:
    def __init__(self, ws):
        self.ws, self.n = ws, 0
        self.choosers: list[dict] = []   # Page.fileChooserOpened params (sent once interception is on)

    def call(self, method: str, params: dict) -> dict:
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        deadline = time.monotonic() + CDP_TIMEOUT
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise CdpError("timeout")
            msg = json.loads(self.ws.recv(timeout=left))
            if msg.get("method") == "Page.fileChooserOpened":
                self.choosers.append(msg.get("params") or {})
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise CdpError("cdp error")
                return msg.get("result") or {}

    def _call_function(self, context_id: int, declaration: str, args, by_value: bool, gesture: bool) -> dict:
        return self.call("Runtime.callFunctionOn", {
            "functionDeclaration": declaration, "executionContextId": context_id,
            "arguments": [{"value": a} for a in args], "returnByValue": by_value,
            **({"userGesture": True} if gesture else {})})

    def function(self, context_id: int, declaration: str, *args, gesture: bool = False):
        """Runtime.callFunctionOn in one execution context; the arguments travel as values, never as source.
        A thrown exception gives None (its details are never read: they could quote an argument). gesture: run as
        a user action (a click that opens a dialog or a file chooser needs one)."""
        r = self._call_function(context_id, declaration, args, True, gesture)
        if "exceptionDetails" in r:
            return None
        return (r.get("result") or {}).get("value")

    def node(self, context_id: int, declaration: str, *args) -> str | None:
        """Like function(), for a function that returns a DOM node: its objectId, or None."""
        r = self._call_function(context_id, declaration, args, False, False)
        res = r.get("result") or {}
        if "exceptionDetails" in r or res.get("subtype") != "node" or not isinstance(res.get("objectId"), str):
            return None
        return res["objectId"]


ORIGIN_FN = "function () { return location.origin; }"
# One JS task: origin re-check, element check, focus, insert. Runs in webcred's isolated world. Returns a PAGE_WORD.
FILL_FN = """function (origin, sel, kind, text) {
  if (location.origin !== origin) return "origin-mismatch";
  let el;
  try { el = document.querySelector(sel); } catch (e) { return "bad-selector"; }
  if (!el) return "no-field";
  if (!(el instanceof HTMLInputElement)) return "not-an-input";
  const t = el.type;
  if (kind === "password" ? t !== "password" : !["text", "email", "tel"].includes(t)) return "wrong-field-type";
  if (el.disabled || el.readOnly) return "field-disabled";
  el.focus();
  if (document.activeElement !== el) return "no-focus";
  try { el.select(); } catch (e) { el.value = ""; }
  if (!document.execCommand("insertText", false, text)) return "insert-failed";
  return location.origin === origin && document.activeElement === el ? "ok" : "moved";
}"""
STILL_FN = """function (origin, sel) {
  try {
    return location.origin === origin && document.activeElement === document.querySelector(sel) ? "ok" : "moved";
  } catch (e) { return "moved"; }
}"""
ENTER = {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}


def fill_page(port: int, target: str, stored_origin: str, steps: list[tuple[str, str, str]],
              submit: bool) -> tuple[str, str | None]:
    """Type each (selector, text, kind) into the page target if it is a page at the stored origin.
    Returns (result, why)."""
    url = f"ws://127.0.0.1:{port}/devtools/page/{target}"
    with ws_connect(url, open_timeout=5, close_timeout=2, max_size=2 ** 22, proxy=None) as ws:
        cdp = Cdp(ws)
        info = cdp.call("Target.getTargetInfo", {"targetId": target}).get("targetInfo") or {}
        if info.get("type") != "page" or info.get("targetId") != target:
            return "error", "not-a-page"
        if not origins_match(stored_origin, info.get("url")):
            return "origin-mismatch", None
        frame = ((cdp.call("Page.getFrameTree", {}).get("frameTree") or {}).get("frame") or {}).get("id")
        if not isinstance(frame, str):
            return "error", "no-frame"
        ctx = cdp.call("Page.createIsolatedWorld", {"frameId": frame, "worldName": "webcred",
                                                    "grantUniveralAccess": False}).get("executionContextId")
        if not isinstance(ctx, int):
            return "error", "no-world"
        actual = cdp.function(ctx, ORIGIN_FN)
        if not isinstance(actual, str) or not origins_match(stored_origin, actual):
            return "origin-mismatch", None
        for sel, text, kind in steps:
            word = cdp.function(ctx, FILL_FN, actual, sel, kind, text)
            if word == "origin-mismatch":
                return "origin-mismatch", None
            if word != "ok":
                return "error", word if word in PAGE_WORDS else "unexpected-page-reply"
            if cdp.function(ctx, STILL_FN, actual, sel) != "ok":
                return "error", "moved"
        if submit:
            cdp.call("Input.dispatchKeyEvent", {"type": "keyDown", "text": "\r", "unmodifiedText": "\r", **ENTER})
            cdp.call("Input.dispatchKeyEvent", {"type": "keyUp", **ENTER})
        return "filled", None


# --- sync-google: the CSV import into Google Password Manager ---

def csv_rows(records) -> list[tuple[str, str, str, str]]:
    """Chrome's password CSV rows (name, url, username, password) for (id, record) pairs; name is the host."""
    return [(urllib.parse.urlsplit(r["origin"]).hostname or r["origin"], r["origin"] + "/", r["username"],
             r["secret"]) for _, r in records]


def shred(path: Path) -> None:
    """`shred -u` a regular file; a link (or anything else) is only unlinked; a missing file is fine. If shred is
    missing or fails, the file is overwritten with zeros and unlinked here."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(st.st_mode):
        os.unlink(path)
        return
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run([SHRED, "-u", "--", str(path)], capture_output=True, timeout=30, env=AGE_ENV)
    if not os.path.lexists(path):
        return
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.write(fd, b"\0" * st.st_size)
            os.fsync(fd)
        finally:
            os.close(fd)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


def write_import_csv(path: Path, rows, gid: int) -> None:
    """Chrome's password CSV at path: 0640 in group gid (inherited from the setgid import dir and checked before a
    byte is written). Every field is quoted, quotes doubled (RFC 4180), so commas, quotes, newlines and edge spaces
    in a username or password survive. A leftover or a link at path is removed first; O_EXCL|O_NOFOLLOW."""
    shred(path)
    buf = io.StringIO()
    csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\n").writerows([CSV_HEADER, *rows])
    data = memoryview(buf.getvalue().encode())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(fd, 0o640)
        st = os.fstat(fd)
        if st.st_gid != gid or stat.S_IMODE(st.st_mode) != 0o640:
            raise StoreError("import file mode or group")
        while data:
            data = data[os.write(fd, data):]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        shred(path)
        raise
    os.close(fd)


def import_dir_ready(d: Path, gid: int) -> bool:
    """The import dir is a real dir of this process's user, in group gid with the setgid bit (so the CSV is
    created in the browser user's group) and writable by its owner only."""
    try:
        st = os.lstat(d)
    except OSError:
        return False
    return (stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid() and st.st_gid == gid
            and bool(st.st_mode & stat.S_ISGID) and not st.st_mode & 0o022)


def sweep_imports(d: Path = IMPORT_DIR) -> None:
    """Shred whatever an interrupted import left in the import dir (at daemon start)."""
    try:
        leftovers = list(Path(d).iterdir())
    except OSError:
        return
    for p in leftovers:
        with contextlib.suppress(OSError):
            shred(p)


class ImportFile:
    """The one plaintext copy of the store webcred ever writes: the CSV the identity browser hands to Google.
    write() starts a timer that shreds it IMPORT_SEC later whatever the import is doing; close() shreds it now
    (idempotent). A daemon killed in between: the unit's ExecStopPost and the next start (sweep_imports) shred
    the leftover; /run is tmpfs, so a reboot drops it too. count: the rows that were written (audited)."""

    def __init__(self, path: Path, gid: int):
        self.path, self.gid = Path(path), gid
        self.count = 0
        self.deadline = 0.0
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def write(self, rows: list) -> None:
        self.deadline = time.monotonic() + IMPORT_SEC
        self._timer = threading.Timer(IMPORT_SEC, self.close)
        self._timer.daemon = True
        self._timer.start()
        write_import_csv(self.path, rows, self.gid)
        self.count = len(rows)

    def close(self) -> None:
        with self._lock:
            shred(self.path)   # if this raises, the timer stays armed: the cap still gets its own try
            if self._timer is not None:
                self._timer.cancel()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# The import page's functions, in webcred's isolated world; each re-checks location.origin first.
PM_FIND_FN = r"""function (origin) {
  if (location.origin !== origin) return null;
  const find = (root) => {
    for (const el of root.querySelectorAll("input")) if (el.type === "file") return el;
    for (const host of root.querySelectorAll("*")) if (host.shadowRoot) { const f = find(host.shadowRoot); if (f) return f; }
    return null;
  };
  return find(document);
}"""
PM_CLICK_FN = r"""function (origin, labels, inDialog) {
  if (location.origin !== origin) return "origin-mismatch";
  const all = (root, sel, out) => {
    if (root.shadowRoot) all(root.shadowRoot, sel, out);
    out.push(...root.querySelectorAll(sel));
    for (const host of root.querySelectorAll("*")) if (host.shadowRoot) all(host.shadowRoot, sel, out);
    return out;
  };
  const scopes = inDialog
    ? all(document, 'dialog[open], [role="dialog"], [role="alertdialog"], [aria-modal="true"]', []) : [document];
  for (const scope of scopes) {
    for (const el of all(scope, 'button, [role="button"], a[href], input[type="button"], input[type="submit"]', [])) {
      const text = ((el.innerText || el.value || "").trim() || el.getAttribute("aria-label") || "")
        .replace(/\s+/g, " ").trim().toLowerCase();
      if (!labels.includes(text) || el.disabled || el.getAttribute("aria-disabled") === "true") continue;
      if (!el.getClientRects().length) continue;
      el.click();
      return "clicked";
    }
  }
  return "none";
}"""
# [re-auth prompt 0|1, failure phrases, done phrases]; null once the page left the origin. A sizeable iframe from
# Google's sign-in counts as a re-auth prompt (Google's hidden cookie iframes are not sizeable). Phrases match at a
# word start; done takes regex sources (DONE_PATTERNS).
PM_STATE_FN = r"""function (origin, signin, failed, done, reauth) {
  if (location.origin !== origin) return null;
  for (const f of document.querySelectorAll("iframe")) {
    let o = "";
    try { o = new URL(f.src, location.href).origin; } catch (e) {}
    const r = f.getBoundingClientRect();
    if (o === signin && r.width >= 100 && r.height >= 100) return [1, 0, 0];
  }
  const t = (document.body ? document.body.innerText : "").replace(/[‘’]/g, "'")
    .replace(/\s+/g, " ").toLowerCase();
  const count = (sources) => sources.reduce((n, s) => n + (t.match(new RegExp(s, "g")) || []).length, 0);
  const words = (ws) => ws.map((w) => "(?<![a-z0-9])" + w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  return [count(words(reauth)) ? 1 : 0, count(words(failed)), count(done)];
}"""
PM_WHERE = {"signed-out": ("needs-human", "signed-out"), "reauth": ("needs-human", "reauth"),
            "elsewhere": ("error", "origin-mismatch"), "not-a-page": ("error", "not-a-page")}


class _Stop(Exception):
    """Ends an import run with (result, why)."""

    def __init__(self, result: str, why: str):
        super().__init__(why)
        self.result, self.why = result, why


def _pm_where(cdp: Cdp, target: str) -> str:
    """Where the target is, browser-side: "page" (the Password Manager, signed in), "signed-out" (its /intro
    landing), "reauth" (Google's sign-in), "elsewhere" or "not-a-page"."""
    info = cdp.call("Target.getTargetInfo", {"targetId": target}).get("targetInfo") or {}
    if info.get("type") != "page" or info.get("targetId") != target:
        return "not-a-page"
    url = info.get("url")
    origin = normalize_origin(url)
    if origin == SIGNIN_ORIGIN:
        return "reauth"
    if origin != PM_ORIGIN:
        return "elsewhere"
    return "signed-out" if urllib.parse.urlsplit(url).path.startswith("/intro") else "page"


def import_to_google(port: int, target: str, rows: list, csv_file: ImportFile) -> tuple[str, str | None]:
    """Drive the identity browser's Password Manager tab through a CSV import. Returns (result, why). The CSV is
    written only once the page's file input is in hand; the caller shreds it (csv_file.close)."""
    url = f"ws://127.0.0.1:{port}/devtools/page/{target}"
    with ws_connect(url, open_timeout=5, close_timeout=2, max_size=2 ** 22, proxy=None) as ws:
        try:
            _import(Cdp(ws), target, rows, csv_file)
        except _Stop as s:
            return s.result, s.why
    return "imported", None


def _import(cdp: Cdp, target: str, rows: list, csv_file: ImportFile) -> None:
    def where() -> None:
        w = _pm_where(cdp, target)
        if w != "page":
            raise _Stop(*PM_WHERE[w])

    where()
    frame = ((cdp.call("Page.getFrameTree", {}).get("frameTree") or {}).get("frame") or {}).get("id")
    if not isinstance(frame, str):
        raise _Stop("error", "no-frame")
    ctx = cdp.call("Page.createIsolatedWorld", {"frameId": frame, "worldName": "webcred",
                                                "grantUniveralAccess": False}).get("executionContextId")
    if not isinstance(ctx, int):
        raise _Stop("error", "no-world")
    if cdp.function(ctx, ORIGIN_FN) != PM_ORIGIN:
        raise _Stop("error", "origin-mismatch")
    cdp.call("Page.enable", {})   # interception needs it (page script can't see it, unlike Runtime.enable)
    cdp.call("Page.setInterceptFileChooserDialog", {"enabled": True})   # no native file dialog can ever open

    def state() -> tuple[int, int]:
        """(failure phrases, done phrases) on the page; a re-auth prompt or a page that went away ends the run."""
        try:
            s = cdp.function(ctx, PM_STATE_FN, PM_ORIGIN, SIGNIN_ORIGIN, FAILED_WORDS, DONE_PATTERNS, REAUTH_WORDS)
        except CdpError:   # the isolated world went away with a navigation
            s = None
        where()
        if not (isinstance(s, list) and len(s) == 3 and all(type(x) is int for x in s)):
            raise _Stop("error", "page-changed")
        if s[0]:
            raise _Stop("needs-human", "reauth")
        return s[1], s[2]

    def file_input() -> dict | None:
        el = cdp.node(ctx, PM_FIND_FN, PM_ORIGIN)
        if el:
            return {"objectId": el}
        for ev in cdp.choosers:   # a chooser the page opened for an input that isn't in the document
            if ev.get("frameId") == frame and type(ev.get("backendNodeId")) is int:
                return {"backendNodeId": ev["backendNodeId"]}
        return None

    def stage(labels: list[str]) -> dict | None:
        """Click the first visible button with one of these labels (retried while the page is still rendering),
        then wait for a file input or a file chooser; IMPORT_STAGE_SEC in all. The click comes first: an input
        already in the page (inside a dialog that isn't open yet) is only used once its dialog has been opened."""
        deadline = time.monotonic() + IMPORT_STAGE_SEC
        clicked = False
        while True:
            state()
            if not clicked:
                cdp.choosers.clear()   # only a chooser opened after this click is the import's
                clicked = cdp.function(ctx, PM_CLICK_FN, PM_ORIGIN, labels, False, gesture=True) == "clicked"
            if clicked and (got := file_input()):
                return got
            if time.monotonic() >= deadline:
                return None
            time.sleep(IMPORT_POLL)

    found = None
    for labels in OPEN_LABELS:   # "Import" (opens the dialog), then "Select file" (a chooser for a detached input)
        if found := stage(labels):
            break
    if not found:
        raise _Stop("error", "no-file-input")
    failed0, done0 = state()
    try:
        csv_file.write(rows)   # the plaintext exists from here; its timer shreds it IMPORT_SEC later at the latest
    except (StoreError, OSError):   # wrong group or mode, a read-only dir: nothing went to the page
        raise _Stop("error", "import-file") from None
    cdp.call("DOM.setFileInputFiles", {"files": [str(csv_file.path)], **found})
    confirmed = False
    while time.monotonic() < csv_file.deadline:
        failed, done = state()
        if failed > failed0:   # failures first: "2 couldn't be imported" never reads as imported
            raise _Stop("error", "import-failed")
        if confirmed and done > done0:   # a NEW positive count, after the dialog's Import was clicked
            return
        if not confirmed and cdp.function(ctx, PM_CLICK_FN, PM_ORIGIN, CONFIRM_LABELS, True,
                                          gesture=True) == "clicked":
            confirmed, done0 = True, done   # text that was there before the confirm click proves nothing
        time.sleep(IMPORT_POLL)
    raise _Stop("error", "no-confirmation")


# --- intake: the write-only phone form ---

FORM = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>web cred: add a login</title>
<style>body{font:16px system-ui,sans-serif;margin:0 auto;max-width:28rem;padding:1rem;background:#fff;color:#111}
label{display:block;margin:.8rem 0 .2rem}input{box-sizing:border-box;width:100%%;padding:.6rem;font-size:16px}
button{margin-top:1rem;padding:.7rem 1.4rem;font-size:16px}p.note{color:#a00}
@media (prefers-color-scheme:dark){body{background:#111;color:#eee}}</style></head>
<body><h1>Add a login</h1>%(note)s
<form method="post" autocomplete="off">
<label for="o">Site (origin)</label><input id="o" name="origin" type="url" required autocomplete="off"
 autocapitalize="off" spellcheck="false" placeholder="https://example.com">
<label for="u">Username</label><input id="u" name="username" type="text" required autocomplete="off"
 autocapitalize="off" spellcheck="false">
<label for="p">Password</label><input id="p" name="password" type="password" required autocomplete="off">
<button type="submit">Save</button></form>
<p>Saved encrypted on odin. Nothing you type here is ever shown back.</p></body></html>
"""
SAVED = "<!doctype html><meta charset=utf-8><title>saved</title><p>saved</p>\n"
CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"


class _QuietServer(HTTPServer):
    def handle_error(self, request, client_address):   # never a traceback (it could quote a request) in the journal
        print(f"webcred: intake request: {sys.exc_info()[0].__name__}", file=sys.stderr, flush=True)


class Intake:
    """A one-time, expiring form on 127.0.0.1:<random port>, path /i/<token>. tailscale serve (run by the client,
    which is the tailnet operator) publishes it under /web-cred; tailscale strips the mount."""

    def __init__(self, courier: "Courier", host: str | None, caller: Caller):
        self.courier, self.caller = courier, caller
        self.token = secrets.token_urlsafe(24)
        self.path = f"/i/{self.token}".encode()
        self.allowed_origins = {f"http://{host}", f"http://{host.split('.')[0]}"} if host else set()
        self.url = f"http://{host}{INTAKE_MOUNT}/i/{self.token}" if host else None
        self.deadline = time.monotonic() + INTAKE_SEC
        self.expires = int(time.time() + INTAKE_SEC)
        self.bad = 0
        self.used = False   # set before the "saved" reply goes out: the form can never take a second post
        self.result: str | None = None
        self.done = threading.Event()
        self._finish_lock = threading.Lock()
        intake = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "webcred"
            sys_version = ""
            timeout = INTAKE_REQ_TIMEOUT

            def log_message(self, *a):   # never log: the path carries the token
                pass

            def _send(self, status: int, body: str, form: bool = False):
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Pragma", "no-cache")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", CSP)
                self.end_headers()
                self.wfile.write(data)

            def _mine(self) -> bool:
                live = not intake.used and not intake.done.is_set()
                return live and hmac.compare_digest(self.path.split("?", 1)[0].encode("utf-8", "replace"),
                                                    intake.path)

            def do_GET(self):
                if not self._mine():
                    return self._send(404, "not found\n")
                self._send(200, FORM % {"note": ""})

            def do_POST(self):
                if not self._mine():
                    return self._send(404, "not found\n")
                origin_hdr = self.headers.get("Origin")
                if origin_hdr is not None and origin_hdr not in intake.allowed_origins:
                    return self._send(403, "forbidden\n")
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if not 0 <= length <= INTAKE_MAX_BODY:
                    intake.rejected()
                    return self._send(413, "too large\n")
                try:
                    fields = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"),
                                                   max_num_fields=10)
                except ValueError:   # too many fields
                    intake.rejected()
                    return self._send(400, FORM % {"note": '<p class="note">not saved: malformed form</p>'})
                why = intake.submit(fields)
                if why is None:
                    self._send(200, SAVED)
                    threading.Thread(target=intake.finish, args=("saved",), daemon=True).start()
                else:
                    self._send(400, FORM % {"note": f'<p class="note">not saved: {html.escape(why)}</p>'})

        self.server = _QuietServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        self.thread.start()

    def rejected(self) -> None:
        self.bad += 1
        if self.bad >= INTAKE_MAX_BAD:
            threading.Thread(target=self.finish, args=("too-many-attempts",), daemon=True).start()

    def submit(self, fields: dict) -> str | None:
        """Validate and store one posted credential. Returns None when saved, else a fixed reason."""
        one = {k: (v[0] if len(v) == 1 else None) for k, v in fields.items()}
        origin = normalize_origin(one.get("origin"))
        username, password = one.get("username"), one.get("password")
        if origin is None:
            why = "the site must be an http(s) origin like https://example.com"
        elif not valid_username(username):
            why = "username missing or invalid"
        elif not isinstance(password, str) or not 0 < len(password) <= MAX_SECRET or "\x00" in password:
            why = "password missing or too long"
        else:
            store = self.courier.store
            try:
                with store.lock:
                    old = [cid for cid, _ in store.find(origin, username)]
                    store.save(origin, username, password)
                    for cid in old:   # the same account, typed again: the previous record is kept 30 days
                        store.supersede(cid)
                        store.audit("supersede", origin, self.caller, "moved")
            except (OSError, StoreError):
                why = "the store refused it"
            else:
                self.used = True
                store.audit("intake", origin, self.caller, "saved")
                for _ in store.prune_superseded():
                    store.audit("prune", None, self.caller, "deleted")
                return None
        self.rejected()
        return why

    def finish(self, result: str) -> None:
        """Stop the form server (the token dies with it), then publish the result. Idempotent."""
        with self._finish_lock:
            if self.done.is_set():
                return
            self.server.shutdown()
            self.server.server_close()
            self.result = result
            self.done.set()


def valid_username(u) -> bool:
    return isinstance(u, str) and 0 < len(u) <= MAX_USER and u.isprintable()


# --- the request handler ---

def _peer(conn: socket.socket) -> Caller:
    try:
        pid, uid, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        return Caller(pid, uid)
    except OSError:
        return NOBODY


def default_allowed_uids() -> set[int]:
    try:
        return {0, pwd.getpwnam(ALLOWED_USER).pw_uid}
    except KeyError:
        return {0}


def default_import_gid() -> int:
    """The identity browser's group (it runs as ALLOWED_USER): the import CSV must be readable by it."""
    try:
        return pwd.getpwnam(ALLOWED_USER).pw_gid
    except KeyError:
        return -1   # no such group: the import dir check refuses


def _endpoint(req: dict) -> tuple[int, str] | None:
    """(port, page target id) of the loopback CDP endpoint a fill or sync-google names, or None."""
    cdp = req.get("cdp")
    m = CDP_RE.fullmatch(cdp) if isinstance(cdp, str) else None
    port = int(m.group(1)) if m else 0
    target = req.get("target")
    if not 0 < port < 65536 or not isinstance(target, str) or not TARGET_RE.fullmatch(target):
        return None
    return port, target


def _read_line(conn: socket.socket) -> bytes | None:
    buf = b""
    while b"\n" not in buf:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
        if len(buf) > MAX_LINE:
            return None
    return buf.split(b"\n", 1)[0]


def _send(conn: socket.socket, obj: dict) -> None:
    conn.sendall(json.dumps(obj).encode() + b"\n")


def _client_gone(conn: socket.socket) -> bool:
    try:
        readable, _, _ = select.select((conn,), (), (), 0)
        return bool(readable) and conn.recv(1, socket.MSG_PEEK) == b""
    except (BlockingIOError, TimeoutError):
        return False
    except OSError:
        return True


class Courier:
    def __init__(self, store: Store, allowed_uids: set[int] | None = None, import_path: Path | None = None,
                 import_gid: int | None = None):
        self.store = store
        self.allowed_uids = default_allowed_uids() if allowed_uids is None else set(allowed_uids)
        self.intake: Intake | None = None
        self.stopping = False
        self._intake_lock = threading.Lock()
        self.import_path = IMPORT_DIR / IMPORT_NAME if import_path is None else Path(import_path)
        self.import_gid = default_import_gid() if import_gid is None else import_gid
        self._import_lock = threading.Lock()   # one import (one CSV) at a time

    def refuse(self, conn: socket.socket, why: str) -> None:
        self.store.audit("refused", None, _peer(conn), why)
        _send(conn, {"result": "error", "why": why})

    def serve_conn(self, conn: socket.socket) -> None:
        pid = _peer(conn)   # (pid, uid) of the caller
        if pid.uid not in self.allowed_uids:
            return self.refuse(conn, "forbidden")
        conn.settimeout(CONN_TIMEOUT)
        try:
            line = _read_line(conn)
        except TimeoutError:
            return self.store.audit("invalid", None, pid, "timeout")
        try:
            req = json.loads(line) if line is not None else None
        except ValueError:
            req = None
        if not isinstance(req, dict) or not isinstance(req.get("op"), str):
            self.store.audit("invalid", None, pid, "error")
            return _send(conn, {"result": "error", "why": "bad-request"})
        op = req["op"]
        if op == "intake-url":
            try:
                return self.op_intake(conn, req, pid)
            except Exception as e:  # noqa: BLE001 - the waiting client needs a reply even if intake setup fails
                print(f"webcred: intake-url failed: {type(e).__name__}", file=sys.stderr, flush=True)
                try:
                    self.store.audit("intake", None, pid, "error")
                except Exception:  # noqa: BLE001 - an audit failure must not suppress the client reply
                    pass
                try:
                    _send(conn, {"result": "error", "why": "internal"})
                except OSError:
                    pass
                return
        handler = {"list": self.op_list, "gen": self.op_gen, "fill": self.op_fill, "rm": self.op_rm,
                   "audit": self.op_audit, "sync-google": self.op_sync_google}.get(op)
        if handler is None:
            self.store.audit("invalid", None, pid, "error")
            return _send(conn, {"result": "error", "why": "unknown-op"})
        extra = {"fill": {"cdp_port": None, "target": None},
                 "sync-google": {"count": 0, "cdp_port": None, "target": None}}.get(op, {})
        try:
            reply, origin, result = handler(req, extra)
        except Exception as e:  # noqa: BLE001 - one failed request must not take the daemon down
            print(f"webcred: {op} failed: {type(e).__name__}", file=sys.stderr, flush=True)
            reply, origin, result = {"result": "error", "why": "internal"}, None, "error"
        self.store.audit(op, origin, pid, result, **extra)
        _send(conn, reply)

    # Each op returns (reply, audit origin, audit result); fill also fills in its audit extras.

    def op_list(self, req, extra):
        return {"items": self.store.items()}, None, "ok"

    def op_audit(self, req, extra):
        n = req.get("n", 20)
        if not isinstance(n, int) or isinstance(n, bool) or not 0 < n <= 1000:
            return {"result": "error", "why": "bad-n"}, None, "error"
        return {"items": self.store.audit_tail(n)}, None, "ok"

    def op_gen(self, req, extra):
        origin = normalize_origin(req.get("origin"))
        username, length = req.get("username"), req.get("length", GEN_DEFAULT)
        if origin is None:
            return {"result": "error", "why": "bad-origin"}, None, "error"
        if not valid_username(username):
            return {"result": "error", "why": "bad-username"}, origin, "error"
        if not isinstance(length, int) or isinstance(length, bool) or not GEN_MIN <= length <= GEN_MAX:
            return {"result": "error", "why": "bad-length"}, origin, "error"
        with self.store.lock:
            if self.store.find(origin, username):
                return {"result": "exists"}, origin, "exists"
            cid = self.store.save(origin, username, gen_password(length))
        return {"result": "ok", "id": cid}, origin, "ok"

    def op_rm(self, req, extra):
        cid = req.get("id")
        if not isinstance(cid, str) or not ID_RE.fullmatch(cid):
            return {"result": "error", "why": "bad-id"}, None, "error"
        origin = None
        try:
            origin = self.store.load(cid)["origin"]
        except (OSError, ValueError, StoreError):
            pass
        if self.store.remove(cid):
            return {"result": "removed"}, origin, "removed"
        return {"result": "no-credential"}, None, "no-credential"

    def op_fill(self, req, extra):
        bad = {"result": "error", "why": "bad-request"}
        endpoint = _endpoint(req)
        submit = req.get("submit", False)
        sels = [req.get("user_sel"), req.get("pass_sel")]
        if endpoint is None:
            return bad, None, "error"
        port, target = endpoint
        extra.update(cdp_port=port, target=target)   # which browser endpoint got (or was refused) the fill
        if not isinstance(submit, bool) or not any(sels):
            return bad, None, "error"
        if not all(s is None or (isinstance(s, str) and 0 < len(s) <= MAX_SEL) for s in sels):
            return bad, None, "error"
        username = req.get("username")
        if username is not None and not valid_username(username):
            return bad, None, "error"
        cid = req.get("id")
        if cid is not None:
            if not isinstance(cid, str) or not ID_RE.fullmatch(cid):
                return bad, None, "error"
            if not self.store.exists(cid):
                return {"result": "no-credential"}, None, "no-credential"
            rec = self.store.load(cid)
        else:
            origin = normalize_origin(req.get("origin"))
            if origin is None:
                return bad, None, "error"
            found = self.store.find(origin, username)
            if not found:
                return {"result": "no-credential"}, origin, "no-credential"
            if len(found) > 1:
                return {"result": "error", "why": "ambiguous: pass username or id"}, origin, "error"
            rec = found[0][1]
        steps = []
        if sels[0]:
            steps.append((sels[0], rec["username"], "user"))
        if sels[1]:
            steps.append((sels[1], rec["secret"], "password"))
        try:
            result, why = fill_page(port, target, rec["origin"], steps, submit)
        except Exception as e:  # noqa: BLE001 - CDP unreachable, closed or slow: an error, never a retry elsewhere
            print(f"webcred: fill: {type(e).__name__}", file=sys.stderr, flush=True)
            result, why = "error", "cdp"
        reply = {"result": result} if why is None else {"result": result, "why": why}
        return reply, rec["origin"], result

    def op_sync_google(self, req, extra):
        endpoint = _endpoint(req)
        if endpoint is None:
            return {"result": "error", "why": "bad-request"}, None, "error"
        port, target = endpoint
        extra.update(cdp_port=port, target=target)
        if not import_dir_ready(self.import_path.parent, self.import_gid):
            return {"result": "error", "why": "import-dir"}, PM_ORIGIN, "error"
        if not self._import_lock.acquire(blocking=False):
            return {"result": "error", "why": "busy"}, PM_ORIGIN, "error"
        csv_file = ImportFile(self.import_path, self.import_gid)
        try:
            try:
                rows = csv_rows(self.store.records())
                if not rows:
                    result, why = "error", "no-credentials"
                else:
                    try:
                        result, why = import_to_google(port, target, rows, csv_file)
                    except Exception as e:  # noqa: BLE001 - CDP unreachable, closed or slow: an error, CSV shredded
                        print(f"webcred: sync-google: {type(e).__name__}", file=sys.stderr, flush=True)
                        result, why = "error", "cdp"
            finally:
                csv_file.close()   # may raise (the shred failed): the timer is still armed, the lock still freed
        finally:
            self._import_lock.release()
        extra["count"] = csv_file.count   # rows that reached the CSV: never a username or a password
        reply = {"result": result, **({"why": why} if why else {}), "count": csv_file.count}
        return reply, PM_ORIGIN, result

    def op_intake(self, conn: socket.socket, req: dict, pid: Caller) -> None:
        host = req.get("host")
        if host is not None and (not isinstance(host, str) or not HOST_RE.fullmatch(host)):
            self.store.audit("intake-url", None, pid, "error")
            return _send(conn, {"result": "error", "why": "bad-host"})
        with self._intake_lock:
            if self.intake is not None:   # the newest intake wins; the older link dies now
                self.intake.finish("replaced")
            intake = self.intake = Intake(self, host, pid)
        try:
            self.store.audit("intake-url", None, pid, "ok")
            _send(conn, {"url": intake.url, "port": intake.port, "expires": intake.expires})
            while not intake.done.wait(0.5):
                if time.monotonic() >= intake.deadline:
                    intake.finish("expired")
                elif _client_gone(conn):
                    intake.finish("cancelled")
            if intake.result != "saved":
                self.store.audit("intake", None, pid, intake.result or "error")
            if intake.result != "cancelled":
                _send(conn, {"result": intake.result})
        finally:
            if not intake.done.is_set():
                intake.finish("error")
            with self._intake_lock:
                if self.intake is intake:
                    self.intake = None


# --- the serve loop ---

def serve(listener: socket.socket, courier: Courier, idle_sec: float = IDLE_SEC) -> None:
    """Accept until idle: no open connection (an intake holds one) for idle_sec. systemd keeps the listening
    socket, so the next connection starts a fresh daemon."""
    listener.settimeout(0.2)
    lock = threading.Lock()
    state = {"active": 0, "last": time.monotonic()}

    def run(conn):
        try:
            with conn:
                courier.serve_conn(conn)
        except Exception as e:  # noqa: BLE001
            print(f"webcred: connection: {type(e).__name__}", file=sys.stderr, flush=True)
        finally:
            with lock:
                state["active"] -= 1
                state["last"] = time.monotonic()

    while not courier.stopping:
        try:
            conn, _ = listener.accept()
        except TimeoutError:
            with lock:
                if state["active"] == 0 and time.monotonic() - state["last"] >= idle_sec:
                    return
            continue
        conn.setblocking(True)
        with lock:
            full = state["active"] >= MAX_CONNS
            if not full:
                state["active"] += 1
            state["last"] = time.monotonic()
        if full:
            try:
                with conn:
                    conn.settimeout(2)
                    courier.refuse(conn, "busy")
            except OSError:
                pass
            continue
        threading.Thread(target=run, args=(conn,), daemon=True).start()


def activated_socket() -> socket.socket:
    """The listening socket systemd passed (fd 3), or exit: this daemon only runs socket-activated."""
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or int(os.environ.get("LISTEN_FDS") or 0) < 1:
        raise SystemExit("webcred: start me through webcred.socket")
    return socket.socket(fileno=3)


def _not_dumpable() -> None:
    """PR_SET_DUMPABLE 0: no core file of this process (it holds decrypted records). age, a child, is covered by
    the unit's LimitCORE=0."""
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)
    except (OSError, AttributeError):
        print("webcred: prctl(PR_SET_DUMPABLE) failed", file=sys.stderr, flush=True)


def main() -> int:
    os.umask(0o077)
    _not_dumpable()
    store = Store(ROOT)
    for _ in store.prune_superseded():
        store.audit("prune", None, NOBODY, "deleted")
    sweep_imports()   # a CSV left by a killed import (ExecStopPost shreds it too)
    serve(activated_socket(), Courier(store))
    return 0


if __name__ == "__main__":
    sys.exit(main())
