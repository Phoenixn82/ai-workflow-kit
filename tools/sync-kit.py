#!/usr/bin/env python3
"""Rebuild framework/ from an allowlist of live sources: stage, trim, scrub, syntax-check, gitleaks; --apply replaces it."""
import argparse
import fnmatch
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


EXCLUDE_DIRS = ("__pycache__", "node_modules", ".git", "logs", "state", "tests", "secrets", "_staging",
                "voice-corpus", ".secrets", "claude_vault", ".codex", ".venv*", "venv", ".pytest_cache",
                ".ruff_cache", "*.egg-info", ".runtime", ".superpowers", "evidence", ".codex-session-changes",
                ".codex-claude-handoff", "_archive", "out", "runs", "deployed")
EXCLUDE_FILES = ("*.log", ".claude-codex-log.md", "desktop.ini", "Thumbs.db", ".DS_Store", "*.bak*", "*.pre-*",
                 "*.win-bak", "*.pyc", "*.db", "*.sqlite*", "*.jsonl", "denylist.local*",
                 "test_public_push_gate_allowlist.py", "tool-parity-report.*", "client-*", "*.proposed*", "*.obsolete",
                 "settings.json.hooks.*", "phase_gate_register.py")
# Data files a tool needs that an EXCLUDE_FILES pattern would otherwise drop (the corpus is not a log).
KEEP_FILES = ("corpus.jsonl",)
# Binary files are never exported (the scrub cannot read them); an image must be drawn as SVG.
BINARY_OK = ()


def private_rules():
    path = os.environ.get("SCRUB_RULES") or str(Path(__file__).with_name("scrub-rules.local.json"))
    try:
        return json.loads(Path(path).read_text())
    except OSError:
        return {}


def excluded(name, patterns):
    if name in KEEP_FILES:
        return False
    patterns = tuple(patterns) + tuple(private_rules().get("excludeNames", ()))
    return any(fnmatch.fnmatch(name.lower(), pattern.lower()) for pattern in patterns)


def report_symlink(path):
    print(f"warning: skip symlink: {path}")


def symlink_component(path):
    """Return a symlink in path (including an ancestor) without resolving it."""
    path = Path(path).absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(mode):
            return current
    return None


def safe_mkdir(path, mode=0o777):
    """Create a directory only through real directory ancestors."""
    path = Path(path).absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            os.mkdir(current, mode if current == path else 0o777)
            continue
        if stat.S_ISLNK(st.st_mode):
            report_symlink(current)
            return False
        if not stat.S_ISDIR(st.st_mode):
            raise NotADirectoryError(current)
    return True


def regular_file(path):
    linked = symlink_component(path)
    if linked:
        report_symlink(linked)
        return None
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    return st if stat.S_ISREG(st.st_mode) else None


def real_directory(path):
    linked = symlink_component(path)
    if linked:
        report_symlink(linked)
        return False
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def walk_error(error):
    raise error


def copy_file(source, destination):
    source_st = regular_file(source)
    if source_st is None:
        return False
    if not safe_mkdir(destination.parent):
        return False
    linked = symlink_component(destination)
    if linked:
        report_symlink(linked)
        return False
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, os.O_RDONLY | nofollow)
    try:
        source_st = os.fstat(source_fd)
        if not stat.S_ISREG(source_st.st_mode):
            return False
        destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | nofollow,
                                 stat.S_IMODE(source_st.st_mode))
        try:
            with os.fdopen(source_fd, "rb", closefd=False) as source_file:
                with os.fdopen(destination_fd, "wb", closefd=False) as destination_file:
                    shutil.copyfileobj(source_file, destination_file)
            os.chmod(destination_fd, stat.S_IMODE(source_st.st_mode))
            os.utime(destination_fd, ns=(source_st.st_atime_ns, source_st.st_mtime_ns))
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)
    return True


