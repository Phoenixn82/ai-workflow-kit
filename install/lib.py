#!/usr/bin/env python3
"""The engine behind install/install.sh and install/doctor.sh. Python 3.10+, stdlib only.

    lib.py install --profile a[,b] [--dry-run] [--yes] [--os arch|debian|macos] [--harness]
                   [--optional all|none|<id,...>] [--only <id,...>] [--ignore-hardware]
    lib.py doctor  [--profile a[,b]] [--os arch|debian|macos]
    lib.py plan    --profile a[,b] [--os ...]     the plan as JSON: [{id, method, cmd, present, optional, skip}]
    lib.py detect-os                              arch | debian | macos | unknown

Every command runs in bash with the clone's root as the working directory and install/shell-env.sh as BASH_ENV.
Exit codes: 0 ok; 1 a tool, service, rule or unit failed (doctor: something required is missing or down);
2 bad arguments or a failed prerequisite (OS, hardware, Homebrew, sudo, no confirmation). install/README.md has
the contract.
"""
import argparse
import concurrent.futures
import copy
import filecmp
import grp
import hashlib
import http.client
import json
import os
import platform
import pwd
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "install"))
from test_manifest import OSES, PIPED, PINNED_SCRIPT, resolve  # noqa: E402  one rule for the checker and the installer

PROFILE_NAMES = ("odin", "loki", "heimdall", "thor", "raven", "telegram")
SHELL_ENV = KIT / "install" / "shell-env.sh"
VERIFY_TIMEOUT = 10
HEALTH_WAIT = int(os.environ.get("KIT_HEALTH_WAIT", "60"))   # seconds a service gets to answer its health check
TS = datetime.now().strftime("%Y%m%d-%H%M%S")
TOL = 0.9   # RAM, VRAM and disks report a little under their label (a "16 GB" machine shows about 15.5 GiB)
OK_STATES = ("present", "installed", "would install")
q = shlex.quote

# Work a compose service needs after `up` that its health check cannot see: the embed API answers before the model
# exists. Keyed by (template, service); run in the project directory. Source: the template's own header.
POST_UP = {
    ("framework/_tools/vault-rag/heimdall/compose.yaml", "ollama"): [
        "docker exec ollama-embed ollama list | grep -q '^nomic-embed-text:latest' || "
        "{ docker exec ollama-embed ollama pull nomic-embed-text:v1.5 && "
        "docker cp nomic-embed-text.Modelfile ollama-embed:/tmp/m && "
        "docker exec ollama-embed ollama create nomic-embed-text:latest -f /tmp/m; }",
    ],
}

REGISTRY_HEAD = """# Automation registry

Every standing automation on your machines: what it does, when it runs, and the exact command that stops it.
install.sh adds a row for each timer or unit it installs. One global stop: touch ~/AI_Projects/_system/automations/HALT.

| Automation | Schedule | Lane | Scope | How to kill |
|---|---|---|---|---|
"""


class UsageError(Exception):
    pass


# ---------------------------------------------------------------------------------------------- paths and data
def home():
    return Path(os.path.expanduser("~"))


def expand(p):
    return Path(os.path.expanduser(str(p)))


def tilde(p):
    s, h = str(p), str(home())
    return "~" + s[len(h):] if s == h or s.startswith(h + "/") else s


def manifest():
    return json.loads((KIT / "install" / "tools.json").read_text())["tools"]


def profile(name):
    return json.loads((KIT / "profiles" / name / "profile.json").read_text())


def detect_os():
    system = platform.system()
    if system == "Darwin":
        return "macos"
    if system != "Linux":
        return "unknown"
    try:
        text = Path(os.environ.get("KIT_OS_RELEASE", "/etc/os-release")).read_text()
    except OSError:
        return "unknown"
    kv = dict(re.findall(r'^(\w+)=["\']?([^"\'\n]*)', text, re.M))
    ids = {kv.get("ID", "")} | set(kv.get("ID_LIKE", "").split())
    if ids & {"arch", "archlinux"}:
        return "arch"
    if ids & {"debian", "ubuntu"}:
        return "debian"
    return "unknown"


def env_file(path):
    """KEY=value lines of an env file (the last one wins; no shell expansion). Values are never printed."""
    out = {}
    try:
        lines = expand(path).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_]\w*)=(.*)$", line)
        if m:
            v = m.group(2).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            out[m.group(1)] = v
    return out


def hosts_env():
    """~/.config/ai-workflow-kit/hosts.env (or $KIT_HOSTS_ENV); an environment variable of the same name wins."""
    vals = env_file(os.environ.get("KIT_HOSTS_ENV") or home() / ".config/ai-workflow-kit/hosts.env")
    return {k: os.environ.get(k) or v for k, v in vals.items()}


def backup_path(path):
    """<file>.bak-<timestamp> beside it. Inside a skills/ folder the copy goes to skills.bak-<timestamp>/<name>
    instead: a copy beside a skill would load as a second skill."""
    path = Path(path)
    if path.parent.name == "skills":
        base = path.parent.with_name(f"skills.bak-{TS}") / path.name
    else:
        base = path.with_name(f"{path.name}.bak-{TS}")
    b, n = base, 1
    while b.exists() or b.is_symlink():
        b, n = base.with_name(f"{base.name}-{n}"), n + 1
    return b


def real(path):
    """The file a link points at (a dotfiles manager's link stays a link: the file behind it is what changes)."""
    path = Path(path)
    return Path(os.path.realpath(path)) if path.is_symlink() else path


def write_atomic(path, text):
    """Replace path with text in one step. The temp file is a new, uniquely named file in the same folder (created
    exclusively, so an existing name or link there is never followed), starts at 0600 and gets the final mode (the
    old file's, else what your umask gives a new one) before any content is written; it is removed on failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mask = os.umask(0o077)
        os.umask(mask)
        mode = 0o666 & ~mask
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".kit-tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), mode)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def trees_equal(src, dest):
    """dest holds every file of src with the same bytes (extra files in dest, such as caches, do not count)."""
    c = filecmp.dircmp(src, dest, ignore=["__pycache__"])
    if c.left_only or c.funny_files:
        return False
    _, mismatch, errors = filecmp.cmpfiles(src, dest, c.common_files, shallow=False)
    return not mismatch and not errors and all(trees_equal(Path(src) / d, Path(dest) / d) for d in c.common_dirs)


def same(do, src, dest, mode=None):
    """dest is already exactly the kit's link or copy."""
    if do == "link":
        return dest.is_symlink() and dest.resolve() == src.resolve()
    if not dest.exists():
        return False
    try:
        if src.is_dir():
            return dest.is_dir() and trees_equal(src, dest)
        if not (dest.is_file() and filecmp.cmp(src, dest, shallow=False)):
            return False
    except OSError:
        return False
    return mode is None or stat.S_IMODE(dest.stat().st_mode) == int(str(mode), 8)


# ---------------------------------------------------------------------------------------------- running commands
def npm_global_prefix(env):
    """Where `npm install -g` writes, worked out the way npm does (a configured prefix, else node's own install
    prefix) without running npm, which would leave a log in ~/.npm. None when no npm or a prefix is configured."""
    node = shutil.which("node", path=env["PATH"])
    if not node or not shutil.which("npm", path=env["PATH"]) or env.get("NPM_CONFIG_PREFIX") or env.get("npm_config_prefix"):
        return None
    try:
        if re.search(r"^\s*prefix\s*=", (home() / ".npmrc").read_text(), re.M):
            return None
    except OSError:
        pass
    return Path(node).resolve().parent.parent


def docker_needs_sg():
    """In the docker group on disk but not in this login (right after the install), and sg can bridge it."""
    if platform.system() != "Linux" or not shutil.which("sg"):
        return False
    try:
        g = grp.getgrnam("docker")
        user = pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return False
    return user in g.gr_mem and g.gr_gid not in os.getgroups() and os.getegid() != g.gr_gid


