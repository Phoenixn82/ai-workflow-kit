#!/usr/bin/env python3
"""Check install/tools.json against the contract in install/README.md, and that it and every compose template pin
exact versions (piped scripts: versioned URL + sha256). Stdlib only, no network, writes nothing.

    python3 install/test_manifest.py          # PASS, or every problem found and exit 1
    python3 install/test_manifest.py --plan   # also print how each tool resolves per OS
    python3 install/test_manifest.py --self-test   # broken copies of the manifest must each be caught
"""
import json
import os
import re
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
MANIFEST = KIT / "install" / "tools.json"

PROFILES = {"odin", "loki", "heimdall", "thor", "raven", "telegram"}
OSES = ("arch", "debian", "macos")
CROSS = ("mise", "uv", "npm", "docker", "claude-plugin")      # tried in this order after the OS's own key
METHOD_KEYS = set(OSES) | set(CROSS) | {"manual"}
RUNTIME = {"mise": "mise", "uv": "uv", "npm": "node", "docker": "docker", "claude-plugin": "claude"}
TOOL_KEYS = {"id", "name", "what", "profiles", "homepage", "license", "bundled", "optional", "verify", "os",
             "methods", "requires", "install", "entrypoints", "config", "script_sha256"}
REQUIRED = {"id": str, "name": str, "what": str, "profiles": list, "homepage": str, "license": str,
            "bundled": bool, "optional": bool, "verify": str, "methods": dict}
STEP_FIELDS = {  # do -> (required fields, optional fields); every step may also carry COMMON
    "mkdir": ({"path"}, set()),
    "link": ({"src", "dest"}, set()),
    "copy": ({"src", "dest"}, {"mode"}),
    "uv-tool": ({"dir"}, set()),
    "uv-sync": ({"dir"}, {"python"}),
    "uv-venv": ({"venv", "packages"}, {"python"}),
    "npm-ci": ({"dir"}, set()),
    "run": ({"cmd", "why"}, set()),
    "note": ({"text"}, set()),
}
COMMON = {"do", "harness", "os", "sudo", "why"}
HARNESS_DEST = ("~/.claude/", "~/.codex/", "~/.claude.json", "~/.gitconfig")
HARNESS_CMD = ("claude mcp add", "git config --global", "handoff.install --apply")
PIPED = re.compile(r"\|\s*(?:[A-Za-z_]\w*=\S*\s+)*(?:sudo\s+(?:-\S+\s+)*)?(?:/usr)?(?:/bin/)?(?:ba|da|z)?sh\b")
# The one piped shape the installer runs, and only after checking the script's sha256 (lib.py run_script):
#   curl [-flags] <https URL> | [VAR=value ...] sh|bash [-s] [--] [args]
PINNED_SCRIPT = re.compile(r"curl(?:\s+-[A-Za-z]+)*\s+(?P<url>https://[^\s'\"|;&]+)\s*\|\s*"
                           r"(?P<env>(?:[A-Za-z_]\w*=[\w.@:/+-]*\s+)*)(?P<sh>(?:ba)?sh)(?:\s+-s)?(?:\s+--)?"
                           r"(?P<args>(?:\s+[\w.=@:/+-]+)*)\s*")
VERSIONED_URL = re.compile(r"/(?:v?\d+(?:\.\d+)+|[0-9a-f]{40})/")    # a release, tag or commit in the path
SHA256 = re.compile(r"[0-9a-f]{64}")
EXACT_TAG = re.compile(r"v?\d+(?:\.\d+)+(?:[-+._][0-9A-Za-z.]+)*")   # 12.1, v2.30.0, 2026.09.0; never latest, 2, v3
EXACT_VER = re.compile(r"\d+(?:\.\d+)+(?:[-+.][0-9A-Za-z.]+)*")
# writes ~/.claude or global git config: allowed only in the claude-plugin method (harness-gated) or a harness step
AGENT_CFG_CMD = re.compile(r"\bclaude\s+(plugin\s+(install|marketplace\s+add)|mcp\s+add)\b|\bgit\s+config\s+--global\b")
# Debian keeps these in /usr/sbin, which is not on a normal user's PATH: `command -v` alone misses them
SBIN = {"smbd", "smartctl", "unbound", "sshd", "nft", "ufw", "iptables", "cupsd", "rrsync"}


