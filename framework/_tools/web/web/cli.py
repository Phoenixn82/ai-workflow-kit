"""`web` CLI: argparse skeleton. Each real subcommand is filled in by a later Phase B task;
until then it prints `not built yet` and exits 5."""
import argparse
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import tomllib
import traceback
from pathlib import Path
from urllib.parse import urlsplit

from . import act as actmod
from . import bg
from . import crawl as crawlmod
from . import cred as credmod
from . import extract as extractmod
from . import gh as ghmod
from . import handoff as handoffmod
from . import host
from . import notify
from . import output_scan
from . import netguard
from . import read as reader
from . import search as searchmod
from . import taint
from . import tripwire
from .config import LANES
from .fence import fence, strip_hidden

UNBUILT_COMMANDS: tuple[str, ...] = ()


def footer(lane: str, ms: int, notes: tuple[str, ...] = (), hidden: int | None = None) -> str:
    line = f"web: host={socket.gethostname().lower()} lane={lane} ms={ms}"
    if hidden is not None:
        line += f" untrusted=1 hidden={hidden}"
    return f"{line} note={','.join(notes)}" if notes else line


def _version(*argv: str) -> str | None:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else None


GUARD_HOOK = "webcred-guard.sh"
DISPATCH = "dispatch/dispatch.sh"   # audit 2026-10-12 item G: one PreToolUse entry runs run.py's PRE table


def _dispatched() -> bool:
    """The hook dispatcher's run.py lists the webcred guard in its PRE table."""
    try:
        src = (Path.home() / ".claude" / "hooks" / "dispatch" / "run.py").read_text()
    except OSError:
        return False
    m = re.search(r"^PRE = \[(.*?)^\]", src, re.S | re.M)
    return bool(m) and GUARD_HOOK in m.group(1)


def _guard(registry: Path) -> str:
    """registered: a PreToolUse entry in this Claude settings.json / Codex hooks.json runs the webcred guard (itself
    or through the dispatcher) and the script it runs exists (courier/install-guards.sh puts both there)."""
    script = Path.home() / ".claude" / "hooks" / GUARD_HOOK
    try:
        pre = json.loads(registry.read_text()).get("hooks", {}).get("PreToolUse", [])
        cmds = [str(h.get("command", "")) for e in pre for h in e.get("hooks", [])]
    except (OSError, ValueError, AttributeError, TypeError):
        cmds = []
    found = any(GUARD_HOOK in c for c in cmds) or (any(DISPATCH in c for c in cmds) and _dispatched())
    return "registered" if found and script.is_file() else "missing"


CODEX_UNTRUSTED = "registered (untrusted — trust it in codex /hooks)"
CODEX_UNKNOWN = "registered (trust unknown — trust it in codex /hooks)"
CODEX_DISABLED = "registered (disabled — enable it in codex /hooks)"


def _codex_hook_hash(event: str, entry: dict, handler: dict) -> str:
    """The trust hash Codex computes for one hooks.json command handler (codex-rs hooks/src/engine/discovery.rs
    hook_hash + config/src/fingerprint.rs version_for_toml): sha256 of the key-sorted compact JSON of {event_name,
    matcher, hooks: [the handler, normalized: timeout defaulted to 600, async, statusMessage]}."""
    timeout = handler.get("timeout", 600)
    norm = {"type": "command", "command": handler.get("command"),
            "timeout": max(1, timeout) if type(timeout) is int else timeout, "async": bool(handler.get("async", False))}
    if isinstance(handler.get("statusMessage"), str):
        norm["statusMessage"] = handler["statusMessage"]
    ident = {"event_name": re.sub(r"(?<!^)([A-Z])", r"_\1", event).lower(), "hooks": [norm]}
    if entry.get("matcher") is not None:
        ident["matcher"] = entry["matcher"]
    blob = json.dumps(ident, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def _codex_guard() -> str:
    """trusted: ~/.codex/config.toml (only ever read) has hooks.state."<hooks.json>:pre_tool_use:<i>:<j>" for the
    guard's entry, enabled, with Codex's trust hash of that entry as it is now. No such entry: untrusted (Codex
    doesn't run it). A different hash is what Codex calls "modified" (trusted before the entry changed), but a hash
    this code computes differently from some future Codex would look the same: trust unknown."""
    codex = Path.home() / ".codex"
    if _guard(codex / "hooks.json") != "registered":
        return "missing"
    try:
        pre = json.loads((codex / "hooks.json").read_text())["hooks"]["PreToolUse"]
        ours = [(i, j, e, h) for i, e in enumerate(pre) for j, h in enumerate(e.get("hooks", []))
                if GUARD_HOOK in str(h.get("command", ""))]
        states = tomllib.loads((codex / "config.toml").read_text()).get("hooks", {}).get("state", {})
    except FileNotFoundError:
        return CODEX_UNTRUSTED
    except (OSError, ValueError, KeyError, TypeError, AttributeError):   # tomllib.TOMLDecodeError is a ValueError
        return CODEX_UNKNOWN
    found = set()
    for i, j, entry, handler in ours:
        st = states.get(f"{codex / 'hooks.json'}:pre_tool_use:{i}:{j}") if isinstance(states, dict) else None
        if not isinstance(st, dict) or "trusted_hash" not in st:
            found.add(CODEX_UNTRUSTED)
        elif st.get("enabled") is False:
            found.add(CODEX_DISABLED)
        elif st["trusted_hash"] == _codex_hook_hash("PreToolUse", entry, handler):
            found.add("trusted")
        else:
            found.add(CODEX_UNKNOWN)
    return next((w for w in ("trusted", CODEX_UNKNOWN, CODEX_DISABLED) if w in found), CODEX_UNTRUSTED)


COURIER_PING_SEC = 2.0
LINE_IDENTITY_SEC = 0.2


def _courier_responding() -> bool:
    """A `list` round trip within 2 s. Only whether a list came back is used: no id, origin or username is kept."""
    if not credmod.SOCKET.exists():
        return False
    try:
        return isinstance(credmod.call({"op": "list"}, timeout=COURIER_PING_SEC).get("items"), list)
    except (credmod.CredError, OSError, ValueError):
        return False


def _status() -> dict:
    from . import identities, anon
    return {
        "host": socket.gethostname().lower(),
        "lanes": LANES,
        "engines": {
            "scrapling": _version("scrapling", "--version"),
            "agent-browser": _version("agent-browser", "--version"),
            "gh": _version("gh", "--version"),
        },
        "identities": identities.status(),
        "anon": anon.status(),
        "identity": {"profile": actmod.IDENTITY_DIR.exists(), "browser_running": actmod.browser_port() is not None},
        "courier": {"socket": credmod.SOCKET.exists(), "responding": _courier_responding()},
        "notify": {"configured": notify.configured()},
        "guard": {"claude": _guard(Path.home() / ".claude" / "settings.json"), "codex": _codex_guard()},
    }


def _line_identity_up() -> bool:
    if not actmod.IDENTITY_DIR.exists():
        return False
    port = actmod._port_file()
    if port is None:
        return False
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=LINE_IDENTITY_SEC):
            return True
    except (OSError, ValueError):
        return False