def included_tree(source, directories=(), files=()):
    """Yield safe files in a tree; directory and file exclusions stay separate."""
    for current, names, leaf_names in os.walk(source, onerror=walk_error):
        current_path = Path(current)
        kept_dirs = []
        for name in names:
            child = current_path / name
            if symlink_component(child):
                report_symlink(child)
            elif not excluded(name, directories):
                kept_dirs.append(name)
        names[:] = kept_dirs
        for name in leaf_names:
            child = current_path / name
            if regular_file(child) is not None and not excluded(name, files):
                yield child


def scrub(script, framework):
    completed = subprocess.run([sys.executable, str(script), str(framework)], text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if completed.returncode == 0:
        print(completed.stdout, end="")
        return
    # scrub.py can include matched text in a denylist failure; keep failure output safe.
    print("[scrub] RESULT: BLOCKED")
    rule = "scrub-guard"
    for line in completed.stdout.splitlines():
        if "real username" in line:
            rule = "username-guard"
        elif "real credential" in line:
            rule = "secret-guard"
        elif "forbidden private path" in line:
            rule = "path-guard"
        elif "identity residue" in line:
            rule = "verify-denylist"
        if line.startswith("    "):
            path, _, rest = line.strip().partition("   [")
            label = rest.partition("]")[0] if rest else rule
            print(f"    {path} [{label}]")
    raise RuntimeError("scrub blocked; staged output was not applied")


# Tools that are never exported. Their names must not survive anywhere in the stage: not in executables (the hook
# dispatcher's entries), settings, units or instructions. Personal/client tool names live in the PRIVATE rules
# ("excludedTools": [...]); sections and bullets whose text matches a private "dropSectionTerms" regex go too.
EXCLUDED_TOOLS = ()


def excluded_tools_re():
    names = EXCLUDED_TOOLS + tuple(private_rules().get("excludedTools", ()))
    alts = "|".join(re.sub(r"\\-|_", "[-_]", re.escape(n.lower())) for n in sorted(set(names), key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z0-9_-])(?:{alts})(?![A-Za-z0-9])", re.IGNORECASE)


def drop_terms_re():
    terms = [excluded_tools_re().pattern] + [t for t in private_rules().get("dropSectionTerms", ())]
    return re.compile("|".join(f"(?:{t})" for t in terms), re.IGNORECASE)


def text_files(framework):
    for path in sorted(framework.rglob("*")):
        if path.is_file() and b"\x00" not in path.read_bytes()[:8192]:
            yield path


def is_shell(path):
    return path.suffix == ".sh" or path.read_bytes()[:64].startswith((b"#!/bin/bash", b"#!/usr/bin/env bash", b"#!/bin/sh"))


def strip_shell_entries(text, pattern):
    """Drop comment lines and quoted array entries that name an excluded tool, e.g. a dispatcher table row
    "<path>/private-tool-watcher.sh"; a trailing ') ;;' is kept so the case arm still parses."""
    out = []
    for line in text.splitlines(keepends=True):
        if pattern.search(line):
            if line.lstrip().startswith("#"):
                continue
            m = re.fullmatch(r'(\s*)(?:\w+=\(\s*)?"[^"]*"(\s*\)\s*;;)?\s*', line.rstrip("\n"))
            if m:
                prefix = re.match(r"\s*(?:\w+=\(\s*)?", line).group(0)
                if m.group(2) or prefix.strip():
                    out.append(prefix + (m.group(2) or "").strip() + "\n")
                continue
        out.append(line)
    return "".join(out)


def strip_markdown(text, pattern):
    """Drop headed sections whose heading matches, then parentheticals, list items and table rows that match.
    Works whatever the heading is called (a renamed private section must not slip through).
    Lines inside code fences are never read as headings or list items."""
    kept, skip_level, fence = [], 0, False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        heading = None if fence else re.match(r"(#{1,6})\s", line)
        if heading:
            level = len(heading.group(1))
            if not (skip_level and level > skip_level):
                skip_level = level if pattern.search(line) else 0
        if not skip_level:
            kept.append(line)
    text = re.sub(r"\s?\([^()\n]*\)", lambda m: "" if pattern.search(m.group(0)) else m.group(0), "".join(kept))
    out, item_indent, fence = [], None, False
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped.startswith(("```", "~~~")):
            fence = not fence
        if item_indent is not None:
            if stripped and indent > item_indent:
                continue          # continuation or nested item of a dropped item
            item_indent = None
        if not fence and re.match(r"([-*+]|\d+\.)\s", stripped) and pattern.search(line):
            item_indent = indent
            continue
        if not fence and stripped.startswith("|") and pattern.search(line):
            continue
        out.append(line)
    return "".join(out)


def strip_excluded(framework):
    """Staging-time removal of excluded-tool wiring and client-only instruction text (before the scrub)."""
    names, terms = excluded_tools_re(), drop_terms_re()
    for path in text_files(framework):
        text = path.read_text(errors="surrogateescape")
        if path.suffix == ".md":
            new = strip_markdown(text, terms)
        elif is_shell(path):
            new = strip_shell_entries(text, names)
        else:
            continue
        if new != text:
            path.write_text(new, errors="surrogateescape")


def assert_no_excluded_tools(framework):
    """r1 #8: no excluded tool name may appear in any exported file (executables, settings, units, instructions)."""
    names, hits = excluded_tools_re(), []
    for path in text_files(framework):
        for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            m = names.search(line)
            if m:
                hits.append(f"{path.relative_to(framework)}:{number} names excluded tool '{m.group(0)}'")
    if hits:
        raise RuntimeError("excluded tools are still referenced in the export:\n  " + "\n  ".join(hits))


UNIT_SUFFIXES = (".service", ".timer", ".slice", ".path", ".socket", ".target", ".mount")
BUILTIN_SLICES = {"system.slice", "user.slice", "machine.slice", "app.slice", "background.slice", "session.slice", "-.slice"}
PATH_REF = re.compile(r"(?:\$\{HOME\}|\$HOME|%h|~|(?<![\w.-])/home/[^/\s\"']+)((?:/[A-Za-z0-9._@+-]+)+)")
SLICE_REF = re.compile(r"(?:--slice[= ]|Slice=)\s*[\"']?([\w@.-]+\.slice)")
SHELL_CMD = r"(?:^|[;&|(`{]|\$\(|\b(?:exec|then|do|else|if|while|until|command|sudo|nohup|xargs)\s)\s*(?:\w+=\S*\s+)*{name}(?![\w.-])"
PY_CMD = (r"""\b(?:run|call|check_call|check_output|Popen|which|execvp?|execlp?)\(\s*\[?\s*["']{name}["']\s*[,)\]]"""
          r"""|(?<![\w\])])\[\s*["']{name}["']\s*,""")


def is_wrapper(path):
    """Files that wire other executables: executables, systemd units and harness settings."""
    if path.suffix in UNIT_SUFFIXES or path.name in ("settings.reference.json", "hooks.json"):
        return True
    return bool(path.stat().st_mode & 0o111) or path.read_bytes()[:2] == b"#!"


def head(path, size):
    with open(path, "rb") as handle:     # ~/.local/bin holds 100+ MB binaries; never read them whole
        return handle.read(size)


def first_party_commands(home):
    """Bare command names a wrapper may call that only exist as first-party files in ~/.local/bin."""
    bindir, found = home / ".local" / "bin", {}
    if not bindir.is_dir():
        return found
    path_dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and Path(d) != bindir]
    for entry in bindir.iterdir():
        name = entry.name
        if len(name) < 3 or "." in name or not os.access(entry, os.X_OK) or entry.is_dir():
            continue
        real = entry.resolve()
        if entry.is_symlink():
            if (home / "AI_Projects") in real.parents:
                found[name] = entry
        elif head(entry, 2) == b"#!" and not any(os.access(Path(d) / name, os.X_OK) for d in path_dirs):
            found[name] = entry
    return found


