#!/usr/bin/env bash
# webcred-guard: PreToolUse hook for Claude Code and Codex (spec §3.5 "guardrails against the agent itself").
#
# Denies tool calls that reach around the credential courier:
#   - any command or path under /var/lib/webcred or /run/webcred (or ~webcred), including the age identity and the
#     .age store, except a plain `web cred|login|status ...` invocation (the courier's only front door is `web cred` /
#     `web login`; `web read/extract/act/crawl` take paths and URLs, so they get no exemption);
#   - acting as webcred: sudo/su/runuser/run0/doas/pkexec/setpriv/machinectl/systemd-run/nsenter with "webcred" in
#     the same command (sudo -u webcred, sudo systemctl ... webcred, run0 --user=webcred, ...);
#   - decrypting with age: `age -d` / `--decrypt` / `-decrypt` (rage too).
# Bash: the command text, with quotes/backslashes removed and slashes collapsed so `/var//lib/web'cred'` still
# matches. File tools: the path as given and its realpath (a symlink into the store is caught). Edit/Write content
# is not inspected (writing code or docs that mention the paths is fine). Codex's apply_patch: the patch's file
# headers.
#
# Output: the documented JSON deny (hookSpecificOutput.permissionDecision = "deny"), exit 0; both Claude Code and
# Codex accept it. Allow = no output. On unparseable input or an internal error: fail CLOSED when the raw payload
# mentions webcred, open otherwise (a broken hook must not brick the session). This is a guardrail with an audit
# trail, not a wall (Claude has passwordless sudo on odin).
# Source of truth: _tools/web/courier/webcred-guard.sh; deployed to ~/.claude/hooks/webcred-guard.sh.
read -r -d '' PROG <<'PY'
import json, os, re, sys

PATH_RE = re.compile(r"/var/lib/webcred(?![\w.-])|/run/webcred(?![\w.-])|~webcred(?![\w.-])")
PRIV_RE = re.compile(r"(?<![\w.-])(sudo|su|runuser|run0|doas|pkexec|setpriv|machinectl|systemd-run|nsenter)"
                     r"(?![\w.-])"
                     r".*(?<![\w-])webcred(?![\w-])", re.S)
AGE_RE = re.compile(r"(?<![\w.-])r?age(?![\w.-])[^;&|\n]*?\s-{1,2}(d|decrypt)(?=[\s=]|$)")
SHELL_META = re.compile(r"[;&|`$<>(){}\n]")
PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.M)
PATH_KEYS = ("file_path", "path", "notebook_path")
WEB_FRONT_DOOR = ("cred", "login", "status")


def norm(text: str) -> str:
    t = re.sub(r"['\"\\]", "", text)
    t = re.sub(r"/+", "/", t)
    while "/./" in t:
        t = t.replace("/./", "/")
    return t


def path_hit(p) -> bool:
    if not isinstance(p, str) or not p:
        return False
    cands = [norm(p), norm(os.path.expanduser(p))]
    try:
        cands.append(os.path.realpath(os.path.expanduser(p)))   # a symlink into the store
    except (OSError, ValueError):
        pass
    return any(PATH_RE.search(c) for c in cands)


def only_web(cmd: str) -> bool:
    """A single plain `web cred|login|status ...` invocation (the courier's front door and the health check): no
    shell syntax that could chain or substitute another command. Other `web` subcommands (read, extract, act, crawl)
    take URLs and file paths, so they get no exemption."""
    if SHELL_META.search(cmd):
        return False
    words = cmd.split()
    return len(words) >= 2 and (words[0] == "web" or words[0].endswith("/web")) and words[1] in WEB_FRONT_DOOR


def screen_command(cmd: str) -> str | None:
    t = norm(cmd)
    if PATH_RE.search(t) and not only_web(cmd):
        return "touches the credential courier's store or socket"
    if PRIV_RE.search(t):
        return "acts as (or on) the webcred user"
    if AGE_RE.search(t):
        return "decrypts with age"
    return None


def screen(tool: str, ti: dict) -> str | None:
    if tool in ("Bash", "PowerShell", "shell", "exec_command"):
        cmd = ti.get("command") or ti.get("cmd") or ""
        if isinstance(cmd, list):
            cmd = " ".join(map(str, cmd))
        return screen_command(str(cmd))
    if tool == "apply_patch":
        patch = str(ti.get("command") or ti.get("input") or ti.get("patch") or "")
        for m in PATCH_FILE.finditer(patch):
            if path_hit((m.group(1) or m.group(2)).strip()):
                return "patches a file in the credential courier's store"
        return None
    for k in PATH_KEYS:
        if path_hit(ti.get(k)):
            return "targets the credential courier's store or socket"
    if tool == "Glob" and path_hit(ti.get("pattern")):
        return "targets the credential courier's store"
    return None


def main() -> None:
    raw = b""
    try:
        raw = sys.stdin.buffer.read()   # bytes: an undecodable byte must not make the check below fail open
        data = json.loads(raw.decode("utf-8", "replace") or "null")
        if not isinstance(data, dict):
            raise ValueError("not an object")
        ti = data.get("tool_input") or {}
        why = screen(str(data.get("tool_name", "")), ti if isinstance(ti, dict) else {})
    except Exception:
        # Closed when the call mentions webcred at all, open otherwise (never brick unrelated work).
        if b"webcred" not in raw:
            return
        why = "could not be checked (the guard failed on it) and mentions webcred"
    if why:
        msg = (f"BLOCKED by webcred-guard: this call {why}. Credentials are reached only through the courier: "
               "`web cred list|gen|rm|intake|audit` and `web login` (it types the secret itself and never "
               "returns it). If you only need to WRITE text that mentions these paths (notes, docs, a commit "
               "message), use the Write tool, or `git commit -F <file>`, instead of putting them on a command "
               "line.")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": msg}}))
        print(msg, file=sys.stderr)


main()
PY
exec /usr/bin/python3 -I -c "$PROG"