def child_env(extra=None):
    """Environment for every command: ~/.local/bin and mise shims first (uv, claude and npm install there),
    Homebrew and sbin dirs last, shell-env.sh as BASH_ENV, and npm -g into ~/.local when its folder is root's."""
    env = dict(os.environ)
    h = home()
    parts = [p for p in env.get("PATH", "/usr/bin:/bin").split(":") if p]
    front = [str(h / ".local/bin"), str(h / ".local/share/mise/shims")]
    back = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/sbin", "/sbin"]
    env["PATH"] = ":".join([p for p in front if p not in parts] + parts + [p for p in back if p not in parts])
    env["BASH_ENV"] = str(SHELL_ENV)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("KIT_ROOT_NO_SUDO", None)
    env.pop("KIT_DOCKER_SG", None)
    if os.geteuid() == 0 and not shutil.which("sudo", path=env["PATH"]):
        env["KIT_ROOT_NO_SUDO"] = "1"
    if docker_needs_sg():
        env["KIT_DOCKER_SG"] = "1"
    p = npm_global_prefix(env)
    if p:
        target = p / "lib" / "node_modules" if (p / "lib" / "node_modules").exists() else p
        if not os.access(target, os.W_OK):
            env["NPM_CONFIG_PREFIX"] = str(h / ".local")
    if extra:
        env.update(extra)
    return env


def sh(cmd, cwd=None, timeout=None, capture=False, env=None):
    """Run cmd in bash. capture=True: no terminal, output returned, the whole process group killed on timeout.
    capture=False: output goes to the terminal (sudo can ask for a password)."""
    p = subprocess.Popen(["bash", "-c", cmd], cwd=str(cwd or KIT), env=env or child_env(),
                         stdin=subprocess.DEVNULL if capture else None,
                         stdout=subprocess.PIPE if capture else None,
                         stderr=subprocess.STDOUT if capture else None, text=True, start_new_session=capture)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            p.kill()
        p.communicate()
        return 124, f"timed out after {timeout}s"
    return p.returncode, out or ""


def remote_script(cmd):
    """The https URL of a remote script the command pipes into a shell, or None."""
    if not PIPED.search(cmd):
        return None
    m = re.search(r"https://\S+", cmd)
    return m.group(0).rstrip("'\")") if m else "(no https URL: refusing)"


SCRIPT_MAX = 8 * 1024 * 1024    # an installer script is a few hundred KB at most


def fetch(url, dest):
    """Download url into dest (a new file in a private folder); the sha256 of what arrived."""
    digest, size = hashlib.sha256(), 0
    req = urllib.request.Request(url, headers={"User-Agent": "ai-workflow-kit-install"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "xb") as f:
        while chunk := r.read(65536):
            size += len(chunk)
            if size > SCRIPT_MAX:
                raise ValueError(f"larger than {SCRIPT_MAX} bytes")
            digest.update(chunk)
            f.write(chunk)
    return digest.hexdigest()


def check_tool(tool, env):
    """ok | missing | wrong-version, from the tool's verify command (10 s timeout, run in the clone)."""
    rc, _ = sh(tool["verify"], timeout=VERIFY_TIMEOUT, capture=True, env=env)
    if rc == 0:
        return "ok"
    first = tool["verify"].split()[0]
    if ("version" in tool["verify"] and "/" not in first and first not in ("command", "test", "[", "!")
            and shutil.which(first, path=env["PATH"])):
        return "wrong-version"
    return "missing"


def present(tool, env=None):
    return check_tool(tool, env or child_env()) == "ok"


def probe(tools, env):
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        return dict(zip([t["id"] for t in tools], ex.map(lambda t: check_tool(t, env), tools)))


# ---------------------------------------------------------------------------------------------- the plan
def closure(ids, by_id):
    out, todo = set(), list(ids)
    while todo:
        i = todo.pop()
        if i not in out:
            out.add(i)
            todo.extend(by_id[i].get("requires", []))
    return out


def order(ids, by_id):
    """Dependencies first; a server lane (packages, Docker, firewall, the stack folder) before everything else."""
    ids = list(ids)
    first = sorted(ids, key=lambda i: (not i.startswith("server-lane-"), ids.index(i)))
    out, seen = [], set()

    def visit(i):
        if i in seen:
            return
        seen.add(i)
        for r in by_id[i].get("requires", []):
            if r in ids:
                visit(r)
        out.append(i)

    for i in first:
        visit(i)
    return out


def plan(names, os_name, only=None, probe_state=True, env=None):
    """One row per tool of the profiles, install order: {id, tool, optional, bundled, method, cmd, skip, state}."""
    by_id = {t["id"]: t for t in manifest()}
    want = []
    for n in names:
        for tid in profile(n)["tools"]:
            if tid not in want:
                want.append(tid)
    if only:
        unknown = sorted(set(only) - set(want))
        if unknown:
            raise UsageError(f"--only: not in {', '.join(names)}: {', '.join(unknown)}")
        keep = closure(only, by_id)
        want = [t for t in want if t in keep]
    rows = []
    for tid in order(want, by_id):
        t = by_id[tid]
        r = resolve(t, os_name)
        rows.append({"id": tid, "tool": t, "optional": t["optional"], "bundled": t["bundled"],
                     "method": r[0] if r else None, "cmd": r[1] if r else None,
                     "skip": None if r else f"not for {os_name} ({', '.join(t['os'])} only)", "state": None})
    if probe_state:
        states = probe([r["tool"] for r in rows if not r["skip"]], env or child_env())
        for r in rows:
            r["state"] = states.get(r["id"])
    return rows


def describe(s):
    tags = (" [--harness]" if s.get("harness") else "") + (" [sudo]" if s.get("sudo") else "") \
        + (f" [{'/'.join(s['os'])} only]" if s.get("os") else "")
    do = s["do"]
    if do == "link":
        return f"link {s['dest']} -> {s['src']}{tags}"
    if do == "copy":
        return f"copy {s['src']} -> {s['dest']}{tags}"
    if do == "mkdir":
        return f"mkdir {s['path']}{tags}"
    if do == "uv-venv":
        return f"uv-venv {s['venv']} ({' '.join(s['packages'])}){tags}"
    if do == "run":
        return f"run {s['cmd'][:90]}{'...' if len(s['cmd']) > 90 else ''}{tags}"
    if do == "note":
        return f"note: {s['text'][:90]}"
    return f"{do} {s['dir']}{tags}"


# ---------------------------------------------------------------------------------------------- hardware
def hw_facts():
    """What this machine has, in GiB. KIT_FAKE_HW (JSON with the same keys) replaces the probe in tests."""
    f = {"ram_gb": 0.0, "disk_gb": 0.0, "nvidia_vram_gb": 0.0, "apple_silicon": False, "unified_memory_gb": 0.0}
    if os.environ.get("KIT_FAKE_HW"):
        f.update(json.loads(os.environ["KIT_FAKE_HW"]))
        return f
    gib = 1024 ** 3
    system, mounts = platform.system(), ["/"]
    if system == "Linux":
        try:
            f["ram_gb"] = int(re.search(r"MemTotal:\s+(\d+)", Path("/proc/meminfo").read_text()).group(1)) * 1024 / gib
            mounts += [ln.split()[1].replace("\\040", " ") for ln in Path("/proc/mounts").read_text().splitlines()
                       if ln.startswith("/dev/")]
        except (OSError, AttributeError):
            pass
    elif system == "Darwin":
        try:
            f["ram_gb"] = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
                                             timeout=10).stdout.strip()) / gib
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        f["apple_silicon"] = platform.machine() == "arm64"
        f["unified_memory_gb"] = f["ram_gb"] if f["apple_silicon"] else 0.0
        mounts += ["/System/Volumes/Data"] + [str(p) for p in Path("/Volumes").glob("*")]
    for m in mounts:
        try:
            st = os.statvfs(m)
            f["disk_gb"] = max(f["disk_gb"], st.f_blocks * st.f_frsize / gib)
        except OSError:
            pass
    if shutil.which("nvidia-smi"):
        try:
            p = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=15)
            vals = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", p.stdout)] if p.returncode == 0 else []
            f["nvidia_vram_gb"] = max(vals) / 1024 if vals else 0.0
        except (OSError, subprocess.TimeoutExpired):
            pass
    return f


def check_hardware(prof, f):
    """[] when the machine fits the profile's requires_hardware, else one reason per miss."""
    hw, bad = prof.get("requires_hardware", {}), []
    if hw.get("ram_gb") and f["ram_gb"] < hw["ram_gb"] * TOL:
        bad.append(f"needs {hw['ram_gb']} GB of RAM; this machine has {f['ram_gb']:.1f} GB")
    if hw.get("disk_gb") and f["disk_gb"] < hw["disk_gb"] * TOL:
        bad.append(f"needs a {hw['disk_gb']} GB disk; the largest here is {f['disk_gb']:.0f} GB")
    if hw.get("any_of"):
        wants, fits = [], False
        for b in hw["any_of"]:
            ram_ok = f["ram_gb"] >= b.get("ram_gb", 0) * TOL
            if b.get("gpu") == "nvidia":
                wants.append(f"an NVIDIA GPU with {b.get('vram_gb', 1)} GB of VRAM")
                fits |= f["nvidia_vram_gb"] >= b.get("vram_gb", 1) * TOL and ram_ok
            elif b.get("cpu") == "apple-silicon":
                wants.append(f"Apple silicon with {b.get('unified_memory_gb', 1)} GB of unified memory")
                fits |= f["apple_silicon"] and f["unified_memory_gb"] >= b.get("unified_memory_gb", 1) * TOL and ram_ok
        if not fits:
            found = [f"NVIDIA GPU with {f['nvidia_vram_gb']:.0f} GB" if f["nvidia_vram_gb"] else "no NVIDIA GPU",
                     f"Apple silicon with {f['unified_memory_gb']:.0f} GB" if f["apple_silicon"] else "not Apple silicon"]
            bad.append("needs " + " or ".join(wants) + f" (found: {', '.join(found)})")
    return bad