def resolve(tool, os_name):
    """(method key, command) the installer uses on os_name; ("install", steps) for a bundled tool;
    None if the tool does not support that OS."""
    if os_name not in tool.get("os", OSES):
        return None
    if tool.get("bundled"):
        return "install", tool.get("install")
    m = tool["methods"]
    for key in (os_name, *CROSS):
        if m.get(key):
            return key, m[key]
    return "manual", m["manual"]


def repo_path(rel):
    return KIT / rel


def image_exact(img):
    """An image reference names one exact version: a digest, or a tag like 1.2 / v1.2.3 (not latest, not 2 or v3)."""
    if re.fullmatch(r"\S+@sha256:[0-9a-f]{64}", img):
        return True
    name, _, tag = img.rpartition(":")
    return bool(name) and "/" not in tag and bool(EXACT_TAG.fullmatch(tag))


def floating(cmd):
    """Package or image specs in a command that do not name one exact version."""
    out = []
    for seg in re.split(r"&&|\|\||;", cmd):
        words = [w.strip("'\"") for w in seg.split()]
        for i, w in enumerate(words):
            nxt = [x for x in words[i + 1:] if not x.startswith("-")]
            if w == "npm" and words[i + 1:i + 2] in (["install"], ["i"], ["add"]):
                out += [x for x in nxt[1:] if not re.fullmatch(r"(?:@[\w.-]+/)?[\w.-]+@" + EXACT_VER.pattern, x)]
            elif w == "uv" and words[i + 1:i + 3] == ["tool", "install"]:
                out += [x for x in nxt[2:3] if not re.fullmatch(r"[\w.\[\],-]+==" + EXACT_VER.pattern, x)]
            elif re.fullmatch(r"(?:\S*/)?pip3?(?:\.\d+)?", w) and words[i + 1:i + 2] == ["install"]:
                out += [x for x in nxt[1:] if not re.fullmatch(r"[\w.\[\],-]+==" + EXACT_VER.pattern, x)]
            elif w == "mise" and words[i + 1:i + 2] == ["use"]:
                out += [x for x in nxt[1:] if not re.fullmatch(r"[\w.-]+@" + EXACT_VER.pattern, x)]
            elif w == "docker" and words[i + 1:i + 2] == ["pull"]:
                out += [x for x in nxt[1:2] if not image_exact(x)]
    return out


def compose_images(text):
    """Every image: value of a compose file."""
    return [m.group(1).strip("'\"") for m in re.finditer(r"^\s*image:\s*(\S+)", text, re.M)]