def _status_line() -> str:
    from . import identities
    names = identities.names()
    identity = (f"identity {'up' if _line_identity_up() else 'down'}" if len(names) == 1 else
                f"identities {sum(row['running'] for row in identities.status())}/{len(names)} up")
    return (f"web: {socket.gethostname().lower()} ok · {identity} "
            f"· notify {'ok' if notify.configured() else 'missing'}")


def _bg_flags(p: argparse.ArgumentParser, kind: str) -> None:
    g = p.add_mutually_exclusive_group()
    g.add_argument("--background", action="store_true", help="print the link and exit 0 at once; the wait goes on "
                                                             "in a detached process (see --status / --wait)")
    g.add_argument("--status", action="store_true", help=f"the latest background {kind}: running | how it ended")
    g.add_argument("--wait", type=float, nargs="?", const=bg.WAIT_SEC, default=None, metavar="SEC",
                   help=f"wait up to SEC (default {bg.WAIT_SEC:g}) for the latest background {kind} to end")
    g.add_argument("--cancel", action="store_true", help=f"stop the running {kind} (SIGTERM; it takes its link "
                                                         "down on the way out)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="web", allow_abbrev=False)
    # Handled (and removed from argv) by host.ensure_host before argparse runs; listed here for --help.
    parser.add_argument("--host", choices=host.HOSTS, help="run on this host (default odin; must come before the "
                                                           "subcommand). thor: read/crawl/search/extract/gh only")
    sub = parser.add_subparsers(dest="command")
    untaint_parser = sub.add_parser("untaint", allow_abbrev=False, help="local recovery from the user's own terminal")
    recovery = untaint_parser.add_mutually_exclusive_group()
    recovery.add_argument("prefix", nargs="?", help="unique session id or listed name prefix")
    recovery.add_argument("--all", dest="all_sessions", action="store_true", help="clear all local sessions")
    exempt_parser = sub.add_parser("exempt", allow_abbrev=False, help="manage local exact file and sha256 exemptions")
    exempt_sub = exempt_parser.add_subparsers(dest="exempt_command", required=True)
    exempt_sub.add_parser("add", allow_abbrev=False).add_argument("file")
    exempt_sub.add_parser("list", allow_abbrev=False)
    exempt_sub.add_parser("rm", allow_abbrev=False).add_argument("file")
    for name in UNBUILT_COMMANDS:
        sub.add_parser(name, allow_abbrev=False)
    # `web act ...` bypasses argparse (see _main): this entry only documents it in --help.
    act_parser = sub.add_parser("act", allow_abbrev=False, help="auto-route public interaction to anon on thor and "
                               "account work to identity windows on odin (open, snapshot, click, fill, ...)")
    act_parser.add_argument("--as", dest="identity", metavar="NAME", help="named identity (or WEB_AS; before the command)")
    act_parser.add_argument("--anon", action="store_true", help="force a disposable interactive browser on thor")
    act_parser.add_argument("command", nargs=argparse.REMAINDER)
    handoff_parser = sub.add_parser("handoff", allow_abbrev=False,
        help="put a human-only step on the user's phone (tailnet live view of "
             "the identity browser) and wait until it clears")
    handoff_parser.add_argument("--as", dest="identity", metavar="NAME", help="named identity (or WEB_AS)")
    handoff_parser.add_argument("reason", nargs="?", help="what the user should do, shown on the phone page")
    handoff_parser.add_argument("--until-url", default=None, metavar="GLOB", help="cleared once the URL matches "
                                "(with neither --until-*, the page must show a CAPTCHA/2FA gate or a wall, and it "
                                "clears once the page moves on with none left)")
    handoff_parser.add_argument("--until-selector", default=None, metavar="CSS", help="cleared once this exists")
    handoff_parser.add_argument("--timeout", type=float, default=900, help="seconds (default and maximum 900)")
    _bg_flags(handoff_parser, "handoff")
    login_parser = sub.add_parser("login", allow_abbrev=False,
        help="sign in on the identity browser's current tab: Sign in with "
             "Google by default; with --user-sel/--pass-sel the courier types a "
             "stored credential after an exact origin check (it never shows it)")
    login_parser.add_argument("--as", dest="identity", metavar="NAME", help="named identity (or WEB_AS)")
    login_parser.add_argument("origin", help="the site's origin, e.g. https://example.com (selects the credential)")
    login_parser.add_argument("--google", action="store_true", help="click the page's Sign in with Google and pick "
                                                                     "the identity profile's account (the default "
                                                                     "when no selectors are given)")
    login_parser.add_argument("--user-sel", default=None, metavar="CSS", help="the username field")
    login_parser.add_argument("--pass-sel", default=None, metavar="CSS", help="the password field")
    login_parser.add_argument("--id", default=None, help="a credential id from `web cred list`")
    login_parser.add_argument("--username", default=None, help="pick this account when the origin has several")
    login_parser.add_argument("--no-submit", action="store_true", help="fill only; don't press Enter")
    cred_parser = sub.add_parser("cred", allow_abbrev=False,
        help="the credential courier's control plane (never shows a secret)")
    cred_sub = cred_parser.add_subparsers(dest="cred_command")
    cred_sub.add_parser("list", allow_abbrev=False, help="stored credentials: id, origin, username, created")
    cred_gen = cred_sub.add_parser("gen", allow_abbrev=False,
        help="store a new random password (signups); fill it with `web login`")
    cred_gen.add_argument("origin")
    cred_gen.add_argument("username")
    cred_gen.add_argument("--length", type=int, default=24)
    cred_rm = cred_sub.add_parser("rm", allow_abbrev=False, help="delete one credential")
    cred_rm.add_argument("id")
    cred_intake = cred_sub.add_parser("intake", allow_abbrev=False,
        help="one-time tailnet phone form (10 minutes) for the user to add a "
             "login; pings the phone and blocks until the form is used or "
             "expires (or --background)")
    _bg_flags(cred_intake, "intake")
    cred_audit = cred_sub.add_parser("audit", allow_abbrev=False,
        help="the courier's audit log (time, op, origin, caller pid, result)")
    cred_audit.add_argument("--n", type=int, default=20)
    sync_google_parser = cred_sub.add_parser("sync-google", allow_abbrev=False,
        help="forward every stored credential to Google Password Manager (CSV "
             "import in the signed-in identity browser; exit 4 = a human step)")
    sync_google_parser.add_argument("--as", dest="identity", metavar="NAME", help="personal only (or WEB_AS)")
    read_parser = sub.add_parser("read", allow_abbrev=False,
        help="one URL as markdown, escalating through the read ladder")
    read_parser.add_argument("url")
    read_parser.add_argument("--lane", default="auto", choices=["auto", *LANES["read"]["rungs"]])
    read_parser.add_argument("--budget", type=float, default=110, metavar="SEC",
                             help="total seconds for the whole ladder (default 110: under an agent's 2-minute tool "
                                  "timeout; 0 = no limit). Out of budget: exit 3 with what was tried")
    read_out = read_parser.add_mutually_exclusive_group()
    read_out.add_argument("--json", action="store_true", help="the full result (minus html) as JSON")
    read_out.add_argument("--raw", action="store_true", help="the page HTML instead of markdown")
    read_parser.add_argument("--include-html", action="store_true", help="keep HTML in read JSON")
    gh_parser = sub.add_parser("gh", allow_abbrev=False, help="GitHub repo intel via gh api")
    gh_sub = gh_parser.add_subparsers(dest="gh_command")
    gh_search = gh_sub.add_parser("search", allow_abbrev=False)
    gh_search.add_argument("query")
    gh_search.add_argument("--n", type=int, default=10)
    gh_search.add_argument("--json", action="store_true")
    gh_repo = gh_sub.add_parser("repo", allow_abbrev=False)
    gh_repo.add_argument("repo")
    gh_repo.add_argument("--json", action="store_true")
    gh_repo.add_argument("--readme", action="store_true")
    crawl_parser = sub.add_parser("crawl", allow_abbrev=False,
        help="bounded crawl: discover same-host URLs, read each, write a manifest")
    crawl_parser.add_argument("url")
    crawl_parser.add_argument("--max", type=int, default=50)
    crawl_parser.add_argument("--match", default=None, help="glob filter applied to discovered URLs")
    crawl_parser.add_argument("--out", default=None, help="output dir (default: ./crawl-<host>)")
    crawl_parser.add_argument("--budget", type=float, default=110, metavar="SEC",
                              help="total seconds (default 110; 0 = no limit): stops cleanly, re-run the same "
                                   "command to resume")
    extract_parser = sub.add_parser("extract", allow_abbrev=False,
        help="schema-validated extraction via the free LLM extract lane")
    extract_parser.add_argument("source", help="a URL (read through the read ladder) or a local file")
    extract_parser.add_argument("--schema", required=True, help="a path to a JSON Schema file, or inline JSON")
    extract_parser.add_argument("--json", action="store_true", help="compact single-line JSON (default: pretty)")
    extract_parser.add_argument("--tier", choices=("public", "private", "business-pii"), default="public",
                                help="data tier: a local file needs private or business-pii, which routes to "
                                     "thor's local model (auto:local) and never to a cloud provider")
    search_parser = sub.add_parser("search", allow_abbrev=False,
        help="keyless web search (DuckDuckGo, falling back to Mojeek)")
    search_parser.add_argument("query")
    search_parser.add_argument("--n", type=int, default=10)
    search_parser.add_argument("--read", type=int, default=0, metavar="K",
                                help="hydrate the top K results' markdown via the read ladder")
    search_parser.add_argument("--json", action="store_true")
    status_parser = sub.add_parser("status", allow_abbrev=False)
    status_out = status_parser.add_mutually_exclusive_group()
    status_out.add_argument("--json", action="store_true")
    status_out.add_argument("--line", action="store_true")
    # Hidden debug command: proves the raven shim forwards argv unmangled (one line per arg, as received).
    echo_parser = sub.add_parser("_echo-args", allow_abbrev=False, help=argparse.SUPPRESS)
    echo_parser.add_argument("rest", nargs=argparse.REMAINDER)
    for fetch_parser in (read_parser, crawl_parser, extract_parser, search_parser):
        fetch_parser.add_argument("--allow-private", action="store_true", help="logged one-command network bypass")
    return parser


def _emit_footer(start: float, lane: str = "-/-", notes: tuple[str, ...] = (),
                 hidden: int | None = None) -> None:
    # Commands that use no engine (status, the not-built stubs, the debug echo) report lane "-/-".
    ms = int((time.monotonic() - start) * 1000)
    print(footer(lane, ms, notes, hidden=hidden), file=sys.stderr)


LANE_IDENTITY = f"{LANES['act']['primary']}/identity"
LANE_HANDOFF = f"{LANES['act']['primary']}/handoff"


def _diagnostic(text: str, source: str | None = None) -> int:
    """Engine and validator details are data, even when printed on stderr."""
    output_scan.scan(text)
    data = fence(text[:2000], source=source)
    sys.stderr.write(data.text)
    return data.hidden


def _guarded(name: str, lane: str, start: float, fn, detail: bool = True) -> int:
    """Every subcommand runs through this, so the exit-code contract holds whatever happens inside: a surprise is a
    bounded diagnostic + the footer + exit 5 (a traceback only with WEB_DEBUG=1); SystemExit (SIGTERM/SIGHUP, see
    _signals_exit: cleanup already ran on the way out) prints the footer, then exits. detail=False keeps the
    exception's text out of stderr (the courier commands)."""
    try:
        return fn()
    except SystemExit:
        print(f"web {name}: interrupted by a signal", file=sys.stderr)
        _emit_footer(start, lane)
        raise
    except actmod.ActError as e:
        hidden = _diagnostic(f"web {name}: {e}")
        _emit_footer(start, lane, hidden=hidden)
        return e.code
    except netguard.BoundaryError:
        _emit_footer(start, lane)
        return 5
    except Exception as e:  # noqa: BLE001 - keep the exit-code contract; a traceback only on request
        msg = f"web {name}: internal error: {type(e).__name__}"
        if detail and str(e):
            msg += ": " + " ".join(str(e).split())[:300]
        if detail:
            if os.environ.get("WEB_DEBUG") == "1":
                msg += "\n" + traceback.format_exc()
            hidden = _diagnostic(msg)
        else:
            print(msg, file=sys.stderr)
            hidden = None
        _emit_footer(start, lane, hidden=hidden)
        return 5


def _read(args, start: float) -> int:
    parts = urlsplit(args.url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        hidden = _diagnostic(f"web read: not an http(s) URL: {args.url!r}")
        _emit_footer(start, hidden=hidden)
        return 2
    try:
        r = reader.read(args.url, lane=args.lane, budget=args.budget or None)
    except reader.ReadError as e:
        kind = "blocked" if isinstance(e, reader.Blocked) else "read failed"
        print(f"web read: {kind}:", file=sys.stderr)
        hidden = _diagnostic("\n".join(e.evidence), source=args.url)
        _emit_footer(start, e.lane, e.notes, hidden=hidden)
        return e.code
    hidden = 0
    try:
        if args.json:
            hidden = strip_hidden(r.markdown)[1]
            data = {k: v for k, v in dataclasses.asdict(r).items() if k != "html" or args.include_html}
            output_scan.scan(data)
            data.update(untrusted=True, hidden_chars=hidden)
            print(json.dumps(data, ensure_ascii=False))
        else:
            output_scan.scan(r.html if args.raw else r.markdown)
            if args.raw:
                text = r.html
            else:
                page = fence(r.markdown, source=r.final_url or args.url)
                text, hidden = page.text, page.hidden
            sys.stdout.write(text if text.endswith("\n") else text + "\n")
        sys.stdout.flush()
    except BrokenPipeError:   # `web read URL | head`: the reader left; that's fine (Python's documented idiom)
        with contextlib.suppress(OSError, ValueError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())   # no second error at exit-time flush
    _emit_footer(start, f"{r.engine}/{r.rung}", r.notes, hidden=hidden)
    return 0


def _crawl_code(stats) -> int:
    """0 once anything was read (or there was nothing to do); with nothing read, 3 when every attempt was blocked,
    5 when errors were part of it."""
    if stats.ok:
        return 0
    if not (stats.blocked or stats.error):
        return 3 if stats.budget_exhausted else 0   # out of time before reading anything: like `web read`
    return 3 if not stats.error else 5


def _crawl(args, start: float) -> int:
    parts = urlsplit(args.url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        print(f"web crawl: not an http(s) URL: {args.url!r}", file=sys.stderr)
        _emit_footer(start)
        return 2
    out_dir = Path(args.out) if args.out else Path.cwd() / f"crawl-{parts.hostname.lower()}"
    try:
        stats = crawlmod.crawl(args.url, out_dir, max_pages=args.max, match=args.match, budget=args.budget or None)
    except OSError as e:
        print(f"web crawl: cannot write to {out_dir}: {e}", file=sys.stderr)
        _emit_footer(start)
        return 5
    print(f"web crawl: ok={stats.ok} blocked={stats.blocked} error={stats.error} skipped={stats.skipped} "
          f"-> {out_dir}", file=sys.stderr)
    if stats.budget_exhausted:
        print("web crawl: budget exhausted; re-run the same command to continue where it stopped (big crawls: in "
              "the background with --budget 0)", file=sys.stderr)
    _emit_footer(start, "crawl/-", ("budget",) if stats.budget_exhausted else (), hidden=stats.hidden)
    return _crawl_code(stats)


def _extract(args, start: float) -> int:
    try:
        schema = extractmod.load_schema(args.schema)
    except (OSError, ValueError) as e:
        hidden = _diagnostic(f"web extract: bad --schema: {e}")
        _emit_footer(start, hidden=hidden)
        return 2

    parts = urlsplit(args.source)
    is_url = parts.scheme in ("http", "https") and bool(parts.hostname)
    if not is_url and args.tier == "public":   # audit 2026-10-12: a private file must never reach a cloud route
        hidden = _diagnostic(f"web extract: {args.source!r} is a local file; add --tier private (or business-pii) "
                             "to extract it on thor's local model")
        _emit_footer(start, hidden=hidden)
        return 2
    model = "auto:extract" if args.tier == "public" else "auto:local"
    lane = "-/-"
    try:
        if is_url:
            r = netguard.fetch_read(args.source)
            markdown, lane = r.markdown, f"{r.engine}/{r.rung}"
        else:
            markdown = Path(args.source).read_text(encoding="utf-8")
    except reader.ReadError as e:
        kind = "blocked" if isinstance(e, reader.Blocked) else "read failed"
        print(f"web extract: {kind}:", file=sys.stderr)
        hidden = _diagnostic("\n".join(e.evidence), source=args.source)
        _emit_footer(start, e.lane, e.notes, hidden=hidden)
        return e.code
    except UnicodeDecodeError:
        hidden = _diagnostic(f"web extract: {args.source!r} is not a text file (UTF-8 expected)")
        _emit_footer(start, hidden=hidden)
        return 2
    except OSError as e:
        hidden = _diagnostic(f"web extract: cannot read {args.source!r}: {e}")
        _emit_footer(start, hidden=hidden)
        return 2

    try:
        data = extractmod.extract(markdown, schema, model=model)
    except extractmod.ExtractError as e:
        print("web extract: schema validation failed:", file=sys.stderr)
        hidden = _diagnostic("\n".join(e.errors), source=args.source)
        _emit_footer(start, lane, hidden=hidden)
        return 5

    output_scan.scan(data)
    hidden = 0
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        output = fence(json.dumps(data, ensure_ascii=False, indent=2), source=args.source)
        sys.stdout.write(output.text)
        hidden = output.hidden
    _emit_footer(start, lane, hidden=hidden)
    return 0


def _gh(args, start: float) -> int:
    if args.gh_command is None:
        print("web gh: a subcommand is required: search, repo", file=sys.stderr)
        _emit_footer(start, "gh/-")
        return 2
    try:
        if args.gh_command == "search":
            rows = ghmod.search(args.query, n=args.n)
            output_scan.scan(rows)
            if args.json:
                text = json.dumps([dict(row, untrusted=True) for row in rows], ensure_ascii=False)
            else:
                text = ghmod.format_search_markdown(rows)
        else:
            data = ghmod.repo(args.repo)
            output_scan.scan(data)
            if args.json:
                text = json.dumps(dict(data, untrusted=True), ensure_ascii=False)
            else:
                text = ghmod.format_repo_markdown(data, with_readme=args.readme)
    except ghmod.GhError as e:
        hidden = _diagnostic(f"web gh: {e}\n{e.stderr or ''}")
        _emit_footer(start, "gh/-", hidden=hidden)
        return 5
    hidden = 0
    if args.json:
        print(text)
    else:
        output = fence(text)
        sys.stdout.write(output.text)
        hidden = output.hidden
    _emit_footer(start, "gh/-", hidden=hidden)
    return 0


def _search(args, start: float) -> int:
    try:
        result = searchmod.search(args.query, n=args.n)
        rows = searchmod.hydrate(result.rows, args.read) if args.read else result.rows
    except searchmod.SearchError as e:
        print("web search: no results from ddg or mojeek", file=sys.stderr)
        _emit_footer(start, "-/-", e.notes, hidden=0)
        return e.code
    output_scan.scan(rows)
    hidden = 0
    if args.json:
        print(json.dumps([dict(row, untrusted=True) for row in rows], ensure_ascii=False))
    else:
        lines = []
        for row in rows:
            lines.append(f"{row['title']}\n  {row['url']}\n  {row['snippet']}")
            if "markdown" in row:
                lines.append(row["markdown"])
            elif "read_error" in row:
                lines.append(f"  [read failed: {row['read_error']}]")
        output = fence("\n".join(lines))
        sys.stdout.write(output.text)
        hidden = output.hidden
    _emit_footer(start, f"{result.engine}/-", result.notes, hidden=hidden)
    return 0


def _act(argv: list[str], start: float) -> int:
    from . import act_routing, anon
    if argv == ["--help"]:
        try:
            build_parser().parse_args(["act", "--help"])
        except SystemExit:
            return 0
    counts, lane = [], LANE_IDENTITY
    try:
        with act_routing.operation():
            tier, args = act_routing.choose(argv)
            if not args:
                raise actmod.ActError("an agent-browser command is required, e.g. `web act open <url>`", 2)
            previous = act_routing.saved().get("tier")
            if tier == "anon":
                lane = "agent-browser-anon/-"
                rc = anon.run(args, output_hidden=counts)
            else:
                actmod.select(tier.removeprefix("identity:"))
                if previous and previous != tier and args[0] == "open":
                    print(f"web act: switching to identity {actmod.IDENTITY}", file=sys.stderr)
                rc = actmod.run(args, output_hidden=counts)
            if args[0] in ("release", "close"):
                act_routing.path().unlink(missing_ok=True)
            else:
                act_routing.remember(tier)
    except actmod.ActError as e:
        hidden = _diagnostic(f"web act: {e}")
        _emit_footer(start, lane, hidden=hidden)
        return e.code
    hidden = sum(counts) if counts or args[0] in actmod.READ_CMDS else None
    _emit_footer(start, lane, hidden=hidden)
    return 0 if rc == 0 else 5


def _bg_report(kind: str, cmd: str, args, start: float, lane: str) -> int:
    """`--status` / `--wait`: the latest background run's word on stdout (exit: bg.exit_code)."""
    word, st = bg.wait(kind, args.wait) if args.wait is not None else bg.status(kind)
    print(word)
    if st is None:
        print(f"web {cmd}: nothing has run in the background yet", file=sys.stderr)
    else:
        print(f"web {cmd}: {word} (pid {st.get('pid')}, log {st.get('log')})", file=sys.stderr)
        if word == "running" and isinstance(st.get("link"), str):
            print(st["link"], file=sys.stderr)
        elif word == "running":
            print(f"web {cmd}: no link yet (still starting); check again in a few seconds", file=sys.stderr)
    _emit_footer(start, lane)
    return bg.exit_code(word, st)


CANCEL_WORDS = {"none": (0, "nothing is running"), "cancelled": (0, "stopped pid {pid}; its link is down"),
                "refused": (2, "pid {pid} doesn't look like a web {cmd} run; not signalled"),
                "still-running": (5, "pid {pid} is still running {sec:g}s after SIGTERM")}


def _bg_cancel(kind: str, cmd: str, start: float, lane: str) -> int:
    """`--cancel`: SIGTERM to the live run (bg.cancel) and wait for it to finish its teardown."""
    word, pid = bg.cancel(kind)
    code, msg = CANCEL_WORDS[word]
    print(word)
    print(f"web {cmd} --cancel: {msg.format(pid=pid, cmd=cmd, sec=bg.CANCEL_WAIT)}", file=sys.stderr)
    _emit_footer(start, lane)
    return code


def _in_child(name: str, lane: str, run, detail: bool = True) -> tuple[str, int]:
    """A background child's command: run(start) -> (word, code) under the same exit-code wrapper."""
    t, out = time.monotonic(), {}

    def fn():
        out["word"], code = run(t)
        return code
    code = _guarded(name, lane, t, fn, detail=detail)
    return out.get("word", "error"), code


def _handoff_run(args, start: float) -> tuple[str, int]:
    try:
        result = handoffmod.handoff(args.reason, until_url=args.until_url, until_selector=args.until_selector,
                                    timeout=args.timeout)
    except handoffmod.HandoffError as e:
        print(f"web handoff: {e}", file=sys.stderr)
        _emit_footer(start, LANE_HANDOFF)
        return "error", e.code
    print(result)
    _emit_footer(start, LANE_HANDOFF)
    return result, 0 if result == "cleared" else 4


def _handoff(args, start: float) -> int:
    if args.cancel:
        return _bg_cancel("handoff", "handoff", start, LANE_HANDOFF)
    if args.status or args.wait is not None:
        return _bg_report("handoff", "handoff", args, start, LANE_HANDOFF)
    if not args.reason:
        print('web handoff: a reason is required, e.g. web handoff "tap the CAPTCHA" --until-url "https://site/*" '
              "--background", file=sys.stderr)
        _emit_footer(start, LANE_HANDOFF)
        return 2
    if args.background:
        code = bg.spawn("handoff", lambda: _in_child("handoff", LANE_HANDOFF, lambda t: _handoff_run(args, t)))
        _emit_footer(start, LANE_HANDOFF)
        return code
    return _handoff_run(args, start)[1]


def _courier(name: str, lane: str, start: float, fn) -> int:
    """One courier client call: CredError → its message and exit code (surprises and signals: _guarded)."""
    try:
        code = fn()
    except credmod.CredError as e:
        print(f"web {name}: {e}", file=sys.stderr)
        code = e.code
    _emit_footer(start, lane)
    return code


def _login_lane(args) -> str:
    return LANE_IDENTITY if args.google else "courier/-"


def _login(args, start: float) -> int:
    def go():
        word, code = credmod.login(args.origin, user_sel=args.user_sel, pass_sel=args.pass_sel, google=args.google,
                                   cred_id=args.id, username=args.username, submit=not args.no_submit)
        print(word)
        return code
    return _courier("login", _login_lane(args), start, go)


GEN_ERRORS = {"bad-origin": "not an http(s) origin", "bad-username": "a username is required (printable, <= 256)",
              "bad-length": "--length must be 12..128"}


def _cred_lane(args) -> str:
    return LANE_IDENTITY if args.cred_command == "sync-google" else "courier/-"


def _intake_run(start: float) -> tuple[str, int]:
    try:
        word, code = credmod.intake()
    except credmod.CredError as e:
        print(f"web cred: {e}", file=sys.stderr)
        _emit_footer(start, "courier/-")
        return "error", e.code
    print(word)
    _emit_footer(start, "courier/-")
    return word, code


def _cred(args, start: float) -> int:
    if args.cred_command == "intake" and args.cancel:
        return _bg_cancel("intake", "cred intake", start, "courier/-")
    if args.cred_command == "intake" and (args.status or args.wait is not None):
        return _bg_report("intake", "cred intake", args, start, "courier/-")
    if args.cred_command == "intake" and args.background:
        code = bg.spawn("intake", lambda: _in_child("cred", "courier/-", _intake_run, detail=False))
        _emit_footer(start, "courier/-")
        return code

    def go():
        cmd = args.cred_command
        if cmd is None:
            raise credmod.CredError("a subcommand is required: list, gen, rm, intake, audit, sync-google", 2)
        if cmd == "sync-google":
            word, code = credmod.sync_google()
            print(word)
            return code
        if cmd == "list":
            items = credmod.call({"op": "list"}).get("items", [])
            output_scan.scan(items)
            print(json.dumps(items))
            return 0
        if cmd == "gen":
            r = credmod.call({"op": "gen", "origin": args.origin, "username": args.username, "length": args.length})
            if r.get("result") == "ok":
                print(r["id"])
                return 0
            if r.get("result") == "exists":
                raise credmod.CredError("that origin + username already has a credential (`web cred rm <id>` first)")
            raise credmod.CredError(GEN_ERRORS.get(r.get("why"), "the courier refused it"),
                                    2 if r.get("why") in GEN_ERRORS else 5)
        if cmd == "rm":
            word = credmod.call({"op": "rm", "id": args.id}).get("result")
            print(word if word in ("removed", "no-credential") else "error")
            return 0 if word == "removed" else 5
        if cmd == "audit":
            r = credmod.call({"op": "audit", "n": args.n})
            if "items" not in r:
                raise credmod.CredError("--n must be 1..1000", 2)
            for item in r["items"]:
                output_scan.scan(item)
                print(json.dumps(item))
            return 0
        word, code = credmod.intake()
        print(word)
        return code
    return _courier("cred", _cred_lane(args), start, go)


def _taint_command(args, start: float) -> int:
    try:
        if args.command == "untaint":
            taint.untaint(args.prefix, all_sessions=args.all_sessions)
        elif args.exempt_command == "add":
            print(taint.exempt_add(args.file))
        elif args.exempt_command == "list":
            for path, digest in sorted(taint.exempt_list().items()):
                print(f"{digest} {path}")
        else:
            removed = taint.exempt_rm(args.file)
            print(f"web exempt: removed {int(removed)} file(s)")
    except ValueError as e:
        print(f"web {args.command}: {e}", file=sys.stderr)
        _emit_footer(start)
        return 2
    _emit_footer(start)
    return 0


def _status_cmd(args, start: float) -> int:
    if args.line:
        print(_status_line())
    else:
        status = _status()
        output_scan.scan(status)
        if args.json:
            print(json.dumps(status))
        else:
            for k, v in status.items():
                print(f"{k}: {v}")
    _emit_footer(start)
    return 0


def _echo(args, start: float) -> int:
    for a in args.rest:
        print(a)
    _emit_footer(start)
    return 0


# subcommand -> (handler, lane for a surprise/signal footer, whether a surprise's message may be shown)
HANDLERS = {
    "read": (_read, lambda a: "-/-", True),
    "crawl": (_crawl, lambda a: "crawl/-", True),
    "extract": (_extract, lambda a: "-/-", True),
    "gh": (_gh, lambda a: "gh/-", True),
    "search": (_search, lambda a: "-/-", True),
    "handoff": (_handoff, lambda a: LANE_HANDOFF, True),
    "login": (_login, _login_lane, False),
    "cred": (_cred, _cred_lane, False),
    "status": (_status_cmd, lambda a: "-/-", True),
    "_echo-args": (_echo, lambda a: "-/-", True),
}


EXIT_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


def _exit_on_signal(signum, frame):
    # Ignore further signals first, so a second one can't interrupt the cleanup this exit triggers.
    for sig in EXIT_SIGNALS:
        signal.signal(sig, signal.SIG_IGN)
    raise SystemExit(5)


@contextlib.contextmanager
def _signals_exit():
    """SIGTERM/SIGHUP/SIGINT (a caller killing web, an ssh hangup, Ctrl-C) raise SystemExit(5) instead of killing
    the process or a KeyboardInterrupt traceback (exit 130), so finally/ExitStack cleanup (engine scopes, the private
    Xvfb, a handoff's tailnet path) runs and _guarded prints "interrupted" + the footer. A SIGINT the process started
    with ignored (a shell's background job) stays ignored. The previous handlers are restored."""
    saved = {}
    with contextlib.suppress(ValueError):   # not the main thread: leave signal handling alone
        for sig in EXIT_SIGNALS:
            if sig == signal.SIGINT and signal.getsignal(sig) is signal.SIG_IGN:
                continue
            saved[sig] = signal.signal(sig, _exit_on_signal)
    try:
        yield
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def self_taint(stdout: str, stderr: str, *, session_id: str, source_rules=()) -> bool:
    """Withhold captured Codex output with fixed rules and no page excerpts. The text never reaches the
    agent, so the session is not flagged (guarded mode, 2026-10-21); session_id stays for the call sites."""
    try:
        rules = sorted(set(source_rules) | {hit.rule for text in (stdout, stderr)
                        for hit in tripwire.scan(text, profile="session")})
    except Exception:
        rules = ["SCANNER"]
    if not rules:
        return False
    print(f"web: stopped, page text addressed to AI agents (rules {','.join(rules)})", file=sys.stderr)
    return True


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    with _signals_exit():
        session_id = os.environ.get("CODEX_SESSION_ID")
        if not session_id:
            return _main(argv)
        # Codex streams tool output before post hooks, including failed commands.
        stdout, stderr = io.StringIO(), io.StringIO()
        interrupted = None
        source_rules = set()
        token = output_scan.rules.set(source_rules)
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    code = _main(argv)
                except SystemExit as error:
                    interrupted = error
        finally:
            output_scan.rules.reset(token)
        if self_taint(stdout.getvalue(), stderr.getvalue(), session_id=session_id, source_rules=source_rules):
            return 6
        try:
            sys.stdout.write(stdout.getvalue())
            sys.stdout.flush()
        except BrokenPipeError:
            with contextlib.suppress(OSError, ValueError):
                os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.stderr.write(stderr.getvalue())
        sys.stderr.flush()
        if interrupted is not None:
            raise interrupted
        return code


def _main(argv: list[str]) -> int:
    start = time.monotonic()
    if argv[:1] == ["_anon"]:
        from . import anon
        return anon.main(argv[1:])
    actmod.caller()   # computed before any fork; children inherit the owner

    # Recovery belongs to this machine, even when a remote host was requested.
    local_argv = host._pop_host(argv)[1]
    if local_argv[:1] and local_argv[0] in ("untaint", "exempt"):
        parser = build_parser()
        try:
            args = parser.parse_args(argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 2
            if code:
                _emit_footer(start)
            return code
        return _guarded(args.command, "-/-", start, lambda: _taint_command(args, start), detail=False)

    session_id = os.environ.get("CODEX_SESSION_ID")
    if (session_id and local_argv[:1] and local_argv[0] in ("read", "crawl", "extract", "search")
            and any(arg.startswith("--") and len(arg) > 2 and "--allow-private".startswith(arg)
                    for arg in local_argv[1:])):  # Refuse bypass prefixes early, even though argparse rejects them.
        try:
            private_tainted = bool(taint.is_tainted("codex", session_id))
        except ValueError:
            private_tainted = True
        if private_tainted:
            print("web: allow-private denied (tainted)", file=sys.stderr)
            _emit_footer(start)
            return 5

    if local_argv[:1] and local_argv[0] in ("read", "crawl", "extract"):   # before any forward to thor
        from .act_routing import VPN_ONLY, vpn_only_url
        hit = next((a for a in local_argv[1:] if not a.startswith("-") and vpn_only_url(a)), None)
        if hit:
            print("web: " + VPN_ONLY.format(urlsplit(hit if "://" in hit else "//" + hit).hostname), file=sys.stderr)
            _emit_footer(start)
            return 2

    argv = host.prefer_thor(argv)
    try:
        target = host._pop_host(argv)[0] or host.HOME
        argv = host.ensure_host(argv)
    except host.Forwarded as e:
        return e.code
    except SystemExit as e:
        _emit_footer(start)
        return e.code if isinstance(e.code, int) else 5

    if argv and argv[0] == "act":   # a pass-through: agent-browser's own options must reach it untouched
        return _guarded("act", LANE_IDENTITY, start, lambda: _act(argv[1:], start))

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 2
        if code != 0:  # argparse's usage/error exits get a footer; --help (code 0) doesn't
            _emit_footer(start)
        return code

    if args.command is None:
        parser.print_help()
        return 0

    if args.command in HANDLERS:
        handler, lane, detail = HANDLERS[args.command]
        def run_command():
            if args.command in ("login", "handoff") or (args.command == "cred" and args.cred_command == "sync-google"):
                actmod.select(args.identity if args.identity is not None else os.environ.get("WEB_AS", "personal"))
                if args.command == "cred" and actmod.IDENTITY != "personal":
                    raise actmod.ActError("sync-google only supports --as personal", code=2)
            elif args.command == "status":
                actmod.select("personal")
            with netguard.command_bypass(getattr(args, "allow_private", False)):
                if args.command in ("read", "crawl"):
                    if args.command == "crawl":
                        out = (Path(args.out).expanduser() if args.out else
                               Path.cwd() / f"crawl-{urlsplit(args.url).hostname or 'unknown'}").resolve()
                        try:
                            out.mkdir(parents=True, exist_ok=True)
                        except OSError:
                            print(f"web crawl: cannot create output directory: {out}", file=sys.stderr)
                            raise netguard.BoundaryError from None
                        args.out = str(out)
                        jail_argv = list(argv)
                        index = jail_argv.index("--") if "--" in jail_argv else len(jail_argv)
                        jail_argv[index:index] = ["--out", str(out)]
                        netguard.reexec(["--host", target, *jail_argv], binds=(out,))
                    else:
                        netguard.reexec(["--host", target, *argv])
                return handler(args, start)
        return _guarded(args.command, lane(args), start, run_command, detail=detail)

    print("not built yet", file=sys.stderr)
    _emit_footer(start)
    return 5
