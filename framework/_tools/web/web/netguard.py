"""Public-network fetch boundary backed by the shared agent jail."""
import contextlib
import contextvars
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys
from urllib.parse import urlsplit

_BYPASS = contextvars.ContextVar("web_allow_private", default=False)
_LOADED = None
FETCH_LIMIT = 1_048_576
FETCH_TIMEOUT = 180


class BoundaryError(RuntimeError):
    """Fixed, already logged failure at the network boundary."""


def _unavailable(which):
    print(f"web: network jail unavailable ({which})", file=sys.stderr)
    raise BoundaryError


def _jail():
    global _LOADED
    directory = Path(os.environ.get("WEB_AGENT_JAIL_DIR", Path(__file__).resolve().parents[2] / "agent-jail"))
    path = directory / "agent_jail.py"
    if not path.is_file():
        _unavailable("agent_jail")
    if _LOADED is not None and _LOADED[0] == path:
        sys.modules["agent_jail"] = _LOADED[1]  # the probe imports it by name
        return _LOADED[1]
    try:
        spec = importlib.util.spec_from_file_location("agent_jail", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        for name in ("netjail_argv", "run", "JailUnavailable"):
            if not hasattr(module, name):
                raise ImportError
    except Exception:  # An unusable shared module must fail without its details.
        sys.modules.pop("agent_jail", None)
        _unavailable("agent_jail")
    _LOADED = path, module
    return module


def refusal(url: str) -> str | None:
    """Validate without resolving a user hostname in the trusted parent.

    Destination filtering, including redirects and changing DNS answers, is done
    by nft inside the jail. Its private loopback remains usable for CDP/fixtures.
    """
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return "not an http(s) URL"
        parts.port
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError:
            return None
        if address.version == 6:
            return "IPv6 unavailable in network jail"
    except ValueError:
        return "invalid URL"
    return None


@contextlib.contextmanager
def command_bypass(allow: bool):
    """One parsed command only; never exported to subsequent commands."""
    if allow:
        # web.taint does not exist yet on this branch. Task 5 supplies it.
        if os.environ.get("WEB_TAINTED") == "1":
            print("web: allow-private denied (tainted)", file=sys.stderr)
            raise BoundaryError
        print("web: allow-private bypass", file=sys.stderr)
    token = _BYPASS.set(allow)
    try:
        yield
    finally:
        _BYPASS.reset(token)


def _argv(argv, *, binds=()):
    jail = _jail()
    try:
        cmd = ["env", "WEB_NETJAIL=1", sys.executable, "-m", "web", *argv]
        return jail.netjail_argv(cmd, binds=binds) if binds else jail.netjail_argv(cmd)
    except jail.JailUnavailable as error:
        _unavailable(str(error))


def reexec(argv: list[str], *, binds=()) -> None:
    """After host selection, enter the network boundary exactly once."""
    if os.environ.get("WEB_NETJAIL") == "1" or _BYPASS.get():
        return
    wrapped = _argv(argv, binds=binds)
    try:
        os.execvp(wrapped[0], wrapped)
    except OSError:
        _unavailable(wrapped[0])


def fetch_read(url: str, lane: str = "auto", budget: float | None = 110):
    """Capture only a jailed fetch; the caller's trusted model/backend stays out."""
    from . import read as reader
    if _BYPASS.get() or os.environ.get("WEB_NETJAIL") == "1":
        return reader.read(url, lane=lane, budget=budget)
    if reason := refusal(url):
        raise reader.BadUrl([reason])
    argv = _argv(["--host", socket.gethostname().lower(), "read", url, "--json", "--include-html",
                  "--lane", lane, "--budget", str(budget or 0)])
    jail = _jail()
    try:
        result = jail.run(argv, b"", timeout=FETCH_TIMEOUT, limit=FETCH_LIMIT)
    except OSError:
        raise reader.JailFetchError(["jailed fetch launch failed"]) from None
    if result.code == 3 and not result.truncated:
        raise reader.Blocked(["jailed fetch blocked"])
    if result.truncated or result.code:
        raise reader.JailFetchError(["jailed fetch overflow" if result.truncated else "jailed fetch failed"])
    try:
        data = json.loads(result.stdout)
        fields = reader.ReadResult.__dataclass_fields__
        data = {k: data[k] for k in fields}
        if not isinstance(data["markdown"], str) or not isinstance(data["html"], str):
            raise ValueError
        data["notes"] = tuple(data["notes"])
        return reader.ReadResult(**data)
    except (ValueError, KeyError, TypeError):
        raise reader.JailFetchError(["jailed fetch invalid output"]) from None