class Checker:
    def __init__(self, data):
        self.data, self.errors = data, []

    def err(self, where, msg):
        self.errors.append(f"{where}: {msg}")

    # ------------------------------------------------------------------ steps
    def check_step(self, tid, i, step, made):
        where = f"{tid} install[{i}]"
        if not isinstance(step, dict) or step.get("do") not in STEP_FIELDS:
            return self.err(where, f"unknown step {step!r}")
        need, opt = STEP_FIELDS[step["do"]]
        for k in need - step.keys():
            self.err(where, f"missing '{k}'")
        for k in step.keys() - need - opt - COMMON:
            self.err(where, f"unknown key '{k}'")
        for k in ("harness", "sudo"):
            if k in step and not isinstance(step[k], bool):
                self.err(where, f"'{k}' must be true or false")
        if not set(step.get("os", OSES)) <= set(OSES) or not step.get("os", OSES):
            self.err(where, f"bad os list {step.get('os')}")
        if step.get("sudo") and not step.get("why"):
            self.err(where, "a sudo step must say why")
        do = step["do"]
        for k in ("src", "dir"):
            if k in step:
                rel = step[k]
                if rel.startswith(("~", "/")) or ".." in Path(rel).parts:
                    self.err(where, f"{k} must be a path inside the kit: {rel}")
                elif not repo_path(rel).exists() and not any(rel.startswith(p) for p in made):
                    self.err(where, f"{k} does not exist in the kit: {rel}")
        for k in ("dest", "path", "venv"):
            if k in step:
                d = step[k]
                if d.startswith("/"):
                    if do != "copy" or not step.get("sudo"):
                        self.err(where, f"absolute {k} needs a sudo copy step with a why: {d}")
                elif not d.startswith("~/"):
                    self.err(where, f"{k} must start with ~/ (or be a sudo copy to an absolute path): {d}")
        if do in ("uv-tool", "uv-sync") and not (repo_path(step["dir"]) / "pyproject.toml").is_file():
            self.err(where, f"no pyproject.toml in {step['dir']}")
        if do == "uv-sync":
            if not (repo_path(step["dir"]) / "uv.lock").is_file():
                self.err(where, f"uv-sync needs a uv.lock in {step['dir']}")
            made.add(step["dir"] + "/.venv/")
        if do == "npm-ci":
            if not (repo_path(step["dir"]) / "package-lock.json").is_file():
                self.err(where, f"npm-ci needs a package-lock.json in {step['dir']}")
            made.add(step["dir"] + "/node_modules/")
        if do == "uv-venv" and (not isinstance(step["packages"], list) or not step["packages"]):
            self.err(where, "uv-venv needs a non-empty packages list")
        if do == "copy" and "mode" in step and not re.fullmatch(r"[0-7]{3,4}", str(step["mode"])):
            self.err(where, f"mode must be octal digits: {step['mode']}")
        if do == "run":
            cmd = step["cmd"]
            if ("sudo " in cmd) != bool(step.get("sudo")):
                self.err(where, "set sudo: true exactly when the command runs sudo")
            first = cmd.split()[0] if cmd.split() else ""
            if first == "sudo" and len(cmd.split()) > 1:
                first = cmd.split()[1]
            if first.startswith(("framework/", "servers/", "install/", "tools/")):
                f = repo_path(first)
                if not f.is_file() or not os.access(f, os.X_OK):
                    self.err(where, f"delegated installer missing or not executable: {first}")
            if PIPED.search(cmd) and "https://" not in cmd:
                self.err(where, "a piped remote script must name its https URL")
            self.check_download(tid, where, cmd)
        if do == "uv-venv" and isinstance(step.get("packages"), list):
            for pkg in step["packages"]:
                if not re.fullmatch(r"[\w.\[\],-]+==" + EXACT_VER.pattern, str(pkg)):
                    self.err(where, f"package not pinned to one exact version: {pkg}")
        harness_target = any(str(step.get(k, "")).startswith(HARNESS_DEST) for k in ("dest", "path", "venv"))
        harness_cmd = do == "run" and any(h in step["cmd"] for h in HARNESS_CMD)
        if (harness_target or harness_cmd) and step.get("harness") is not True:
            self.err(where, "touches agent config (~/.claude, ~/.codex, global git): needs harness: true")

    def check_download(self, tid, where, cmd):
        """Remote code only from immutable versions: a piped script has the pinned shape, a versioned URL and a
        recorded sha256; curl/wget appear in no other shape; npm, uv, pip, mise and docker name exact versions."""
        hashes = self.tool.get("script_sha256", {}) if isinstance(self.tool.get("script_sha256"), dict) else {}
        if PIPED.search(cmd):
            m = PINNED_SCRIPT.fullmatch(cmd.strip())
            if not m:
                self.err(where, "a piped script must be exactly `curl <https URL> | [VAR=v] sh|bash [args]` "
                                "(the installer checks the file's sha256, then runs it)")
            else:
                url = m.group("url")
                self.used_urls.add(url)
                if not VERSIONED_URL.search(url):
                    self.err(where, f"piped script URL names no exact version (a release, tag or commit): {url}")
                if not SHA256.fullmatch(str(hashes.get(url, ""))):
                    self.err(where, f"no sha256 for {url} in script_sha256")
        elif re.search(r"\b(curl|wget)\b", cmd):
            self.err(where, "downloads outside the pinned `curl <url> | sh` shape (no sha256 check would run)")
        for spec in floating(cmd):
            self.err(where, f"not pinned to one exact version: {spec}")

    # ------------------------------------------------------------------ tools
    def check_tool(self, t, ids):
        tid = t.get("id", "<no id>")
        for k, typ in REQUIRED.items():
            if not isinstance(t.get(k), typ) or (typ in (str, list, dict) and not t.get(k)):
                self.err(tid, f"'{k}' missing or not a non-empty {typ.__name__}")
        for k in t.keys() - TOOL_KEYS:
            self.err(tid, f"unknown key '{k}'")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", str(t.get("id", ""))):
            self.err(tid, "id must be lowercase letters, digits and dashes")
        if not isinstance(t.get("profiles"), list) or not isinstance(t.get("methods"), dict):
            return
        for p in set(t["profiles"]) - PROFILES:
            self.err(tid, f"unknown profile '{p}' (allowed: {', '.join(sorted(PROFILES))})")
        if len(set(t["profiles"])) != len(t["profiles"]):
            self.err(tid, "duplicate profile names")
        if "os" in t and (not t["os"] or not set(t["os"]) <= set(OSES)):
            self.err(tid, f"bad os list {t['os']}")
        m = t["methods"]
        self.tool, self.used_urls = t, set()
        for k in m.keys() - METHOD_KEYS:
            self.err(tid, f"unknown method '{k}'")
        for k, v in m.items():
            if not isinstance(v, str) or not v.strip():
                self.err(tid, f"method '{k}' must be a non-empty command string")
            elif PIPED.search(v) and "https://" not in v:
                self.err(tid, f"method '{k}' pipes a remote script without naming its https URL")
            elif k != "claude-plugin" and AGENT_CFG_CMD.search(v):
                self.err(tid, f"method '{k}' writes agent config outside the harness gate "
                              "(only the claude-plugin method or a harness: true step may)")
            elif k != "manual":
                self.check_download(tid, f"{tid} method '{k}'", v)
                pull = re.fullmatch(r"docker pull (\S+)", v.strip())
                if pull and pull.group(1) not in t.get("verify", ""):
                    self.err(tid, f"verify must check the image the docker method pulls ({pull.group(1)})")
        for b in re.findall(r"command -v ([\w.-]+)", t.get("verify", "")):
            if b in SBIN and f"/usr/sbin/{b}" not in t["verify"]:
                self.err(tid, f"verify finds {b} with command -v only; Debian keeps it in /usr/sbin (add || test -x /usr/sbin/{b})")
        if not m.get("manual"):
            self.err(tid, "methods.manual is required (what to do when no method fits)")
            return
        for os_name in t.get("os", OSES):
            key, _ = resolve(t, os_name)
            if key == "manual" and "http" not in m["manual"]:
                self.err(tid, f"{os_name} falls back to manual, which names no URL to install from")
            if key in RUNTIME and RUNTIME[key] != tid and RUNTIME[key] not in t.get("requires", []):
                self.err(tid, f"{os_name} installs with {key} but requires lacks '{RUNTIME[key]}'")
        req = t.get("requires", [])
        if not isinstance(req, list):
            self.err(tid, "requires must be a list")
            req = []
        for r in req:
            if r not in ids:
                self.err(tid, f"requires unknown tool '{r}'")
            elif r == tid:
                self.err(tid, "requires itself")
        made = set()
        if t["bundled"]:
            if not repo_path(m["manual"]).exists():
                self.err(tid, f"bundled tool's methods.manual must be its path in the kit: {m['manual']}")
            if not repo_path(t["homepage"]).exists():
                self.err(tid, f"bundled tool's homepage must be its path in the kit: {t['homepage']}")
            for k in ("install", "entrypoints", "config"):
                if k not in t:
                    self.err(tid, f"bundled tool needs '{k}'")
            if not t.get("install"):
                self.err(tid, "bundled tool needs at least one install step")
        if "install" in t:
            if not isinstance(t["install"], list):
                self.err(tid, "install must be a list of steps")
            else:
                for i, step in enumerate(t["install"]):
                    self.check_step(tid, i, step, made)
        sha = t.get("script_sha256", {})
        if not isinstance(sha, dict):
            self.err(tid, "script_sha256 must map a script URL to its sha256")
        else:
            for url, h in sha.items():
                if url not in self.used_urls:
                    self.err(tid, f"script_sha256 names {url}, which no method or step pipes into a shell")
                elif not SHA256.fullmatch(str(h)):
                    self.err(tid, f"script_sha256 for {url} is not 64 lowercase hex digits")
        ep = t.get("entrypoints", {})
        if not isinstance(ep, dict):
            self.err(tid, "entrypoints must map a command name to its path in the kit")
            ep = {}
        for name, rel in ep.items():
            if not repo_path(rel).exists() and not any(rel.startswith(p) for p in made):
                self.err(tid, f"entrypoint '{name}' missing: {rel}")
        for step in t.get("install", []) if isinstance(t.get("install"), list) else []:
            if isinstance(step, dict) and step.get("do") == "link" and str(step.get("dest", "")).startswith("~/.local/bin/"):
                f = repo_path(step["src"])
                if f.exists() and not os.access(f, os.X_OK):
                    self.err(tid, f"{step['src']} is linked onto PATH but is not executable")
        cfg = t.get("config", [])
        if not isinstance(cfg, list) or not all(isinstance(c, dict) and isinstance(c.get("path"), str) for c in cfg):
            self.err(tid, "config must be a list of {path, keys?, required?, note?} objects")
        else:
            for c in cfg:
                for k in c.keys() - {"path", "keys", "required", "note"}:
                    self.err(tid, f"config key '{k}' unknown")

    def run(self):
        d = self.data
        if d.get("version") != 1 or not isinstance(d.get("tools"), list) or not d["tools"]:
            self.err("manifest", "expected {\"version\": 1, \"tools\": [...]}")
            return self.errors
        tools = [t for t in d["tools"] if isinstance(t, dict)]
        seen = {}
        for t in tools:
            seen[t.get("id")] = seen.get(t.get("id"), 0) + 1
        for tid, n in seen.items():
            if n > 1:
                self.err(str(tid), f"id used {n} times")
        ids = set(seen)
        by_id = {t.get("id"): t for t in tools}
        for t in tools:
            self.check_tool(t, ids)
        # requires: no cycles; profile closure; required tools never depend on optional ones
        state = {}

        def visit(tid, path):
            if state.get(tid) == 1:
                self.err(tid, "requires cycle: " + " -> ".join(path + [tid]))
                return
            if state.get(tid) == 2 or tid not in by_id:
                return
            state[tid] = 1
            for r in by_id[tid].get("requires", []) or []:
                visit(r, path + [tid])
            state[tid] = 2

        for tid in by_id:
            visit(tid, [])
        for t in tools:
            for r in t.get("requires", []) or []:
                dep = by_id.get(r)
                if not dep:
                    continue
                for p in t.get("profiles", []):
                    if p not in dep.get("profiles", []):
                        self.err(t["id"], f"requires '{r}', which is not in profile '{p}'")
                if not t.get("optional") and dep.get("optional"):
                    self.err(t["id"], f"is required but depends on optional '{r}'")
        for p in sorted(PROFILES):
            if not any(p in t.get("profiles", []) for t in tools):
                self.err("manifest", f"profile '{p}' has no tools")
        self.check_images(by_id)
        return self.errors

    def check_images(self, by_id):
        """Every compose template names exact image versions, and a docker-method tool pulls the image its service's
        compose file runs (tools.json, the templates and the uninstall lines stay on one version)."""
        files = sorted((KIT / "servers" / "compose").rglob("*.y*ml"))
        services = []
        for f in sorted((KIT / "profiles").glob("*/profile.json")):
            for s in json.loads(f.read_text()).get("services", []):
                if s.get("kind") == "compose":
                    services.append(s)
                    if KIT / s["compose"] not in files:
                        files.append(KIT / s["compose"])
        for f in files:
            for img in compose_images(f.read_text()):
                if not image_exact(img):
                    self.err(str(f.relative_to(KIT)), f"image not pinned to one exact version: {img}")
        for s in services:
            t = by_id.get(s.get("tool"), {})
            pull = re.fullmatch(r"docker pull (\S+)", str(t.get("methods", {}).get("docker", "")).strip())
            if pull and pull.group(1) not in compose_images((KIT / s["compose"]).read_text()):
                self.err(t["id"], f"pulls {pull.group(1)}, but {s['compose']} runs another image")