def assert_wiring(framework, table, home, manifest=None):
    """r1 #6: every first-party executable or unit an exported wrapper references must itself be exported."""
    roots = [home / "AI_Projects", home / ".claude", home / ".codex", home / ".local" / "bin",
             home / ".config" / "systemd" / "user", home / "agentic-os"]
    exported_names = {p.name for p in framework.rglob("*") if p.is_file()}
    commands = first_party_commands(home)
    if manifest is None:
        manifest = json.loads((Path(__file__).resolve().parent.parent / 'install/tools.json').read_text())
    third_party = {t['id'] for t in manifest['tools'] if not t['bundled']}
    commands = {name: path for name, path in commands.items() if name not in third_party}
    installed = {name: framework / target.removeprefix('framework/')
                 for t in manifest['tools'] if t['bundled'] for name, target in t.get('entrypoints', {}).items()}


    def staged(live):
        """Framework path for a live path covered by the allowlist table, else None."""
        best = None
        for source, dest, names in table:
            try:
                rel = live.relative_to(source)
            except ValueError:
                continue
            if rel.parts and (names is None or rel.parts[0] in names):
                if best is None or len(source.parts) > len(best[0].parts):
                    best = (source, framework / dest / rel)
        return best and best[1]

    def runnable(live):     # a script run through its interpreter (`bash x.sh`, `python3 x.py`) needs no +x bit
        return live.is_file() and (live.suffix in UNIT_SUFFIXES + (".sh", ".py") or os.access(live, os.X_OK))

    def ok(live):
        if live.parent == home / ".local/bin" and live.name in installed and installed[live.name].is_file():
            return True
        for candidate in {live, live.resolve()}:
            target = staged(candidate)
            if target is not None and target.exists():
                return True
        # a console script in a project's venv (~/.local/bin/lane -> _tools/lanes/.venv/bin/lane) is rebuilt
        # by installing that project, so the exported pyproject.toml is what has to ship
        venv = next((p for p in live.resolve().parents if p.name.startswith(".venv")), None)
        target = venv and staged(venv.parent / "pyproject.toml")
        return bool(target and target.exists())

    problems = []
    for path in text_files(framework):
        if not is_wrapper(path):
            continue
        text = path.read_text(errors="replace")
        rel = path.relative_to(framework)
        for m in PATH_REF.finditer(text):
            live = home / m.group(1).lstrip("/")
            if live.parent == home / ".local" / "bin" and live.name not in commands:
                continue    # third-party binary or shim (installed by the installer, not exported)
            if any(r == live or r in live.parents for r in roots) and runnable(live) and not ok(live):
                problems.append(f"{rel} references {m.group(0)}, which is not in the export")
        for m in SLICE_REF.finditer(text):
            if m.group(1) not in BUILTIN_SLICES and m.group(1) not in exported_names:
                problems.append(f"{rel} runs in {m.group(1)}, which is not in the export")
        pattern = SHELL_CMD if is_shell(path) else PY_CMD if path.suffix == ".py" or text.startswith("#!/usr/bin/env python") else None
        if pattern is None:
            continue
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        for name, entry in commands.items():
            if name in exported_names or ok(entry):
                continue
            if re.search(pattern.replace("{name}", re.escape(name)), code, re.MULTILINE):
                problems.append(f"{rel} calls '{name}' (first-party ~/.local/bin), which is not in the export")
    if problems:
        raise RuntimeError("exported wrappers reference files that are not exported:\n  " + "\n  ".join(sorted(set(problems))))


