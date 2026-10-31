"""Host guard: web runs on odin. Anywhere else (raven, and thor too) the call is forwarded to odin over ssh.

`--host thor` (before the subcommand) is the user's explicit opt-in to thor: there the read lanes (read, crawl,
search, extract, gh) run locally; act/login/handoff/cred refuse with exit 2, because the identity browser and the
credential courier live on odin only. From any other host `--host thor` forwards to thor with the flag kept.

ssh runs with BatchMode=yes: a forward that would ask for a password or a host-key confirmation fails at once
instead of hanging an agent's tool call. WEB_FORWARDED=1 is set on the remote process (via `env`, not SendEnv,
which needs server-side AcceptEnv config we don't control) so a second hop that still isn't on the target host fails
loudly instead of forwarding forever.
"""
import os
import shlex
import shutil
import socket
import subprocess
import sys

HOSTS = ("odin", "thor")
HOME = "odin"
ODIN_ONLY = {"act", "login", "handoff", "cred"}
SCRAPE = {"read", "crawl", "search", "extract"}   # default to thor (the GPU box), keeping load off the laptop


class Forwarded(SystemExit):
    """A captured remote command already supplied its own footer."""


def _pop_host(argv: list[str]) -> tuple[str | None, list[str]]:
    """A leading `--host NAME` / `--host=NAME` and the argv without it."""
    if argv[:1] == ["--host"] and len(argv) >= 2:
        return argv[1].lower(), argv[2:]
    if argv and argv[0].startswith("--host="):
        return argv[0].split("=", 1)[1].lower(), argv[1:]
    return None, argv


def prefer_thor(argv: list[str]) -> list[str]:
    """Scrape commands with no --host run on thor (woken with thor-up first). If thor can't be reached they stay on
    odin with a note. WEB_SCRAPE_HOST=odin opts out; forwarded and jailed hops already carry --host."""
    if (_pop_host(argv)[0] or not argv or argv[0] not in SCRAPE or os.environ.get("WEB_FORWARDED")
            or os.environ.get("WEB_NETJAIL") == "1" or os.environ.get("WEB_SCRAPE_HOST", "thor") != "thor"):
        return argv
    if not wake_thor():
        print("web: thor unreachable, scraping on odin", file=sys.stderr)
        return argv
    return ["--host", "thor", *argv]


def wake_thor() -> bool:
    """Shared wake/probe for disposable reads and interactive browsers."""
    if socket.gethostname().lower() == "thor":
        return True
    wake = shutil.which("thor-up")
    probe = [wake] if wake else ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "thor", "true"]
    try:
        return subprocess.run(probe, capture_output=True, timeout=90).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_host(argv: list[str]) -> list[str]:
    """The argv to run here (without --host), or forward to the target host (os.execvp, no return), or exit:
    2 for a bad --host or an odin-only command on thor, 5 for a forwarding loop or a missing ssh."""
    want, rest = _pop_host(argv)
    if want is not None and want not in HOSTS:
        print(f"web: --host must be one of {', '.join(HOSTS)} (got {want!r})", file=sys.stderr)
        sys.exit(2)
    target = want or HOME
    hostname = socket.gethostname().lower()
    if os.environ.get("WEB_NETJAIL") == "1":
        # pasta gives its child a private hostname prefixed with pasta-.
        hostname = hostname.removeprefix("pasta-")
    if hostname == target:
        if target != HOME and rest[:1] and rest[0] in ODIN_ONLY:
            print(f"web: `{rest[0]}` is odin only (the identity browser and the credential courier live there); "
                  f"drop --host {target}", file=sys.stderr)
            sys.exit(2)
        return rest
    if os.environ.get("WEB_FORWARDED"):
        print(f"web: already forwarded once and host is still {hostname!r} (not {target}) — refusing to forward again",
              file=sys.stderr)
        sys.exit(5)
    remote = ["--host", target, *rest] if target != HOME else rest
    command = ["ssh", "-o", "BatchMode=yes", target, "--", "env", "WEB_FORWARDED=1",
               *(["WEB_TAINTED=1"] if os.environ.get("WEB_TAINTED") == "1" else []),
               *([shlex.quote(f"WEB_AS={os.environ['WEB_AS']}")] if "WEB_AS" in os.environ else []), "web",
               *map(shlex.quote, remote)]
    try:
        if os.environ.get("CODEX_SESSION_ID"):
            # Keep the calling CLI alive to scan both remote streams before emission.
            result = subprocess.run(command, capture_output=True)
            from . import output_scan
            stdout = result.stdout.decode("utf-8", errors="surrogateescape")
            stderr = result.stderr.decode("utf-8", errors="surrogateescape")
            output_scan.serialized(stdout)
            output_scan.serialized(stderr)
            sys.stdout.write(stdout)
            sys.stderr.write(stderr)
            raise Forwarded(result.returncode)
        os.execvp("ssh", command)
    except FileNotFoundError:
        print(f"web: ssh not found on PATH — cannot forward to {target}", file=sys.stderr)
        sys.exit(5)
    return rest   # only reached when execvp is stubbed (tests)