def self_test(data):
    """Each broken copy of the real manifest must be caught with the expected message."""
    import copy

    def first(d, tid):
        return next(t for t in d["tools"] if t["id"] == tid)

    cases = [
        ("duplicate id", lambda d: d["tools"].append(copy.deepcopy(first(d, "git"))), "id used 2 times"),
        ("unknown profile", lambda d: first(d, "git")["profiles"].append("loki2"), "unknown profile 'loki2'"),
        ("empty verify", lambda d: first(d, "jq").update(verify=""), "'verify' missing"),
        ("unknown require", lambda d: first(d, "jq").update(requires=["nope"]), "requires unknown tool 'nope'"),
        ("cycle", lambda d: first(d, "uv").update(requires=["scrapling"]), "requires cycle"),
        ("harness flag", lambda d: first(d, "kit-skills")["install"][0].pop("harness"), "needs harness: true"),
        ("piped script", lambda d: first(d, "mise")["methods"].update(debian="curl x.example | sh"), "https URL"),
        ("missing src", lambda d: first(d, "route")["install"][0].update(src="framework/nope"), "does not exist"),
        ("npm runtime", lambda d: first(d, "agent-browser").update(requires=[]), "requires lacks 'node'"),
        ("required on optional", lambda d: first(d, "web")["requires"].append("ffmpeg"), "depends on optional"),
        ("profile closure", lambda d: first(d, "tg")["requires"].append("route"), "not in profile 'telegram'"),
        ("sudo without flag", lambda d: first(d, "server-lane-loki")["install"][0].pop("sudo"), "sudo: true"),
        ("absolute dest", lambda d: first(d, "route")["install"][0].update(dest="/usr/local/bin/route"), "absolute dest"),
        ("unknown step", lambda d: first(d, "route")["install"].append({"do": "curl"}), "unknown step"),
        ("unknown key", lambda d: first(d, "git").update(homepge="x"), "unknown key 'homepge'"),
        ("plugin outside gate", lambda d: first(d, "superpowers")["methods"].update(npm="claude plugin install x@y"),
         "outside the harness gate"),
        ("sbin verify", lambda d: first(d, "samba").update(verify="command -v smbd"), "/usr/sbin"),
        ("unpinned script", lambda d: first(d, "uv")["methods"].update(debian="curl -fsSL https://astral.sh/uv/install.sh | sh"),
         "names no exact version"),
        ("script without sha256", lambda d: first(d, "uv").pop("script_sha256"), "no sha256 for"),
        ("stale sha256", lambda d: first(d, "uv")["script_sha256"].update({"https://x.example/v1.0/a.sh": "0" * 64}),
         "which no method or step pipes"),
        ("download and run", lambda d: first(d, "uv")["methods"].update(debian="curl -fsSLo /tmp/u.sh https://x.example/v1.0/u.sh && sh /tmp/u.sh"),
         "outside the pinned"),
        ("sudo pipe", lambda d: first(d, "uv")["methods"].update(debian="curl -fsSL https://x.example/v1.0/u.sh | sudo sh"),
         "must be exactly"),
        ("floating image", lambda d: first(d, "ntfy")["methods"].update(docker="docker pull binwiederhier/ntfy:latest"),
         "not pinned to one exact version: binwiederhier/ntfy:latest"),
        ("bare major image", lambda d: first(d, "uptime-kuma")["methods"].update(docker="docker pull louislam/uptime-kuma:2"),
         "not pinned to one exact version: louislam/uptime-kuma:2"),
        ("unpinned npm", lambda d: first(d, "gemini")["methods"].update(npm="npm install -g @google/gemini-cli"),
         "not pinned to one exact version: @google/gemini-cli"),
        ("unpinned uv tool", lambda d: first(d, "yt-dlp")["methods"].update(uv="uv tool install yt-dlp"),
         "not pinned to one exact version: yt-dlp"),
        ("unpinned pip", lambda d: first(d, "faster-whisper")["methods"].update(debian="python3 -m venv ~/v && ~/v/bin/pip install -q 'av<16'"),
         "not pinned to one exact version: av<16"),
        ("unpinned pip3", lambda d: first(d, "faster-whisper")["methods"].update(debian="python3 -m venv ~/v && ~/v/bin/pip3 install -q av"),
         "not pinned to one exact version: av"),
        ("unpinned npm i", lambda d: first(d, "gemini")["methods"].update(npm="npm i -g @google/gemini-cli"),
         "not pinned to one exact version: @google/gemini-cli"),
        ("unpinned venv package", lambda d: first(d, "tg-admin")["install"][0].update(packages=["telethon"]),
         "not pinned to one exact version: telethon"),
        ("verify misses image", lambda d: first(d, "pihole").update(verify="docker image inspect pihole/pihole >/dev/null"),
         "verify must check the image"),
        ("image drift", lambda d: first(d, "pihole")["methods"].update(docker="docker pull pihole/pihole:2025.01.0"),
         "runs another image"),
    ]
    for img, ok in (("ollama/ollama:latest", False), ("ollama/ollama:0", False), ("qmcgaw/gluetun:v3", False),
                    ("jellyfin/jellyfin:12.1", True), ("pihole/pihole:2026.09.0", True), ("host:5000/x", False),
                    ("x@sha256:" + "a" * 64, True)):
        if image_exact(img) != ok:
            print(f"FAIL self-test image_exact({img!r}) should be {ok}")
            return 1
    bad = 0
    for name, mutate, expect in cases:
        d = copy.deepcopy(data)
        mutate(d)
        errs = Checker(d).run()
        if not any(expect in e for e in errs):
            bad += 1
            print(f"FAIL self-test '{name}': expected '{expect}', got {errs[:3]}")
    if Checker(copy.deepcopy(data)).run():
        bad += 1
        print("FAIL self-test: the real manifest must pass first")
    print(f"self-test: {len(cases) - bad}/{len(cases)} broken manifests caught" if not bad else "self-test FAILED")
    return 1 if bad else 0


def main():
    try:
        data = json.loads(MANIFEST.read_text())
    except (OSError, ValueError) as e:
        print(f"FAIL: cannot load {MANIFEST}: {e}")
        return 1
    if "--self-test" in sys.argv[1:]:
        return self_test(data)
    errors = Checker(data).run()
    if "--plan" in sys.argv[1:]:
        for t in data["tools"]:
            row = []
            for o in OSES:
                r = resolve(t, o)
                row.append(f"{o}={'skip' if r is None else r[0]}")
            print(f"{t['id']:24} {' '.join(row)}")
    if errors:
        print("\n".join(f"FAIL {e}" for e in errors))
        print(f"FAIL: {len(errors)} problem(s) in {MANIFEST.relative_to(KIT)}")
        return 1
    n = len(data["tools"])
    b = sum(t["bundled"] for t in data["tools"])
    print(f"PASS: {n} tools ({b} bundled, {n - b} third-party), every profile and OS resolves")
    return 0


if __name__ == "__main__":
    sys.exit(main())