# (live source, path inside framework/, top-level names to take or None for everything).
# This list IS the allowlist: nothing outside it is ever exported.
# Live folder/file names that differ from their published name live in the PRIVATE rules file
# ("sourceAliases": {"published-name": "live-name"}), so this committed file names nothing personal.
def live(name):
    return private_rules().get("sourceAliases", {}).get(name, name)


UNSHIPPED_SKILLS = ()
GENERIC_HINTS = {
    "vercel": "Use the official npx route (`npx -y vercel@latest <cmd> --scope <your-team>`).",
    "google-workspace": "Use document/spreadsheet plugins for local files. Mail connectors draft only, never send.",
}


def trim_framework(framework):
    """Drop index rows for skills that are not exported, and private entries from the tool-parity registry."""
    for name in ("SKILLS_INDEX.md", "DECISION_MAP.md", "AGENTS.md"):
        path = framework / name
        lines = path.read_text().splitlines(keepends=True)
        path.write_text("".join(l for l in lines if not (l.startswith("|") and any(
            f"**{s}**" in l or f"`{s}`" in l or f"~~`{s}`" in l for s in UNSHIPPED_SKILLS))))
    for block in private_rules().get("dropBlocks", ()):   # owner-derived worked examples
        path = framework / block["file"]
        text = path.read_text()
        a, b = text.find(block["start"]), text.find(block["end"])
        if a < 0 or b < a:
            raise RuntimeError(f"dropBlocks markers not found in {block['file']}")
        path.write_text(text[:a] + text[b:])
    # The hook's live literal home path would become an unexpanded "$HOME" Python string after scrubbing.
    common = framework / "harness/hooks/web-taint/common.py"
    if common.exists():
        text = common.read_text()
        text = re.sub(r"^sys\.path\.insert\(0, os\.environ\.get\('WEB_PKG_DIR'.*$",
                      "sys.path.insert(0, os.environ.get('WEB_PKG_DIR') or str(Path.home() / 'AI_Projects/_tools/web'))",
                      text, flags=re.M)
        common.write_text(text)
    path = framework / "_system" / "tool-parity" / "registry.json"
    registry = json.loads(path.read_text())
    drop = set(private_rules().get("dropRegistryTools", ()))
    registry["priority_tools"] = [t for t in registry["priority_tools"] if t.get("name") not in drop]
    for tool in registry["priority_tools"]:
        if tool.get("name") in GENERIC_HINTS:
            tool["repair_hint"] = GENERIC_HINTS[tool["name"]]
    path.write_text(json.dumps(registry, indent=2) + "\n")


