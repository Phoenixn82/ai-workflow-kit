"""OpenAI-compatible router client with short-lived read caches."""

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

KIT_HOSTS = os.environ.get("KIT_HOSTS_ENV") or os.path.expanduser("~/.config/ai-workflow-kit/hosts.env")


def hosts_env(key):
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


def router_base():
    """FreeLLMAPI's root URL: FREELLMAPI_URL (its /v1 base, from the environment or hosts.env) without the /v1."""
    url = hosts_env("FREELLMAPI_URL").rstrip("/")
    return url[:-3] if url.endswith("/v1") else url


BASE = router_base()
V1 = BASE + "/v1"
NO_ROUTER = f"lane: no router: set FREELLMAPI_URL (its /v1 base URL) in {KIT_HOSTS}"
_lock = threading.Lock()
_key = None
_admin = None


class Router:
    def __init__(self, transport=None):
        self.transport = transport
        self.cache = {}
        self.BASE = BASE

    def admin_token(self):
        global _admin
        if not BASE:  # every real router call fetches a token first: stop here with what to set
            raise SystemExit(NO_ROUTER)
        with _lock:
            if _admin is None:
                _admin = subprocess.run(
                    ["ssh", "thor", "cat ~/freellmapi-docker/.admin-session"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                ).stdout.strip()
            return _admin

    def key(self):
        global _key
        if not BASE:
            raise SystemExit(NO_ROUTER)
        with _lock:
            if _key is None:
                # double quotes: the remote shell must expand $(cat .admin-session)
                cmd = 'cd ~/freellmapi-docker && curl -s -H "Authorization: Bearer $(cat .admin-session)" http://127.0.0.1:3001/api/settings/api-key'
                out = subprocess.run(
                    ["ssh", "thor", cmd],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                ).stdout
                _key = json.loads(out)["apiKey"]
            return _key

    def _get(self, path):
        if self.transport:
            return self.transport("GET", path, None)
        request = urllib.request.Request(
            BASE + path, headers={"Authorization": "Bearer " + self.admin_token()}
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    def cached(self, name, function):
        if name not in self.cache or time.time() - self.cache[name][0] > 300:
            self.cache[name] = (time.time(), function())
        return self.cache[name][1]

    def catalog(self):
        return self.cached("catalog", lambda: self._get("/api/models"))

    def profiles(self):
        return self.cached("profiles", lambda: self._get("/api/profiles"))

    def profile_models(self, profile_id):
        return self.cached(
            f"profile:{profile_id}",
            lambda: self._get(f"/api/profiles/{profile_id}/models"),
        )

    def chat(self, model, messages, tools=None, max_tokens=4096, response_format=None):
        body = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if model.startswith("auto:local") and os.environ.get("LANE_LOCAL_JSON") == "1":
            body.update(response_format={"type": "json_object"}, reasoning_effort="none", temperature=0)
        if response_format is not None:
            body['response_format'] = response_format
        if tools:
            body["tools"] = tools
        request = urllib.request.Request(
            V1 + "/chat/completions",
            json.dumps(body).encode(),
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.key(),
            },
        )
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                data = json.load(response)
                message = data["choices"][0]["message"]
                return {
                    "content": message.get("content", ""),
                    "tool_calls": message.get("tool_calls"),
                    "usage": data.get("usage"),
                    "routed": response.headers.get("X-Routed-Via"),
                    "latency": time.time() - started,
                    "status": response.status,
                }
        except urllib.error.HTTPError as error:
            return {
                "error": error.read().decode("utf-8", "replace")[:400],
                "routed": error.headers.get("X-Routed-Via"),
                "latency": time.time() - started,
                "status": error.code,
            }
        except (OSError, ValueError, KeyError) as error:
            return {
                "error": str(error)[:300],
                "routed": None,
                "latency": time.time() - started,
                "status": None,
            }
