"""Router client for the free LLM extract lane: a minimal, standalone copy of lane-evals/lib.py's
`router_key()`/`post()` (not imported from there - `web` ships its own copy so it has no dependency on that
project). The key is fetched once over ssh from thor's admin API, held in memory only, and never printed,
logged, or placed on argv (the ssh command builds the curl call on the remote side). Every error string raised
from here is redacted against the key first: an auth-rejection response can echo the Authorization header back,
and that must never reach a caller's stderr."""
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request

KIT_HOSTS = os.environ.get("KIT_HOSTS_ENV") or os.path.expanduser("~/.config/ai-workflow-kit/hosts.env")


def hosts_env(key: str) -> str:
    """KEY from the environment, else from the kit's hosts.env (last KEY=value wins); "" when missing."""
    if os.environ.get(key):
        return os.environ[key]
    try:
        with open(KIT_HOSTS) as f:
            vals = [v.strip().strip("'\"") for k, _, v in (line.partition("=") for line in f.read().splitlines())
                    if k.strip() == key]
    except OSError:
        return ""
    return vals[-1] if vals else ""


# the router's OpenAI-compatible /v1 base: FREELLMAPI_URL from the environment or hosts.env
ROUTER = hosts_env("FREELLMAPI_URL").rstrip("/")
_KEY = None
_KEY_LOCK = threading.Lock()


class LlmError(Exception):
    """The router request failed: network error, timeout, or a non-2xx response. The message is always
    redacted against the router key."""


def _redact(text: str, key: str | None) -> str:
    return text.replace(key, "<redacted>") if key else text


def _router_key() -> str:
    global _KEY
    if not ROUTER:
        raise LlmError(f"no router: set FREELLMAPI_URL (its /v1 base URL) in {KIT_HOSTS}")
    with _KEY_LOCK:
        if _KEY is not None:
            return _KEY
        cmd = ('cd ~/freellmapi-docker && curl -s -H "Authorization: Bearer $(cat .admin-session)" '
               'http://127.0.0.1:3001/api/settings/api-key')
        try:
            out = subprocess.run(["ssh", "thor", cmd], capture_output=True, text=True, timeout=30)
        except Exception as e:  # noqa: BLE001 - missing ssh, timeout: all one failure mode
            raise LlmError(f"could not fetch the router key from thor: {type(e).__name__}") from None
        if out.returncode != 0:
            raise LlmError(f"could not fetch the router key from thor: ssh exited {out.returncode}") from None
        try:
            key = json.loads(out.stdout)["apiKey"]
        except (ValueError, KeyError, TypeError):
            raise LlmError("could not fetch the router key from thor: unexpected response shape") from None
        if not isinstance(key, str) or not key:
            raise LlmError("could not fetch the router key from thor: empty key") from None
        _KEY = key
    return _KEY


def chat(messages: list[dict], model: str = "auto:extract", max_tokens: int = 2000) -> str:
    """One chat completion against the router. Returns the assistant message's content (never the key).
    Raises LlmError on any failure; the message is always redacted against the key."""
    key = _router_key()
    body = {"model": model, "messages": messages, "max_tokens": max_tokens}
    req = urllib.request.Request(ROUTER + "/chat/completions", json.dumps(body).encode(),
                                  {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise LlmError(_redact(f"http {e.code}: {e.read()[:300].decode('utf-8', 'replace')}", key)) from e
    except Exception as e:  # noqa: BLE001 - network errors, timeouts, bad JSON: all one failure mode
        raise LlmError(_redact(repr(e)[:300], key)) from e
    message = (data.get("choices") or [{}])[0].get("message") or {}
    return message.get("content") or ""