HOOKS = ["secret-firewall", "aos-lock-read-fingerprint.sh", "codex-activity-watcher.sh", "codex-claude-shared-log.sh",
         "codex-handoff-watcher.sh", "codex-session-manifest.sh", "codex-shared-log-codex.sh", "github-publish-nudge.sh",
         "herdr-agent-state.sh", "herdr-quiet-diffs.py", "test_herdr_quiet_diffs.py", "hook_event.py", "hook-event.sh",
         "workstation-model-guard.py", "webcred-guard.sh", "dispatch", "route-guard.py", "camera-guard.py"]


def sources(aip, claude, home):
    T = aip / "_tools"
    return [
        (aip / "_system" / "skills", "_system/skills", None),
        (aip / "_system" / "tool-parity", "_system/tool-parity", None),
        (aip / "_system" / "automations", "_system/automations", ["README.md", "preflight.py", "record_spend.py"]),
        (aip, ".", ["AGENTS.md", "CLAUDE.md", "DECISION_MAP.md", "SKILLS_INDEX.md", "RELIABILITY_STANDARD.md"]),
        (aip / ".claude" / "workflows", ".claude/workflows", ["open-loops.js"]),
        (T / "web", "_tools/web", ["web", "skill", "scripts", "courier", "pyproject.toml", "uv.lock", ".gitignore"]),
        (T / "hermes-kit", "_tools/hermes-kit", None),
        (T / "helper-wall", "_tools/helper-wall", ["helper-wall"]),
        (home / ".hermes" / "agent-hooks", "_tools/hermes-kit/agent-hooks", ["route-guard.py"]),
        (T / "hermes-loki", "_tools/hermes-loki", ["loki-llm-gate", "loki-llm-gate.service", "desk-shot", "desk-gui"]),
        (T / "thor-wake-proxy", "_tools/thor-wake-proxy", ["thor-wake-proxy", "thor-wake-proxy.service"]),
        (T / "lanes", "_tools/lanes", ["lanes", "config", "README.md", "pyproject.toml", "uv.lock", ".gitignore"]),
        (T / "review-queue", "_tools/review-queue", ["review_queue", "README.md", "SPEC.md", "pyproject.toml", "uv.lock", ".gitignore"]),
        (T / "open-loops", "_tools/open-loops", None),
        (T / "secret-scrub", "_tools/secret-scrub", ["scrub.py"]),
        (T / "transcript-prune", "_tools/transcript-prune", None),
        (T / "vault-rag", "_tools/vault-rag", ["vault_rag.py", "reindex.sh", "hook-user-prompt.sh", "install.sh",
                                               "vault-recall", "systemd", "heimdall", "README.md", ".gitignore"]),
        (T / "lane-intel", "_tools/lane-intel", ["apply_lanes.py", "backfill_claims.py", "common.py", "install.sh", "PROMPT-summarize.md",
                                                 "propose.py", "README.md", "route.py", "run.sh", "slopalytics.py",
                                                 "stance.py", "summarize.py", "theo_watch.py", "units", "whisper_thor.py",
                                                 "x_watch.py"]),
        (T / "vault-lint", "_tools/vault-lint", ["vault_lint.py", "README.md"]),
        (T / "weekly-review", "_tools/weekly-review", ["collect.py", "install.sh", "PROMPT.md", "README.md", "run.sh",
                                                       "units", "VERIFY.md"]),
        (T / "harness-lint", "_tools/harness-lint", ["harness_lint.py"]),
        (T / "retention", "_tools/retention", ["install.sh", "PATCHES.md", "README.md", "repatch.py", "retain.py", "units"]),
        (T / "telegram", "_tools/telegram", ["tg", "tg-admin", "tg-odin", "heimdall-alert", "install.sh", "README.md",
                                             "README-admin.md", "skill"]),
        (T / "x-scroll", "_tools/x-scroll", ["xscroll.py", "extract.js", "mark.js", "scroll.js", "schema.json",
                                             "PROMPT-digest.md", "README.md", "install.sh", "units", "third_party", "tests"]),
        (T / "fleet-wake", "_tools/fleet-wake", ["fleet-wake", "fleet-wake.service", "fleet-wake-shutdown@.service",
                                                 "fleet-wake.timer", "thor"]),
        (T / "fleet-probes", "_tools/fleet-probes", ["disk-probe", "freellmapi-probe", "install.sh", "README.md", "units"]),
        (T / "host-heat", "_tools/host-heat", ["host-heat", "fleet-heat", "install.sh", "remote-install.sh",
                                           "test_host_heat.py"]),
        (T / "agents-slice", "_tools/agents-slice", ["claude-slice", "heavy", "README.md"]),
        (home / ".config" / "systemd" / "user", "_tools/agents-slice", ["agents.slice"]),
        (T / "secret-firewall-corpus", "_tools/secret-firewall-corpus", ["run.py", "corpus.jsonl", "README.md"]),
        (aip / "_system", "_system", ["lanes.json"]),
        (aip / ".claude" / "hooks", ".claude/hooks", ["inject-vault-boot.sh"]),
        (T / "jev-browse", "_tools/jev-browse", ["jev_browse.py", "skill", "test_jev_browse.py"]),
        (T / "motion", "_tools/motion", ["bin", "template", "skill", "package.json", "package-lock.json", ".gitignore"]),
        (T / live("desk-control"), "_tools/desk-control", ["computer.py", "mcp_console.py", "launch", "install", "overlay.qml",
                                                          "demo.qml", "zen-agent", "pyproject.toml", "uv.lock", ".gitignore"]),
        (T / live("desk-control") / "firefox-mcp", "_tools/desk-control/firefox-mcp", ["package.json", "package-lock.json", "patch.mjs"]),
        (aip / "pipeline" / "handoff-v2", "pipeline/handoff-v2", ["handoff", "bin", "hooks", "README.md", ".gitignore"]),
        (home / "agentic-os" / "bin", "agentic-os", ["aos_lock.py"]),
        (claude / "hooks", "harness/hooks", HOOKS),
        (claude / "hooks/web-taint", "harness/hooks/web-taint",
         ["common.py", "guard.py", "scan.py", "taint_on_failure.py", "supervise.py", "codex_adapter.py", "codex_supervise.py",
          "policy.py", "sandbox.py", "agents.py", "judge_run.py"]),
        (claude / "output-styles", "harness/output-styles", ["signal.md"]),
        (claude / "agents", "harness/agents", ["lean.md"]),
        # route is a symlink to _tools/lane-intel/route.py: stage_source copies the real file under its link name.
        (home / ".local" / "bin", "harness/bin", ["freellm-keydrop", "cx", "route", "thor-up", "vault-sync",
                                                  "chrome-devtools-repatch", "loki-shot", "loki-gui"]),
        (home / ".codex", "harness/codex", ["AGENTS.md", "hooks.json", "herdr-agent-state.sh"]),
        (claude, "harness", ["settings.json"]),
        (claude / "commands", "harness/commands", ["lane.md", "handoff.md", "resume-session.md", "open-loops.md", "freellmapi.md"]),
    ] + [(claude / "skills" / n, f"harness/skills/{n}", None)
         for n in ("codex-build", "codex-review", "codex-goal-dispatcher", "grill-me-codex", "grill-with-docs-codex")]