# ---------------------------------------------------------------------------------------------- merging rules
def norm_cmd(c):
    """A hook command compared without quotes and with $HOME, ${HOME} and ~/ spelled out."""
    h = str(home())
    c = c.replace("${HOME}", h).replace("$HOME", h)
    c = re.sub(r"(^|\s)~/", lambda m: m.group(1) + h + "/", c)
    return " ".join(c.replace('"', "").replace("'", "").split())


HOME_VAR = re.compile(r"\$\{HOME\}|\$HOME(?![A-Za-z0-9_])")


def render_command(cmd, h=None):
    """A hook command with every $HOME / ${HOME} (and a word-initial ~) replaced by the home path h, quoted for the
    spot it stands in: bare -> shlex-quoted; inside "..." -> as is when it has no special characters, else the
    quotes are closed around a shlex-quoted copy; inside '...' -> '\\'' for each quote. The home stays one word
    whatever it holds (spaces, quotes, $, backslashes), read the same by sh, bash and shlex; every other character,
    such as a launch-failure guard around the command, is kept as it is."""
    h = str(home()) if h is None else h
    dq = h if q(h) == h else f'"{q(h)}"'
    sq = h.replace("'", "'\\''")
    out, i, quote, n = [], 0, None, len(cmd)
    while i < n:
        c = cmd[i]
        if c == "\\" and quote != "'" and i + 1 < n:      # an escaped character stays as it is
            out.append(cmd[i:i + 2])
            i += 2
            continue
        if c in "'\"" and quote in (None, c):
            quote = None if quote == c else c
            out.append(c)
            i += 1
            continue
        m = HOME_VAR.match(cmd, i)
        if m:
            out.append(q(h) if quote is None else dq if quote == '"' else sq)
            i = m.end()
            continue
        if (c == "~" and quote is None and (i == 0 or cmd[i - 1] in " \t;&|(=")
                and (i + 1 == n or cmd[i + 1] in "/ \t;&|)")):
            out.append(q(h))
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def render_strings(v, h, key=None):
    """$HOME spelled out: shell-quoted in a hook "command", plainly anywhere else (env values are not shell)."""
    if isinstance(v, str):
        if key == "command":
            return render_command(v, h)
        return v.replace("${HOME}", h).replace("$HOME", h)
    if isinstance(v, list):
        return [render_strings(x, h, key) for x in v]
    if isinstance(v, dict):
        return {k: render_strings(x, h, k) for k, x in v.items()}
    return v


def cmd_words(cmd):
    """The words of a shell command after quote removal (operators split off); a plain split if it will not parse."""
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return cmd.split()


def merge_hooks(mine, kit_hooks, added, skipped, hook_ok):
    """Add each kit hook whose command (normalised) is not wired yet and whose scripts are installed."""
    hooks = mine.get("hooks") if isinstance(mine.get("hooks"), dict) else None
    for event, groups in kit_hooks.items():
        have = {norm_cmd(h.get("command", "")) for g in (hooks or {}).get(event, []) for h in g.get("hooks", [])}
        for g in groups:
            for h in g.get("hooks", []):
                c = h.get("command", "")
                if norm_cmd(c) in have:
                    continue
                missing = hook_ok(c)
                if missing:
                    skipped.append(f"hooks.{event}: {tilde(missing)} is not installed")
                    continue
                if hooks is None:
                    hooks = mine["hooks"] = {}
                lst = hooks.setdefault(event, [])
                grp_ = next((x for x in lst if x.get("matcher") == g.get("matcher")), None)
                if grp_ is None:
                    grp_ = {"matcher": g["matcher"]} if "matcher" in g else {}
                    grp_["hooks"] = []
                    lst.append(grp_)
                grp_["hooks"].append(copy.deepcopy(h))
                have.add(norm_cmd(c))
                added.append(f"hooks.{event}: {c[:70]}")


def hold_unrestricted(mine, merged, added, skipped):
    """Unattended mode (bypassPermissions, no prompt) is added only when the safety dispatcher is wired as a
    PreToolUse hook in the result; otherwise the keys this merge would add are left out and reported."""
    hooks = merged.get("hooks") if isinstance(merged.get("hooks"), dict) else {}
    if any("dispatch/dispatch.sh" in str(h.get("command", "")) for g in hooks.get("PreToolUse") or []
           if isinstance(g, dict) for h in g.get("hooks") or [] if isinstance(h, dict)):
        return
    perms, mine_perms = merged.get("permissions"), mine.get("permissions")
    if isinstance(perms, dict) and perms.get("defaultMode") == "bypassPermissions" and \
            not (isinstance(mine_perms, dict) and "defaultMode" in mine_perms):
        del perms["defaultMode"]
        skipped.append("permissions.defaultMode (bypassPermissions waits for the safety dispatcher's PreToolUse hook)")
    if merged.get("skipDangerousModePermissionPrompt") is True and "skipDangerousModePermissionPrompt" not in mine:
        del merged["skipDangerousModePermissionPrompt"]
        skipped.append("skipDangerousModePermissionPrompt (waits for the safety dispatcher's PreToolUse hook)")
    added[:] = [a for a in added if a not in ("permissions.defaultMode", "skipDangerousModePermissionPrompt")]


def merge_values(mine, kit, path, added, kept, skipped, hook_ok):
    """Your values win; missing keys and list items are added; differing values are kept and reported."""
    for k, v in kit.items():
        where = f"{path}{k}"
        if k == "hooks" and not path and isinstance(v, dict):
            merge_hooks(mine, v, added, skipped, hook_ok)
            continue
        if k not in mine:
            if v not in ({}, []):
                mine[k] = copy.deepcopy(v)
                added.append(where)
            continue
        cur = mine[k]
        if isinstance(cur, dict) and isinstance(v, dict):
            merge_values(cur, v, where + ".", added, kept, skipped, hook_ok)
        elif isinstance(cur, list) and isinstance(v, list):
            new = [x for x in v if x not in cur]
            if new:
                cur.extend(copy.deepcopy(new))
                added.append(f"{where} (+{len(new)})")
        elif cur != v:
            kept.append(where)


def md_sections(text):
    """(preamble, [(key, block)]) split at level-2 headings outside code fences."""
    pre, secs, cur, key, fence = [], [], None, None, False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        if not fence and line.startswith("## "):
            if cur is not None:
                secs.append((key, "".join(cur)))
            key, cur = " ".join(line[3:].strip().lower().split()), [line]
            continue
        (cur if cur is not None else pre).append(line)
    if cur is not None:
        secs.append((key, "".join(cur)))
    return "".join(pre), secs


def merge_markdown(kit_text, mine_text, label):
    """Append the kit's level-2 sections whose heading your file lacks; never edit a section you have.
    Returns (new text, added headings, your sections that differ from the kit's)."""
    if mine_text is None:
        return kit_text, ["(new file)"], []
    _, kit_secs = md_sections(kit_text)
    _, my_secs = md_sections(mine_text)
    mine = dict(my_secs)
    flat = lambda s: " ".join(s.split())  # noqa: E731
    if not kit_secs:
        missing = [] if flat(kit_text) in flat(mine_text) else [("(whole file)", kit_text)]
    else:
        missing = [(k, b) for k, b in kit_secs if k not in mine]
    kept = [k for k, b in kit_secs if k in mine and flat(mine[k]) != flat(b)]
    if not missing:
        return mine_text, [], kept
    stamp = f"<!-- added by ai-workflow-kit install.sh on {TS[:8]} from {label}; edit or delete freely -->"
    new = mine_text.rstrip("\n") + "\n\n" + stamp + "\n\n" + "\n".join(b.rstrip("\n") + "\n" for _, b in missing)
    return new, [b.splitlines()[0].strip() if b.strip() else k for k, b in missing], kept


