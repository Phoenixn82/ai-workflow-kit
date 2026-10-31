"""Pre-transmission redactor.

The existing ~/.claude/hooks/secret-firewall/detect-secrets.py is a PostToolUse
tripwire and cannot rewrite output - its own docstring says so. It contributes
PATTERNS here and nothing else; this module is the thing that actually
transforms content before it reaches thor or the queue (PLAN.md D).
"""
import importlib.util
import re
from pathlib import Path

_TRIPWIRE = Path.home() / ".claude" / "hooks" / "secret-firewall" / "detect-secrets.py"

PLACEHOLDER = "[REDACTED:{kind}]"


def _load_shared_patterns() -> list[re.Pattern]:
    """Import PATTERNS from the tripwire so there is one source of truth."""
    try:
        spec = importlib.util.spec_from_file_location("_secret_tripwire", _TRIPWIRE)
        if spec is None or spec.loader is None:
            return []
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return list(getattr(mod, "PATTERNS", []))
    except Exception:
        return []


# Assignment-shaped secrets the token-prefix patterns cannot catch. Each entry
# is (pattern, kind); the pattern keeps everything that is safe to show in
# group 1 (and group 3 where a trailing context matters) and puts only the
# secret value in group 2.
_LOCAL = [
    # key = value  /  key: value   -> keep the key and its separator
    # (no \b: `_` is a word char, so POSTGRES_PASSWORD= / GITHUB_TOKEN= / HF_TOKEN=
    # never matched; the optional quote covers JSON `"password": "..."`)
    (re.compile(r"(?i)(?<![A-Za-z0-9])([A-Za-z0-9_]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)"
                r"[A-Za-z0-9_]*['\"]?\s*[:=]\s*['\"]?)([^\s'\"]{8,})"), "assignment"),
    # CLI flags: a token or password flag, then its value after a space or "="
    (re.compile(r"(?i)(--(?:token|password|passwd|secret|api[_-]?key|access[_-]?key|auth)(?:=|\s+)['\"]?)"
                r"([^\s'\"]{8,})"), "flag"),
    # Authorization: Bearer xxx    -> keep the scheme
    (re.compile(r"(?i)\b(authorization\s*:\s*(?:bearer|basic)\s+)(\S{12,})"), "authz-header"),
    # scheme://user:pass@host      -> keep scheme, user and host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@]+:)([^\s/@]{4,})(@)"), "url-credentials"),
]

# Multi-line blocks must run BEFORE the prefix patterns: the shared list matches
# the BEGIN marker alone and would destroy the anchor this needs, leaving the key
# body behind.
_BLOCKS = [
    (re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----)(.*?)(-----END [A-Z ]*PRIVATE KEY-----)",
                re.S), "private-key-block"),
]

_SHARED = _load_shared_patterns()


def redact(text: str) -> tuple[str, int]:
    """Return (redacted_text, number_of_redactions).

    Fails safe: a pattern that errors is skipped rather than aborting the run,
    but the shared-pattern list being empty is reported by `self_check`.
    """
    count = 0

    def _sub_groups(m, kind):
        tail = m.group(3) if m.re.groups >= 3 else ""
        return f"{m.group(1)}{PLACEHOLDER.format(kind=kind)}{tail}"

    for rx, kind in _BLOCKS:
        try:
            text, n = rx.subn(lambda m, k=kind: _sub_groups(m, k), text)
            count += n
        except re.error:
            continue
    for rx in _SHARED:
        try:
            text, n = rx.subn(PLACEHOLDER.format(kind="secret"), text)
            count += n
        except re.error:
            continue
    for rx, kind in _LOCAL:
        try:
            def _sub(m, _kind=kind):
                # group 1 is safe context, group 2 is the secret, group 3 (if the
                # pattern has one) is safe trailing context.
                tail = m.group(3) if m.re.groups >= 3 else ""
                return f"{m.group(1)}{PLACEHOLDER.format(kind=_kind)}{tail}"
            text, n = rx.subn(_sub, text)
            count += n
        except re.error:
            continue
    return text, count


def self_check() -> tuple[bool, str]:
    """Refuse to run the pipeline with a broken redactor."""
    if not _SHARED:
        return False, f"shared PATTERNS unavailable from {_TRIPWIRE}"
    probe = "key = sk-ant-abcdefghijklmnopqrstuvwxyz012345"
    out, n = redact(probe)
    if n == 0 or "sk-ant-" in out:
        return False, "redactor failed its own probe"
    return True, "ok"