LINK_ROOT = Path(os.environ.get("AI_PROJECTS_ROOT", Path.home() / "AI_Projects")).resolve()


def stage_source(source, destination, names):
    if not real_directory(source):
        raise RuntimeError(f"missing live source: {source}")
    for name in names or sorted(os.listdir(source)):
        child = source / name
        # a directory named on purpose in the allowlist (x-scroll's tests, which its --self-test runs) ships even if
        # EXCLUDE_DIRS would drop it in a walk; file exclusions (*.bak*, logs, ...) always apply
        if excluded(name, EXCLUDE_FILES if names else EXCLUDE_DIRS + EXCLUDE_FILES):
            continue
        if real_directory(child):
            for path in included_tree(child, EXCLUDE_DIRS, EXCLUDE_FILES):
                copy_file(path, destination / path.relative_to(source))
        elif regular_file(child) is not None:
            copy_file(child, destination / name)
        elif names and os.path.islink(child) and LINK_ROOT in Path(os.path.realpath(child)).parents:
            # an allowlisted link name (e.g. ~/.local/bin/route) ships as a copy of its first-party target
            if not copy_file(Path(os.path.realpath(child)), destination / name):
                raise RuntimeError(f"allowlisted link has no regular target: {child}")
        elif names:
            raise RuntimeError(f"missing allowlisted file: {child}")