def chromium_path():
    for c in ("chromium", "chromium-browser"):
        p = shutil.which(c)
        if p:
            return p
    mac = "/Applications/Chromium.app/Contents/MacOS/Chromium"
    return mac if os.path.exists(mac) else None


def schedule(unit):
    p = KIT / unit
    if p.suffix != ".timer":
        return f"{p.suffix[1:]} unit"
    vals = re.findall(r"^(OnCalendar|OnBootSec|OnUnitActiveSec|OnActiveSec)=(.+)$", p.read_text(), re.M)
    return "; ".join(f"{k}={v.strip()}" for k, v in vals) or "timer"


# ---------------------------------------------------------------------------------------------- the installer
class Installer:
    def __init__(self, a):
        self.a, self.dry, self.harness, self.os = a, a.dry_run, a.harness, a.os
        self.family = "macos" if a.os == "macos" else "linux"
        self.status, self.why = {}, {}
        self.backups, self.kept, self.notes, self.failed, self.left_out = [], [], [], [], []
        self.would_backup = 0
        self.backed_up = set()
        self.will_exist = {}        # dry run: destination -> its source in the kit (hook filtering)
        self.apt_updated = False
        self.env = child_env()
        self.chosen = set()
        self.all_optional = False
        self.did = False
        self.rows = {}
        self.fresh_env = set()      # compose projects whose .env this run made (they wait for you)

    def say(self, msg=""):
        print(msg, flush=True)

    # ------------------------------------------------------------------ state helpers
    def set(self, tid, status, why=""):
        self.status[tid], self.why[tid] = status, why
        if status == "failed":
            self.failed.append(f"{tid}: {why}" if why else tid)
        return status

    def chosen_has(self, name):
        return self.all_optional or name in self.chosen

    def included(self, tid):
        r = self.rows.get(tid)
        return bool(r) and not r["skip"] and (not r["optional"] or self.chosen_has(tid))

    def tool_ok(self, tid):
        return self.included(tid) and self.status.get(tid) in OK_STATES

    def unmet(self, tid):
        """What tid requires that is not in place in this run."""
        return [d for d in self.rows[tid]["tool"].get("requires", []) if not self.tool_ok(d)]

    def why_not(self, tid):
        if tid not in self.rows:
            return f"its tool {tid} is not part of this run"
        if not self.included(tid):
            return f"its tool {tid} is " + (self.rows[tid]["skip"] or "optional and not chosen")
        return f"its tool {tid} is {self.status.get(tid, 'not installed')}"

    def backup(self, path, move):
        b = backup_path(path)
        b.parent.mkdir(parents=True, exist_ok=True)
        if move:
            os.rename(path, b)
        elif Path(path).is_dir() and not Path(path).is_symlink():
            shutil.copytree(path, b, symlinks=True)
        else:
            shutil.copy2(path, b, follow_symlinks=False)
        self.backed_up.add(str(path))
        self.backups.append(f"{tilde(path)} -> {tilde(b)}")
        self.say(f"  backup: {tilde(path)} -> {tilde(b)}")

    def backup_once(self, path):
        """Copy a file we are about to change, once per run (the file behind a link, not the link)."""
        path = real(path)
        if str(path) in self.backed_up or not path.exists():
            return
        if self.dry:
            self.backed_up.add(str(path))
            self.would_backup += 1
            self.say(f"  + would back up {tilde(path)} -> {tilde(backup_path(path))}")
            return
        self.backup(path, move=False)

    def exec(self, cmd, why=None, cwd=None, tool=None):
        if self.os == "debian" and not self.apt_updated and re.search(r"\bapt-get install\b", cmd):
            self.apt_updated = True
            if not self.exec("sudo apt-get update -qq", why="refresh the package lists once before the first apt install"):
                return False
        if why:
            self.say(f"  why: {why}")
        elif re.search(r"(^|[;&|(]\s*)sudo\b", cmd):
            self.say("  needs root (sudo): a system package or service")
        url = remote_script(cmd)
        if url:
            return self.run_script(cmd, url, tool, cwd)
        self.say(f"  + {cmd}" + (f"   (in {tilde(cwd)})" if cwd else ""))
        if self.dry:
            return True
        rc, _ = sh(cmd, cwd=cwd, env=self.env)
        if rc:
            self.say(f"  ! exit {rc}")
        return rc == 0

    def run_script(self, cmd, url, tool, cwd=None):
        """A `curl <versioned https URL> | sh` method is never piped: the script is downloaded into a private folder,
        its sha256 checked against the tool's script_sha256 in tools.json, and only then that file is run. Any other
        piped shape, a URL without a recorded hash, or a script that changed upstream is refused before it runs."""
        m = PINNED_SCRIPT.fullmatch(cmd.strip())
        want = (tool or {}).get("script_sha256", {}).get(m.group("url")) if m else None
        manual = (tool or {}).get("methods", {}).get("manual") or url
        if not m or not want:
            self.say(f"  ! refused: {cmd}\n    (a piped script must be `curl <versioned https URL> | sh` with its sha256 "
                     f"in tools.json script_sha256); install manually: {manual}")
            return False
        self.say(f"  runs the remote script {url} (sha256 {want[:16]}..., checked before it runs)")
        head, args = f"{m.group('env')}{m.group('sh')}", m.group("args") or ""
        self.say(f"  + {cmd}\n    as: {head} <the checked copy>{args}" + (f"   (in {tilde(cwd)})" if cwd else ""))
        if self.dry:
            return True
        d = tempfile.mkdtemp(prefix="kit-script.")
        try:
            path = os.path.join(d, "install.sh")
            try:
                got = fetch(url, path)
            except (OSError, ValueError, http.client.HTTPException) as e:     # a cut-off download too
                self.say(f"  ! could not download {url}: {e}; nothing was run")
                return False
            if got != want:
                self.say(f"  ! upstream changed: {url} has sha256 {got}, the kit pins {want}; nothing was run. "
                         f"Install manually: {manual}")
                return False
            rc, _ = sh(f"{head} {q(path)}{args}", cwd=cwd, env=self.env)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        if rc:
            self.say(f"  ! exit {rc}")
        return rc == 0

    # ------------------------------------------------------------------ prerequisites, plan, confirmation
    def prerequisites(self, profs):
        problems, hard = [], []
        for p in profs:
            if self.os not in p["os"]:
                hard.append(f"{p['name']}: runs on {', '.join(p['os'])}, not {self.os}")
        facts = hw_facts()
        for p in profs:
            problems += [f"{p['name']}: {w}" for w in check_hardware(p, facts)]
            if p.get("requires_hardware", {}).get("always_on"):
                self.notes.append(f"{p['name']}: this box should stay powered on and wired (always on)")
        for x in hard + problems:
            self.say(f"prerequisite: {x}")
        if hard or (problems and not self.a.ignore_hardware):
            self.say("Stopped before installing anything." + (" (--ignore-hardware skips the hardware check.)"
                                                               if problems and not hard else ""))
            return False
        if problems:
            self.say("--ignore-hardware: going on anyway.")
        return True

    def print_plan(self, rows, names):
        self.say(f"Plan: {', '.join(names)} on {self.os} ({len(rows)} tools; clone: {KIT})")
        for r in rows:
            opt = " (optional)" if r["optional"] else ""
            if r["skip"]:
                self.say(f"  {'skip':<9}{r['id']:<26}skip: {r['skip']}{opt}")
                continue
            if r["bundled"]:
                steps = r["cmd"] or []
                via = f"bundled: {len(steps)} step(s): " + "; ".join(describe(s) for s in steps[:3]) \
                    + (" ..." if len(steps) > 3 else "")
            elif r["method"] == "manual":
                via = f"install manually: {r['cmd']}"
            else:
                via = f"{r['method']}: {r['cmd']}"
            if r["state"] == "ok":
                state = "present"
            elif r["method"] == "manual":
                state = "manual"
            elif r["method"] == "claude-plugin" and not self.harness:
                state = "harness"
            else:
                state = "install"
            via = via if len(via) <= 160 else via[:157] + "..."
            self.say(f"  {state:<9}{r['id']:<26}{via}{opt}")

    def choose(self, candidates):
        """Which optional tools and services to add: --optional, else ask (a dry run shows them all)."""
        opt = self.a.optional
        if opt:
            if opt == "all":
                self.all_optional = True
            elif opt != "none":
                self.chosen = {x.strip() for x in opt.split(",") if x.strip()}
                unknown = self.chosen - set(candidates) - {r for r in self.rows if self.rows[r]["optional"]}
                if unknown:
                    raise UsageError(f"--optional: not optional in this plan: {', '.join(sorted(unknown))}")
            return
        if self.dry:
            self.all_optional = True
            return
        if not candidates:
            return
        if self.a.yes:
            self.say(f"Optional, left out (add with --optional all or --optional <id,...>): {', '.join(candidates)}")
            return
        ans = input(f"Optional, not installed yet: {', '.join(candidates)}\n"
                    "Add them? [a]ll, [n]one, or names separated by commas [n]: ").strip()
        if ans.lower() in ("a", "all"):
            self.all_optional = True
        elif ans and ans.lower() not in ("n", "none"):
            self.chosen = {x.strip() for x in ans.split(",") if x.strip()} & set(candidates)

    def sudo_reasons(self, profs):
        out = []
        for r in self.rows.values():
            if r["skip"] or r["state"] == "ok" or not self.included(r["id"]):
                continue
            cmds = [r["cmd"]] if isinstance(r["cmd"], str) and r["method"] != "manual" else []
            steps = r["cmd"] if r["bundled"] else r["tool"].get("install", [])
            for s in steps or []:
                if s.get("sudo") and self.os in s.get("os", OSES):
                    out.append(f"{r['id']}: {s.get('why')}")
            if any(re.search(r"\bsudo\b", c) for c in cmds):
                out.append(f"{r['id']}: system package")
        for p in profs:
            for a in p["automations"]:
                if a["scope"] == "system" and self.family == "linux" and self.included(a["tool"]) and any(
                        not (Path("/etc/systemd/system") / Path(f).name).exists() for f in a["files"]):
                    out.append(f"{a['name']}: system unit")
        return out

    # ------------------------------------------------------------------ tools
    def install_tool(self, r):
        tid, t = r["id"], r["tool"]
        if r["skip"]:
            return self.set(tid, "skipped", r["skip"])
        if r["state"] == "ok":
            return self.set(tid, "present")
        if r["optional"] and not self.chosen_has(tid):
            return self.set(tid, "not chosen")
        blockers = [d for d in t.get("requires", []) if d in self.rows and self.status.get(d) not in OK_STATES]
        if blockers:
            return self.set(tid, "blocked", "needs " + ", ".join(blockers))
        self.say(f"\n== {tid}: {t['name']}" + (f" ({r['state']})" if r["state"] == "wrong-version" else ""))
        self.did = False            # did any step (or the method) do work, as opposed to wait for --harness?
        if t["bundled"]:
            outcome = self.run_steps(tid, t.get("install", []))
        else:
            outcome = self.run_method(r)
            if outcome == "ok" and t.get("install"):
                outcome = self.run_steps(tid, t["install"])
        if self.dry:
            if outcome == "harness" and not self.did:
                return self.set(tid, "needs --harness")
            return self.set(tid, {"ok": "would install", "manual": "manual", "harness": "would install",
                                  "note": "would install", "kept": "would keep yours", "fail": "failed"}[outcome])
        self.env = child_env()      # a new PATH, npm prefix or docker group may have appeared
        if present(t, self.env):
            return self.set(tid, "installed")
        if outcome == "manual":
            return self.set(tid, "manual")
        if outcome == "harness":
            return self.set(tid, "needs --harness")
        if outcome == "note":
            return self.set(tid, "needs you", "see the notes below")
        if outcome == "kept":
            return self.set(tid, "failed", "one of your own files stands where the kit's goes (kept; see Kept)")
        return self.set(tid, "failed", "the install step failed" if outcome == "fail" else "its verify still fails")

    def docker_group(self):
        """Linux: be in the docker group, so compose and image pulls work without root. The package installers do
        not add you (the server lane does); until your next login, shell-env.sh runs docker through sg."""
        if self.family != "linux" or os.geteuid() == 0:
            return
        user = pwd.getpwuid(os.geteuid()).pw_name
        try:
            members = grp.getgrnam("docker").gr_mem
        except KeyError:
            members = None if self.dry else []
        if members is not None and user in members:
            return
        if not self.dry and sh("docker info", capture=True, timeout=20, env=self.env)[0] == 0:
            return                  # already usable (rootless Docker, or a group set up another way)
        if self.exec(f"sudo usermod -aG docker {q(user)}", why="run docker without sudo (takes effect at your next "
                     "login; this run bridges it with sg)"):
            self.notes.append("docker: you were added to the docker group; log out and back in once")
            self.env = child_env()
        else:
            self.failed.append("docker: could not add you to the docker group")

    def run_method(self, r):
        key, cmd = r["method"], r["cmd"]
        if key == "manual":
            self.say(f"  install manually: {cmd}")
            self.notes.append(f"{r['id']}: install manually: {cmd}")
            return "manual"
        if key == "claude-plugin":
            if not self.harness:
                self.say("  needs --harness: a Claude Code plugin installs into ~/.claude (settings.json)")
                return "harness"
            self.backup_once(home() / ".claude" / "settings.json")
        if self.os == "macos" and cmd.startswith("brew ") and not shutil.which("brew", path=self.env["PATH"]) \
                and not self.dry:
            self.say("  ! Homebrew is missing: https://brew.sh")
            return "fail"
        self.did = True
        return "ok" if self.exec(cmd, tool=r["tool"]) else "fail"

    def run_steps(self, tid, steps):
        rank = {"ok": 0, "kept": 1, "note": 2, "harness": 3, "fail": 4}
        worst = "ok"
        for s in steps:
            res = self.step(tid, s)
            if res == "fail":
                return "fail"
            worst = max(worst, res, key=rank.get)
        return worst

    def step(self, tid, s):
        do = s["do"]
        if "os" in s and self.os not in s["os"]:
            return "ok"
        if s.get("harness") and not self.harness:
            self.say(f"  - needs --harness: {describe(s)}")
            return "harness"
        if do == "note":
            self.say(f"  note: {s['text']}")
            self.notes.append(f"{tid}: {s['text']}")
            return "note"
        self.did = True
        if do in ("link", "copy"):
            return self.place(s)
        if do == "mkdir":
            d = expand(s["path"])
            if d.is_dir():
                self.say(f"  = present: {tilde(d)}/")
            elif self.dry:
                self.say(f"  + would mkdir {tilde(d)}")
                self.will_exist[str(d)] = None
            else:
                d.mkdir(parents=True, exist_ok=True)
                self.say(f"  + mkdir {tilde(d)}")
            return "ok"
        if do == "uv-tool":
            cmd = f"uv tool install --editable {q(str(KIT / s['dir']))}"
        elif do == "uv-sync":
            cmd = f"uv sync --frozen --project {q(str(KIT / s['dir']))}" + (f" --python {q(s['python'])}"
                                                                          if s.get("python") else "")
        elif do == "uv-venv":
            v = q(str(expand(s["venv"])))
            cmd = (f"{{ [ -x {v}/bin/python ] || uv venv --quiet" + (f" --python {q(s['python'])}" if s.get("python")
                                                                    else "") + f" {v}; }}"
                   f" && uv pip install --quiet --python {v}/bin/python " + " ".join(q(p) for p in s["packages"]))
        elif do == "npm-ci":
            cmd = f"npm ci --prefix {q(str(KIT / s['dir']))}"
        else:
            cmd = s["cmd"]
        return "ok" if self.exec(cmd, why=s.get("why"), tool=self.rows.get(tid, {}).get("tool")) else "fail"

    def place(self, s):
        """link / copy: an exact match is left alone; your own file is kept (only a --harness step replaces it,
        after a backup); a sudo copy goes through install(1)."""
        do, src, dest = s["do"], KIT / s["src"], expand(s["dest"])
        verb = "link" if do == "link" else "copy"
        if same(do, src, dest, s.get("mode")):
            self.say(f"  = present: {tilde(dest)}")
            return "ok"
        exists = dest.exists() or dest.is_symlink()
        if exists and not s.get("harness"):
            self.say(f"  = kept: {tilde(dest)} is already there and is not the kit's; yours stays (kit's: {s['src']})")
            self.kept.append(tilde(dest))
            return "kept"
        if self.dry:
            self.will_exist[str(dest)] = src
            if exists:
                self.would_backup += 1
                self.say(f"  + would back up {tilde(dest)} -> {tilde(backup_path(dest))}, then {verb} {s['src']}")
            else:
                self.say(f"  + would {verb} {tilde(dest)} <- {s['src']}")
            return "ok"
        if s.get("sudo"):
            b = backup_path(dest)
            keep = f"sudo cp -a {q(str(dest))} {q(str(b))} && " if exists else ""
            cmd = (keep + f"sudo mkdir -p {q(str(dest.parent))} && "
                   f"sudo install -m {s.get('mode', '644')} {q(str(src))} {q(str(dest))}")
            if not self.exec(cmd, why=s.get("why")):
                return "fail"
            if exists:
                self.backups.append(f"{tilde(dest)} -> {tilde(b)}")
            return "ok"
        if exists:
            self.backup(dest, move=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if do == "link":
            os.symlink(src, dest)
        elif src.is_dir():
            shutil.copytree(src, dest, symlinks=True)
        else:
            shutil.copy2(src, dest)
        if s.get("mode"):
            os.chmod(dest, int(str(s["mode"]), 8))
        self.say(f"  + {verb} {tilde(dest)} <- {s['src']}")
        return "ok"

    # ------------------------------------------------------------------ rules
    def hook_missing(self, cmd):
        """The first file under your home a hook command names that is not installed (None = all there). The command
        is read as shell words, so a home with spaces or quotes still yields whole paths."""
        h = str(home())
        for w in cmd_words(cmd):
            p = w.split("=", 1)[1] if "=" in w and not w.startswith("/") else w
            if not p.startswith(h + "/") or os.path.exists(p):
                continue
            covered = False
            for dest, src in self.will_exist.items():
                if p == dest:
                    covered = True
                elif p.startswith(dest + "/") and src is not None and (Path(src) / p[len(dest) + 1:]).exists():
                    covered = True
            if not covered:
                return p
        return None

    def plugin_ok(self, key):
        for r in self.rows.values():
            if f"claude plugin install {key}" in r["tool"]["methods"].get("claude-plugin", ""):
                return self.tool_ok(r["id"])
        return False

    def render_settings(self, d):
        """Your home spelled out (shell-quoted where it stands in a hook command, plainly in env values), host values
        from hosts.env, and only the plugins that are installed."""
        d = render_strings(d, str(home()))
        env = d.get("env")
        if isinstance(env, dict):
            if "OLLAMA_HOST" in env:
                url = hosts_env().get("THOR_OLLAMA_URL")
                if url:
                    env["OLLAMA_HOST"] = url
                else:
                    del env["OLLAMA_HOST"]
                    self.left_out.append("env.OLLAMA_HOST (set THOR_OLLAMA_URL in hosts.env, then run again)")
            if "CHROME_PATH" in env:
                c = chromium_path()
                if c:
                    env["CHROME_PATH"] = c
                else:
                    del env["CHROME_PATH"]
                    self.left_out.append("env.CHROME_PATH (no Chromium found)")
        if isinstance(d.get("enabledPlugins"), dict):
            keep = {k: v for k, v in d["enabledPlugins"].items() if v is True and self.plugin_ok(k)}
            for k, v in d["enabledPlugins"].items():
                if v is True and k not in keep:
                    self.left_out.append(f"enabledPlugins.{k} (not installed)")
            d["enabledPlugins"] = keep
            markets = {k.split("@", 1)[1] for k in keep if "@" in k}
            if isinstance(d.get("extraKnownMarketplaces"), dict):
                d["extraKnownMarketplaces"] = {k: v for k, v in d["extraKnownMarketplaces"].items() if k in markets}
        return d

    def rules(self, profs):
        rules = [r for p in profs for r in p["rules"]]
        if not rules:
            return
        self.say("\n== Agent rules (merged into yours, never replaced)")
        for r in rules:
            if r.get("tool") and not self.tool_ok(r["tool"]):
                self.say(f"  - skip {r['dest']}: {self.why_not(r['tool'])}")
                continue
            if not self.harness:
                self.say(f"  - needs --harness: merge {r['src']} into {r['dest']}")
                continue
            self.merge_rule(r)

    def merge_rule(self, r):
        src, dest = KIT / r["src"], real(expand(r["dest"]))
        kept, skipped = [], []
        if r["how"] == "merge-json":
            try:
                mine = json.loads(dest.read_text()) if dest.exists() else {}
                if not isinstance(mine, dict):
                    raise ValueError("not a JSON object")
            except (OSError, ValueError) as e:
                self.say(f"  ! {r['dest']}: cannot read it as JSON ({e}); fix it, then run again")
                self.failed.append(f"rule {r['dest']}: unreadable")
                return
            kit = self.render_settings(json.loads(src.read_text()))
            merged, added = copy.deepcopy(mine), []
            merge_values(merged, kit, "", added, kept, skipped, self.hook_missing)
            hold_unrestricted(mine, merged, added, skipped)
            new = json.dumps(merged, indent=2) + "\n"
            if added and not dest.exists():
                added.insert(0, "(new file)")
        else:
            new, added, kept = merge_markdown(src.read_text(), dest.read_text() if dest.exists() else None, r["src"])
        for x in skipped:
            self.left_out.append(f"{r['dest']} {x}")
        if not added:
            self.say(f"  = {r['dest']}: nothing to add" + (f" (your own value kept for {len(kept)} key(s) or "
                                                          "section(s))" if kept else ""))
            return
        what = ", ".join(added[:6]) + (f" and {len(added) - 6} more" if len(added) > 6 else "")
        self.backup_once(dest)      # once: a plugin install this run may already have copied the original
        if self.dry:
            self.say(f"  + would merge into {r['dest']}: {what}")
            return
        write_atomic(dest, new)
        self.say(f"  + merged into {r['dest']}: {what}")
        if kept:
            self.say(f"    your own value kept for: {', '.join(kept[:6])}{' ...' if len(kept) > 6 else ''}")

    # ------------------------------------------------------------------ services
    def services(self, profs):
        svcs = [s for p in profs for s in p["services"]]
        if not svcs:
            return
        self.say("\n== Services")
        for s in svcs:
            if s["linux_only"] and self.family == "macos":
                self.say(f"  - {s['name']}: Linux only, skipped on macOS")
            elif s["optional"] and not self.chosen_has(s["name"]):
                self.say(f"  - {s['name']}: optional, not chosen")
            elif not self.tool_ok(s["tool"]):
                self.say(f"  - {s['name']}: skipped, {self.why_not(s['tool'])}")
            elif self.unmet(s["tool"]):
                # a tool already present still waits for what it requires (heimdall's listeners for the server
                # lane's firewall), even when that failed this run or --only left it out
                self.say(f"  - {s['name']}: not started, {self.why_not(self.unmet(s['tool'])[0])}")
            elif s["kind"] == "compose":
                self.compose(s)
            else:
                self.say(f"  {s['name']}: {s['unit']} ({s['scope']} unit, set up by {s['tool']})")
                self.health(s)

    def compose(self, s):
        """docker compose in the service's project_dir, never on the template in the clone. A missing project gets
        the template's files once (env_example as .env); a .env made this run waits for you to fill it in."""
        d = expand(s["project_dir"][self.family])
        tpl = KIT / s["compose"]
        self.say(f"  {s['name']}: compose project {tilde(d)} (service {s['service']})")
        if s.get("env_file"):
            vals = env_file(s["env_file"])
            need = [v for v in re.findall(r"\$\{(\w+):\?", tpl.read_text()) if not (os.environ.get(v) or vals.get(v))]
            if need:
                msg = f"{s['name']}: set {', '.join(sorted(set(need)))} in {s['env_file']}, then run install.sh again"
                self.say(f"  ! {msg}")
                self.notes.append(msg)
                return
        fresh, copies = False, []
        if not (d / tpl.name).exists():
            copies = [(f, d / f.name) for f in sorted(tpl.parent.iterdir()) if f.is_file() and not (d / f.name).exists()]
        if s.get("env_example") and not (d / ".env").exists() and str(d) not in self.fresh_env:
            copies.append((KIT / s["env_example"], d / ".env"))
            fresh = True
        if str(d) in self.fresh_env:
            self.say(f"  ! waits for the same {tilde(d / '.env')}")
            return
        if copies:
            if self.dry:
                lane = [r for r, st in self.status.items() if r.startswith("server-lane-") and st == "would install"]
                if lane and not d.exists():
                    self.say(f"  ({lane[0]} sets this folder up first; if it does not:)")
                self.say(f"  + would copy into {tilde(d)}: " + ", ".join(dst.name for _, dst in copies))
            else:
                if not self.make_dir(d):
                    self.failed.append(f"service {s['name']}: cannot create {d}")
                    return
                for src, dst in copies:
                    shutil.copy2(src, dst)
                    if dst.name == ".env":
                        os.chmod(dst, 0o600)
                self.say(f"  + copied into {tilde(d)}: " + ", ".join(dst.name for _, dst in copies))
        if fresh:
            self.fresh_env.add(str(d))
            msg = (f"{s['name']}: fill in {tilde(d / '.env')} (copied from {s['env_example']}), "
                   "then run install.sh again to start it")
            self.say(f"  ! {msg}")
            self.notes.append(msg)
            return
        cmd = "docker compose" + (f" --env-file {q(str(expand(s['env_file'])))}" if s.get("env_file") else "") \
            + "".join(f" --profile {q(p)}" for p in s["compose_profiles"]) + f" up -d {q(s['service'])}"
        if not self.exec(cmd, cwd=d):
            self.failed.append(f"service {s['name']}: docker compose up failed")
            return
        for post in POST_UP.get((s["compose"], s["service"]), []):
            if not self.exec(post, why="load the model the service serves (its health check passes without it)",
                             cwd=d):
                self.failed.append(f"service {s['name']}: post-start step failed")
                return
        self.health(s)

    def make_dir(self, d):
        if d.is_dir():
            return True
        parent = d
        while not parent.exists():
            parent = parent.parent
        if os.access(parent, os.W_OK):
            d.mkdir(parents=True)
            return True
        user = pwd.getpwuid(os.geteuid()).pw_name
        return self.exec(f"sudo mkdir -p {q(str(d))} && sudo chown {q(user)} {q(str(d))}",
                         why=f"make {d} for the compose project, owned by you")

    def health(self, s):
        cmd = s["health"]
        if self.dry:
            self.say(f"  + health check: {cmd}")
            return True
        env = child_env(hosts_env())
        deadline = time.time() + HEALTH_WAIT
        while True:
            rc, out = sh(cmd, timeout=15, capture=True, env=env)
            if rc == 0:
                self.say(f"  = {s['name']} is healthy")
                return True
            if time.time() >= deadline:
                self.say(f"  ! {s['name']} is not healthy after {HEALTH_WAIT}s: {out.strip()[-200:]}")
                self.failed.append(f"service {s['name']}: health check failed ({cmd})")
                return False
            time.sleep(3)

    # ------------------------------------------------------------------ automations
    def automations(self, profs):
        autos, seen = [], set()
        for p in profs:
            for a in p["automations"]:
                if a["unit"] not in seen:
                    seen.add(a["unit"])
                    autos.append((p["name"], a))
        if not autos:
            return
        self.say("\n== Scheduled jobs and units")
        reload, enable = set(), []
        for pname, a in autos:
            if a["linux_only"] and self.family == "macos":
                self.say(f"  - {a['name']}: Linux only (systemd), skipped on macOS")
                continue
            if not self.tool_ok(a["tool"]):
                self.say(f"  - {a['name']}: skipped, {self.why_not(a['tool'])}")
                continue
            changed = self.install_units(a)
            if changed is None:
                self.failed.append(f"unit {a['name']}: could not install its unit files")
                continue
            if changed:
                reload.add(a["scope"])
            self.register(pname, a)
            if a["enable"]:
                enable.append(a)
            else:
                flag = "--user " if a["scope"] == "user" else ""
                pre = "" if a["scope"] == "user" else "sudo "
                unit = Path(a["unit"]).name
                if sh(f"systemctl {flag}is-enabled --quiet {q(unit)}", capture=True, timeout=10, env=self.env)[0] == 0:
                    self.say(f"  = {unit} is enabled (you turned it on; the kit leaves it off by default)")
                else:
                    self.notes.append(f"{a['name']}: left off after the install; turn it on with "
                                      f"{pre}systemctl {flag}enable --now {unit}")
        for scope in sorted(reload):
            cmd = "systemctl --user daemon-reload" if scope == "user" else "sudo systemctl daemon-reload"
            if not self.exec(cmd, why=None if scope == "user" else "load the new system units"):
                self.failed.append(f"systemctl {scope} daemon-reload failed")
        linger = False
        for a in enable:
            unit = Path(a["unit"]).name
            flag, pre = ("--user ", "") if a["scope"] == "user" else ("", "sudo ")
            rc, _ = sh(f"systemctl {flag}is-enabled --quiet {q(unit)}", capture=True, timeout=10, env=self.env)
            if rc == 0:
                self.say(f"  = {unit} is already enabled")
            elif not self.exec(f"{pre}systemctl {flag}enable --now {q(unit)}",
                               why=None if pre == "" else f"enable the system unit {unit}"):
                self.failed.append(f"unit {a['name']}: enable failed")
            linger |= a["scope"] == "user"
        if linger:
            self.linger()

    def install_units(self, a):
        """Copy each unit file into its systemd folder. True = something new, False = all there, None = failed."""
        if a["scope"] == "user":
            base, sudo = home() / ".config/systemd/user", False
        else:
            base, sudo = Path("/etc/systemd/system"), True
        changed = False
        for f in a["files"]:
            src, dest = KIT / f, base / Path(f).name
            if (dest.exists() and dest.resolve() == src.resolve()) or same("copy", src, dest):
                self.say(f"  = present: {tilde(dest)}")
                continue
            if dest.exists() or dest.is_symlink():
                self.say(f"  = kept: {tilde(dest)} is yours and differs from the kit's {f}")
                self.kept.append(tilde(dest))
                continue
            changed = True
            if self.dry:
                self.say(f"  + would copy {f} -> {tilde(dest)}")
            elif sudo:
                if not self.exec(f"sudo install -D -m 644 {q(str(src))} {q(str(dest))}",
                                 why=f"system unit for {a['name']}"):
                    return None
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                os.chmod(dest, 0o644)
                self.say(f"  + copy {f} -> {tilde(dest)}")
        return changed

    def linger(self):
        user = pwd.getpwuid(os.geteuid()).pw_name
        rc, out = sh(f"loginctl show-user {q(user)} --property=Linger --value", capture=True, timeout=10, env=self.env)
        if rc == 0 and out.strip() == "yes":
            self.say("  = lingering is on (user units run without a login)")
            return
        why = "user units keep running when nobody is logged in (needed on a server)"
        if not self.exec(f"loginctl enable-linger {q(user)} 2>/dev/null || sudo loginctl enable-linger {q(user)}",
                         why=why):
            self.failed.append("loginctl enable-linger failed")

    def register(self, pname, a):
        """A REGISTRY.md row with the kill switch, unless the unit already has one."""
        reg = real(home() / "AI_Projects/_system/automations/REGISTRY.md")
        unit = Path(a["unit"]).name   # the file name, not the stem: "retain" is also in "retained"
        text = reg.read_text() if reg.exists() else None
        if text is not None and re.search(rf"(?<![\w.@-]){re.escape(unit)}(?![\w.@-])", text):
            self.say(f"  = registry: {unit} already has a row")
            return
        host = platform.node().split(".")[0] or "this machine"
        state = "enabled" if a["enable"] else "installed, off until you enable it"
        kill = a["kill_switch"].replace("|", "\\|")
        row = (f"| {a['name']} (added {TS[:8]} by install.sh, profile {pname}) | {schedule(a['unit'])} on **{host}** "
               f"({a['scope']} `{Path(a['unit']).name}`, {state}) | local | {a['tool']}: "
               f"{', '.join('`' + Path(f).name + '`' for f in a['files'])} | {kill} |")
        if self.dry:
            self.say(f"  + would add a row to {tilde(reg)} for {a['name']} (kill switch: {a['kill_switch']})")
            return
        if text is None:
            text = REGISTRY_HEAD
        else:
            self.backup_once(reg)
        write_atomic(reg, text.rstrip("\n") + "\n" + row + "\n")
        self.say(f"  + registry row for {a['name']} in {tilde(reg)}")

    # ------------------------------------------------------------------ the run
    def run(self):
        names = self.a.profiles
        profs = [profile(n) for n in names]
        if not self.prerequisites(profs):
            return 2
        rows = plan(names, self.os, only=self.a.only, env=self.env)
        self.rows = {r["id"]: r for r in rows}
        self.print_plan(rows, names)
        cand = [r["id"] for r in rows if r["optional"] and not r["skip"] and r["state"] != "ok"]
        cand += [s["name"] for p in profs for s in p["services"] if s["optional"] and s["name"] not in cand
                 and self.included(s["tool"]) and not (s["linux_only"] and self.family == "macos")]
        if not self.dry and not self.a.yes and not sys.stdin.isatty():
            self.say("Nothing installed: no terminal to ask on. Re-run with --yes to accept the plan above.")
            return 2
        self.choose(cand)
        todo = [r for r in rows if not r["skip"] and r["state"] != "ok" and self.included(r["id"])]
        if self.os == "macos" and not self.dry and not shutil.which("brew", path=self.env["PATH"]) and any(
                isinstance(r["cmd"], str) and r["cmd"].startswith("brew ") for r in todo):
            self.say('prerequisite: Homebrew is missing. Install it first (https://brew.sh):\n'
                     '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"')
            return 2
        if not self.dry and not self.a.yes:
            n = len([r for r in todo if not r["optional"] or self.chosen_has(r["id"])])
            ans = input(f"Install {n} tool(s), then set up the rules, services and units below? [y/N]: ").strip()
            if ans.lower() not in ("y", "yes"):
                self.say("Nothing installed.")
                return 2
        reasons = self.sudo_reasons(profs)
        if reasons and not self.dry and os.geteuid() != 0:
            self.say("sudo is needed for:\n" + "\n".join(f"  - {x}" for x in reasons))
            if sh("sudo -v")[0] != 0:
                self.say("sudo was refused; nothing installed.")
                return 2
        if self.dry:
            self.say("\nDry run: what each missing tool would do")
        for r in rows:
            self.install_tool(r)
            if r["id"] == "docker" and self.tool_ok("docker"):
                self.docker_group()
        self.rules(profs)
        self.services(profs)
        self.automations(profs)
        return self.summary()

    def summary(self):
        self.say("\n== Summary")
        groups = {}
        for tid, st in self.status.items():
            groups.setdefault(st, []).append(tid)
        for st in ("installed", "would install", "present", "manual", "needs --harness", "needs you",
                   "would keep yours", "not chosen", "skipped", "blocked", "failed"):
            if st in groups:
                extra = ""
                if st in ("blocked", "failed", "skipped"):
                    extra = "  (" + "; ".join(f"{t}: {self.why[t]}" for t in groups[st] if self.why.get(t)) + ")"
                self.say(f"  {st:<16}{len(groups[st]):>3}  {', '.join(groups[st])}{extra if extra != '  ()' else ''}")
        if self.backups:
            self.say("Backups made (restore by moving one back):\n" + "\n".join(f"  {b}" for b in self.backups))
        if self.kept:
            self.say("Kept as they are (yours, not the kit's):\n" + "\n".join(f"  {k}" for k in self.kept))
        if self.left_out:
            self.say("Left out of the merged agent config:\n" + "\n".join(f"  {x}" for x in self.left_out))
        if self.notes:
            self.say("To do by hand:\n" + "\n".join(f"  - {n}" for n in self.notes))
        if str(home() / ".local/bin") not in os.environ.get("PATH", "").split(":"):
            self.say("Add ~/.local/bin to your PATH: the kit's commands are linked there.")
        if self.dry:
            self.say(f"Dry run: nothing was changed. Files that would be backed up first: {self.would_backup}. "
                     f"Your own files kept as they are: {len(self.kept)}. Overwritten without a backup: 0.")
            for tid, st in self.status.items():   # a delegated server setup counts its own writes, not these
                if tid.startswith("server-lane-") and st == "would install":
                    for s in self.rows[tid]["tool"].get("install", []):
                        if s["do"] == "run" and self.os in s.get("os", OSES):
                            cmd = re.sub(r"^sudo ", "", s["cmd"])
                            self.say(f"Not counted above: {tid} runs `{s['cmd']}`, which manages system files (sshd, "
                                     f"firewall, units) itself; see what it would write with `{cmd} --dry-run`.")
            return 0
        if self.failed:
            self.say("Failed:\n" + "\n".join(f"  - {x}" for x in self.failed))
            return 1
        self.say("Done. Check any time with: install/doctor.sh --profile " + ",".join(self.a.profiles))
        return 0


# ---------------------------------------------------------------------------------------------- doctor
def doctor(a):
    env = child_env()
    if a.profiles:
        rows = plan(a.profiles, a.os, env=env)
        profs = [profile(n) for n in a.profiles]
    else:
        rows = []
        tools = manifest()
        states = probe([t for t in tools if resolve(t, a.os)], env)
        for t in tools:
            rows.append({"id": t["id"], "tool": t, "optional": True, "state": states.get(t["id"]),
                         "skip": None if resolve(t, a.os) else f"not for {a.os}"})
        profs = []
    bad = 0
    for r in rows:
        if r["skip"]:
            print(f"{'skip':<14}{r['id']:<30} {r['skip']}")
            continue
        req = a.profiles and not r["optional"]
        print(f"{r['state']:<14}{r['id']:<30} {r['tool']['name']}{'' if req or not a.profiles else ' (optional)'}")
        if req and r["state"] != "ok":
            bad += 1
    family = "macos" if a.os == "macos" else "linux"
    ok_tools = {r["id"] for r in rows if r["state"] == "ok"}
    henv = child_env(hosts_env())
    for p in profs:
        for s in p["services"]:
            if s["linux_only"] and family == "macos":
                continue
            if s["tool"] not in ok_tools:
                print(f"{'skip':<14}{'service ' + s['name']:<30} its tool {s['tool']} is not installed")
                continue
            rc, _ = sh(s["health"], timeout=15, capture=True, env=henv)
            print(f"{'ok' if rc == 0 else 'down':<14}{'service ' + s['name']:<30} {s['health']}"
                  f"{' (optional)' if s['optional'] else ''}")
            bad += rc != 0 and not s["optional"]
        for au in p["automations"]:
            if au["linux_only"] and family == "macos":
                continue
            unit = Path(au["unit"]).name
            flag = "--user " if au["scope"] == "user" else ""
            rc, out = sh(f"systemctl {flag}is-enabled {q(unit)} 2>/dev/null", capture=True, timeout=10, env=env)
            st = out.strip().splitlines()[-1] if out.strip() else "not-installed"
            print(f"{('on' if rc == 0 else 'off' if st in ('disabled', 'static', 'linked') else 'not-installed'):<14}"
                  f"{'unit ' + unit:<30} kill switch: {au['kill_switch']}")
    if not a.profiles:
        print("(no --profile: every tool in the manifest, for information; --profile <name> checks one setup)")
        return 0
    print(f"{'PASS' if not bad else 'FAIL'}: {bad} required item(s) missing or down")
    return 1 if bad else 0


# ---------------------------------------------------------------------------------------------- arguments
def parse(argv):
    ap = argparse.ArgumentParser(prog="lib.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("install", "doctor", "plan"):
        p = sub.add_parser(name)
        p.add_argument("--profile", dest="profile", default="")
        p.add_argument("--os", choices=OSES)
        if name == "install":
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--yes", action="store_true")
            p.add_argument("--harness", action="store_true")
            p.add_argument("--optional", default="")
            p.add_argument("--only", default="")
            p.add_argument("--ignore-hardware", action="store_true")
    sub.add_parser("detect-os")
    a = ap.parse_args(argv)
    if a.cmd == "detect-os":
        return a
    a.profiles = [x.strip() for x in a.profile.split(",") if x.strip()]
    unknown = [x for x in a.profiles if x not in PROFILE_NAMES]
    if unknown:
        raise UsageError(f"unknown profile(s) {', '.join(unknown)}; pick from {', '.join(PROFILE_NAMES)}")
    if a.cmd != "doctor" and not a.profiles:
        raise UsageError(f"--profile is required ({', '.join(PROFILE_NAMES)}; several: --profile odin,telegram)")
    detected = detect_os()
    if not a.os:
        if detected == "unknown":
            raise UsageError("this OS is not one the kit knows (arch, debian, macos); pass --os with --dry-run "
                             "to see the plan, then install the tools by hand")
        a.os = detected
    elif a.cmd == "install" and not a.dry_run and detected not in ("unknown", a.os):
        raise UsageError(f"--os {a.os} but this machine is {detected}: use --os with --dry-run only")
    if a.cmd == "install":
        a.only = [x.strip() for x in a.only.split(",") if x.strip()]
    return a


def main(argv=None):
    try:
        a = parse(sys.argv[1:] if argv is None else argv)
        if a.cmd == "detect-os":
            print(detect_os())
            return 0
        if a.cmd == "plan":
            rows = plan(a.profiles, a.os)
            print(json.dumps([{"id": r["id"], "method": r["method"], "cmd": r["cmd"], "present": r["state"] == "ok",
                               "optional": r["optional"], "skip": r["skip"]} for r in rows], indent=2))
            return 0
        if a.cmd == "doctor":
            return doctor(a)
        return Installer(a).run()
    except UsageError as e:
        print(f"install: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        return 1
    except EOFError:
        print("\nno answer; nothing more installed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
