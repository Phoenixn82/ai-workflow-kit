"""Caller-local interactive tier selection. Browser commands stay on Odin's output boundary."""
import json
import os
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

from . import act, identities
from .config import LANES


def identity_url(url: str) -> bool:
    host = (urlsplit(url if "://" in url else "//" + url).hostname or "").rstrip(".")
    host = host.encode("idna").decode().lower()
    # Config contains registrable roots; suffix boundaries also cover all their product hosts.
    return any(host == domain or host.endswith("." + domain) for domain in LANES["act"]["identity_domains"])


def vpn_only_url(url: str) -> bool:
    """Sites the user allows only through a VPN exit: never their accounts, never their home IP."""
    try:
        host = (urlsplit(url if "://" in url else "//" + url).hostname or "").rstrip(".").lower()
    except ValueError:   # not a URL at all (an inline JSON schema, say)
        return False
    return any(host == domain or host.endswith("." + domain) for domain in LANES["act"]["vpn_only_domains"])


VPN_ONLY = ("{} is VPN-only: never the user's accounts or home IP, and this lane exits from home. "
            "Use a VPN-routed lane (social-intel) instead")


def path():
    return act.STATE_DIR / "act-routes" / f"{identities.caller_key()}.json"


def saved() -> dict:
    try:
        data = json.loads(path().read_text())
        now = time.time()
        # Browser idle expiry must never send an anonymous caller into a signed-in window.
        if data["owner"] == act.caller() and identities._live({**data, "ts": now}, now):
            return data
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {}


def choose(argv: list[str]) -> tuple[str, list[str]]:
    name, anon, explicit = os.environ.get("WEB_AS"), False, False
    while argv and (argv[0] in ("--as", "--anon") or argv[0].startswith("--as=")):
        if argv[0] == "--anon":
            anon, argv = True, argv[1:]
        elif argv[0] == "--as":
            if len(argv) < 2:
                raise act.ActError("--as requires an identity name", code=2)
            name, argv, explicit = argv[1], argv[2:], True
        else:
            name, argv, explicit = argv[0].split("=", 1)[1], argv[1:], True
    if anon and explicit:
        raise act.ActError("--anon cannot be combined with --as", code=2)
    if len(argv) >= 2 and argv[0] == "open" and vpn_only_url(argv[1]):
        raise act.ActError(VPN_ONLY.format(urlsplit(argv[1] if "://" in argv[1] else "//" + argv[1]).hostname), code=2)
    if anon:
        return "anon", argv
    if name is not None:
        act.select(name)  # validate before persisting anything
        return "identity:" + name, argv
    if act.caller() in ("terminal", "forwarded"):
        return "identity:personal", argv
    if len(argv) >= 2 and argv[0] == "open":
        return ("identity:personal" if identity_url(argv[1]) else "anon"), argv
    return saved().get("tier", "identity:personal"), argv


@contextmanager
def operation():
    with act._lock(f"act-route-{identities.caller_key()}.lock"):
        yield


def remember(tier: str):
    path().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    act._write_private(path(), json.dumps({"owner": act.caller(), "tier": tier, "ts": time.time()}))