def syntax_check(framework):
    """A replacement must never break code: every exported Python/shell file has to still parse."""
    broken = []
    for path in framework.rglob("*"):
        if not path.is_file():
            continue
        head = path.read_bytes()[:64]
        if path.suffix == ".py" or head.startswith(b"#!/usr/bin/env python"):
            try:
                compile(path.read_bytes(), str(path), "exec")
            except SyntaxError as error:
                broken.append(f"{path.relative_to(framework)}: {error.msg} (line {error.lineno})")
        elif path.suffix == ".sh" or head.startswith((b"#!/bin/bash", b"#!/usr/bin/env bash", b"#!/bin/sh")):
            done = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
            if done.returncode:
                broken.append(f"{path.relative_to(framework)}: {done.stderr.strip()[:200]}")
    if broken:
        raise RuntimeError("scrub broke code:\n  " + "\n  ".join(broken))


def assert_not_ignored(repo, framework):
    """Every staged file must reach a clone: a .gitignore rule (e.g. *.jsonl for logs) would silently drop a file the
    export needs (the firewall corpus, KEEP_FILES) from the commit while the working tree still has it."""
    paths = [f"framework/{p.relative_to(framework).as_posix()}" for p in sorted(framework.rglob("*")) if p.is_file()]
    done = subprocess.run(["git", "-C", str(repo), "check-ignore", "--no-index", "--stdin", "-z"],
                          input="\0".join(paths) + "\0", text=True, capture_output=True)
    if done.returncode == 0:      # 0: some path is ignored; 1: none is
        ignored = [p for p in done.stdout.split("\0") if p]
        raise RuntimeError("git would not commit these exported files (add a !negation to .gitignore or drop them from "
                           "the allowlist):\n  " + "\n  ".join(ignored))
    if done.returncode != 1:
        raise RuntimeError(f"git check-ignore failed: {done.stderr.strip()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="replace framework/ in the checkout with the scrubbed stage")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parent.parent
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    stage = Path(os.environ.get("TMPDIR", "/var/tmp")) / f"kit-stage-{stamp}"
    os.mkdir(stage, mode=0o700)
    framework = stage / "framework"
    if not private_rules().get("replacements"):
        raise RuntimeError("private scrub rules not found (set SCRUB_RULES); refusing to export with template rules")
    aip = Path(os.environ.get("AI_PROJECTS_ROOT", Path.home() / "AI_Projects"))
    print(f"== Staging allowlisted live sources into {stage} ==")
    table = [(source, dest, [live(n) for n in names] if names else names)
             for source, dest, names in sources(aip, Path.home() / ".claude", Path.home())]
    for source, dest, names in table:
        stage_source(source, framework / dest, names)
    harness = framework / "harness"
    settings = json.loads((harness / "settings.json").read_text())
    excluded_re = excluded_tools_re()
    for event, groups in settings.get("hooks", {}).items():  # drop wiring for hooks that are not exported
        for group in groups:
            group["hooks"] = [h for h in group["hooks"] if not excluded_re.search(h.get("command", ""))]
        settings["hooks"][event] = [g for g in groups if g["hooks"]]
    (harness / "settings.reference.json").write_text(json.dumps(settings, indent=2) + "\n")
    (harness / "settings.json").unlink()
    binaries = [p for p in framework.rglob("*") if p.is_file() and b"\x00" in p.read_bytes()[:8192]
                and p.name not in BINARY_OK]
    if binaries:
        raise RuntimeError("binary files staged (cannot be scrubbed): " + ", ".join(str(p.relative_to(framework)) for p in binaries))
    (framework / "harness" / "CLAUDE.global.md").write_bytes((Path.home() / ".claude" / "CLAUDE.md").read_bytes())

    strip_excluded(framework)   # dispatcher entries, client-only instruction sections (any heading)
    trim_framework(framework)
    print("== Export assertions ==")
    assert_wiring(framework, table, Path.home())   # on live paths, before the scrub rewrites them
    print("every referenced first-party executable/unit is exported")
    print("== Privacy scrub + safety guard ==")
    scrub(repo / "tools" / "scrub.py", framework)
    assert_no_excluded_tools(framework)            # on what actually ships
    print("no excluded tool is named in the export")
    syntax_check(framework)
    gitleaks = shutil.which("gitleaks")
    if not gitleaks:
        raise RuntimeError("gitleaks not installed; refusing to continue (fail closed)")
    subprocess.run([gitleaks, "dir", str(framework), "--no-banner", "--redact"], check=True)
    assert_not_ignored(repo, framework)
    print("every exported file is committable (no .gitignore rule drops it)")

    if args.apply:
        shutil.rmtree(repo / "framework", ignore_errors=True)
        shutil.copytree(framework, repo / "framework", symlinks=False)
        print("framework/ replaced. Review with: git status && git diff --stat")
    else:
        print(f"Dry run. Scrubbed stage: {framework}  (re-run with --apply to replace framework/)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
