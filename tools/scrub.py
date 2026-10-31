#!/usr/bin/env python3
"""
scrub.py - privacy scrub + identity generalization + safety gate for the workflow kit.

Run AFTER sync-kit.ps1 copies live sources into framework/. Operates in-place on a
target dir (default: ../framework relative to this file).

Pipeline (in order):
  1. PATH SCRUB        (deterministic): rewrite real machine paths -> placeholders.
  2. GENERALIZE        (rules-driven): auto-replace operator/personal-identity references
                       with generic forms, per tools/scrub-rules.local.json when present,
                       otherwise the public tools/scrub-rules.json template. Allowlisted
                       brand/project tokens are sentinel-protected first, so a broad rule
                       can never mangle them.
  3. USERNAME GUARD    (abort): real OS username must not survive the path scrub.
  4. SECRET GUARD      (abort): refuse on a real credential pattern.
  5. PATH GUARD        (abort): refuse if a forbidden private path is staged.
  6. VERIFY GATE       (abort): refuse if any active ruleset verifyDenylist pattern still
                       appears after generalization. THIS is "make all future instances
                       generic via verification when pushing".

The real username is read from the environment at runtime and is NEVER written into this
file, so the committed tooling can't leak it. Maintainer-specific generalization rules live
in git-ignored tools/scrub-rules.local.json; the committed tools/scrub-rules.json is a
generic template.

Exit codes: 0 = clean. 2 = blocked (any guard/gate tripped). The orchestrator must NOT
commit on a non-zero exit.

CHECK MODE (non-mutating): scrub.py --check <paths...> scans every text file it is given (directories are
walked; .git and the active private rules file are skipped) and reports, by file and line, everything the
scrub would rewrite (path, username, generalization rules, file names) plus every guard hit (username,
credential, forbidden path, verify denylist, binary file). With --guard-denylist FILE it also runs the public
push gate's personal-data denylist (the gate module shipped in framework/). It never writes. It refuses to run
on the template rules alone unless --template-ok. Exit codes: 0 = clean, 1 = hits, 2 = usage/config error.
Matched text is printed only with --show. A word-boundary rule cannot see a word right after a backslash-n, -t
or -r escape inside a string literal ("...\\nName"), and the scrub misses it there, so the check also scans a copy
with those escapes blanked and reports what only that copy shows.
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PUBLIC_RULES_PATH = os.path.join(HERE, "scrub-rules.json")
LOCAL_RULES_PATH = os.environ.get("SCRUB_RULES") or os.path.join(HERE, "scrub-rules.local.json")

# --- real identity, derived at runtime (no literal username in this file) ---
HOME = os.path.expanduser("~")
USER = os.path.basename(HOME.rstrip("\\/"))
PLACEHOLDER_USER = "<you>"   # in Windows-style paths
BARE_USER = "you"            # bare mentions: stays a valid shell/Python token

TEXT_EXT = {
    ".md", ".py", ".ps1", ".ts", ".js", ".mjs", ".json", ".txt", ".sh",
    ".bat", ".html", ".css", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".env",
}


def path_rewrites(user):
    drive = HOME[:2] if len(HOME) > 1 and HOME[1] == ":" else "C:"
    return [
        (f"{drive}\\Users\\{user}", f"{drive}\\Users\\{PLACEHOLDER_USER}"),
        (f"{drive}/Users/{user}",   f"{drive}/Users/{PLACEHOLDER_USER}"),
        (f"/{drive[0].lower()}/Users/{user}", f"/{drive[0].lower()}/Users/{PLACEHOLDER_USER}"),
        (f"\\Users\\{user}", f"\\Users\\{PLACEHOLDER_USER}"),
        (f"/Users/{user}",   f"/Users/{PLACEHOLDER_USER}"),
    ]


# Real-credential signatures (tuned long/high-entropy so the secret-firewall's own
# detection regexes and short doc examples don't false-positive).
SECRET_PATTERNS = [
    re.compile(r"gh[oprs]_[A-Za-z0-9]{36,}"),
    re.compile(r"sk-ant-api\d{2}-[A-Za-z0-9_\-]{80,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[bp]-\d{10,}-\d{10,}-[A-Za-z0-9]{24,}"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
]

# Private paths that must NEVER appear as files in the shared tree.
# NOTE: the second-brain *skill* (_system/skills/second-brain/) is shippable machinery.
# What's forbidden is the vault *content* at _system/second-brain/ (a different path).
FORBIDDEN_SEGMENTS = {".secrets", "voice-corpus", "claude_vault", ".codex"}


def forbidden_path(rel):
    parts = [p.lower() for p in re.split(r"[\\/]", rel) if p]
    if any(seg in FORBIDDEN_SEGMENTS for seg in parts):
        return True
    if any(p == ".env" or p.startswith(".env.") for p in parts):
        return True
    for i in range(len(parts) - 1):
        a, b = parts[i], parts[i + 1]
        if a == "_system" and b == "second-brain":   # vault CONTENT, not the skill
            return True
        if a == "secret-store" and b == "secrets":    # secret store DATA, not secret.ps1
            return True
    return False


def load_rules():
    if os.environ.get("SCRUB_RULES") and not os.path.isfile(LOCAL_RULES_PATH):
        print(f"[scrub] ERROR: SCRUB_RULES points at a missing file: {LOCAL_RULES_PATH}")
        raise SystemExit(2)
    rules_path = LOCAL_RULES_PATH if os.path.isfile(LOCAL_RULES_PATH) else PUBLIC_RULES_PATH
    if not os.path.isfile(rules_path):
        return {"replacements": [], "allowlist": [], "verifyDenylist": [], "source": "none"}, []
    print(f"[scrub] using {'local' if rules_path == LOCAL_RULES_PATH else 'template'} rules")
    with open(rules_path, "r", encoding="utf-8") as fh:
        rules = json.load(fh)
    warnings = []
    compiled_repl = []
    for r in rules.get("replacements", []):
        flags = 0 if r.get("caseSensitive", True) else re.IGNORECASE
        pat = r["find"] if r.get("isRegex") else re.escape(r["find"])
        try:
            compiled_repl.append((re.compile(pat, flags), r["replace"], r.get("note", "")))
        except re.error as e:
            warnings.append(f"bad replacement regex {r['find']!r}: {e}")
    compiled_deny = []
    for d in rules.get("verifyDenylist", []):
        try:
            compiled_deny.append((re.compile(d["pattern"]), d.get("label", d["pattern"])))
        except re.error as e:
            warnings.append(f"bad denylist regex {d['pattern']!r}: {e}")
    allow = sorted({a["token"] for a in rules.get("allowlist", [])}, key=len, reverse=True)
    return {"replacements": compiled_repl, "allowlist": allow, "verifyDenylist": compiled_deny,
            "source": "local" if rules_path == LOCAL_RULES_PATH else "template"}, warnings


def generalize(text, rules):
    """Apply path scrub + ruleset replacements, protecting allowlisted tokens with sentinels."""
    # protect allowlisted tokens
    sentinels = {}
    for i, tok in enumerate(rules["allowlist"]):
        s = f"\x00AL{i}\x00"
        if tok in text:
            text = text.replace(tok, s)
            sentinels[s] = tok
    # ruleset replacements
    for pat, repl, _note in rules["replacements"]:
        text = pat.sub(repl, text)
    # restore allowlisted tokens
    for s, tok in sentinels.items():
        text = text.replace(s, tok)
    return text


def rewrite_text(text, path, rules, pathsubs):
    """The scrub's content rewrite (paths, username, generalization rules); --check compares against it."""
    for old, new in pathsubs:
        text = text.replace(old, new)
    # Python: a home-path string literal becomes an expanduser() call so the code still runs anywhere.
    if path.endswith(".py") or text.startswith("#!/usr/bin/env python"):
        text = re.sub(rf"(?<![A-Za-z])([\"'])/home/{re.escape(USER)}((?:/[^\"'\n]*)?)\1",
                      lambda m: f"__import__('os').path.expanduser({m.group(1)}~{m.group(2)}{m.group(1)})", text)
    # Linux home: $HOME stays valid inside shell commands (a <you> placeholder would be a redirection).
    text = re.sub(rf"/home/{re.escape(USER)}(?![A-Za-z0-9_.-])", "$HOME", text)
    # Runtime username generalization (portable — tracks the machine, no hardcoded name).
    # Catches forms the path rewrites miss: the slugified project dir (C--Users-<user>-...)
    # and bare prose mentions. The residue guard below verifies none survive.
    if USER and len(USER) >= 4:
        text = re.sub(rf"\b{re.escape(USER)}\b", BARE_USER, text, flags=re.IGNORECASE)
    return generalize(text, rules)


def rewrite_name(name, rules):
    new = generalize(re.sub(rf"\b{re.escape(USER)}\b", "you", name, flags=re.IGNORECASE) if USER else name, rules)
    return new.replace("<", "").replace(">", "").replace(" ", "-")


def iter_text_files(root):
    """Every file that looks like text: known extension, or no NUL byte in its first 8 KB.
    Extensionless scripts (CLIs, systemd units) must not slip past the scrub and its guards."""
    for dirpath, _dirs, filenames in os.walk(root):
        if ".git" in dirpath.split(os.sep):
            continue
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.splitext(name)[1].lower() in TEXT_EXT:
                yield path
                continue
            try:
                with open(path, "rb") as fh:
                    if b"\x00" not in fh.read(8192):
                        yield path
            except OSError:
                continue


def scrub_main(target):
    target = os.path.abspath(target)
    if not os.path.isdir(target):
        print(f"[scrub] ERROR: target dir not found: {target}")
        return 2

    rules, warnings = load_rules()
    for w in warnings:
        print(f"[scrub] ERROR: {w}")
    if warnings:
        return 2   # a broken rule silently scrubs nothing: fail closed
    pathsubs = path_rewrites(USER)
    user_residue = re.compile(rf"\b{re.escape(USER)}\b", re.IGNORECASE) if USER else None

    changed = 0
    residue_hits, secret_hits, path_hits, deny_hits = [], [], [], []

    # Path guard runs over ALL files (even binary) by filename.
    for dirpath, _dirs, files in os.walk(target):
        for name in files:
            rel = os.path.relpath(os.path.join(dirpath, name), target)
            if forbidden_path(rel):
                path_hits.append(rel)

    for f in iter_text_files(target):
        try:
            text = open(f, "r", encoding="utf-8").read()
            encoding = "utf-8"
        except UnicodeDecodeError:
            try:
                text = open(f, "r", encoding="utf-16").read()
                encoding = "utf-16"
            except (UnicodeDecodeError, OSError):
                continue
        except OSError:
            continue
        rel = os.path.relpath(f, target)
        original = text
        text = rewrite_text(text, f, rules, pathsubs)
        if text != original:
            open(f, "w", encoding=encoding, newline="").write(text)
            changed += 1
        # post-scrub guards (on the rewritten text)
        if user_residue and user_residue.search(text):
            residue_hits.append(rel)
        for pat in SECRET_PATTERNS:
            if pat.search(text):
                secret_hits.append((rel, pat.pattern))
                break
        for pat, label in rules["verifyDenylist"]:
            m = pat.search(text)
            if m:
                deny_hits.append((rel, label, m.group(0)[:60]))

    # File and directory NAMES carry identity too (e.g. a tool named after the operator): scrub them, deepest first.
    for dirpath, dirs, files in os.walk(target, topdown=False):
        for name in files + dirs:
            new = rewrite_name(name, rules)
            if new != name:
                os.rename(os.path.join(dirpath, name), os.path.join(dirpath, new))
                changed += 1
    for dirpath, dirs, files in os.walk(target):
        for name in files + dirs:
            rel = os.path.relpath(os.path.join(dirpath, name), target)
            for pat, label in rules["verifyDenylist"]:
                if pat.search(name):
                    deny_hits.append((rel, label + " (in a file name)", name))

    print(f"[scrub] path scrub + generalization applied to {changed} file(s).")
    if not rules["replacements"]:
        print("[scrub] NOTE: no generalization ruleset loaded.")

    blocked = False
    if residue_hits:
        blocked = True
        print("\n[scrub] BLOCK - real username still present after scrub in:")
        for r in sorted(set(residue_hits)):
            print(f"    {r}")
    if secret_hits:
        blocked = True
        print("\n[scrub] BLOCK - possible real credential:")
        for r, p in secret_hits:
            print(f"    {r}   (pattern: {p})")
    if path_hits:
        blocked = True
        print("\n[scrub] BLOCK - forbidden private path staged:")
        for r in sorted(set(path_hits)):
            print(f"    {r}")
    if deny_hits:
        blocked = True
        print("\n[scrub] BLOCK - identity residue survived generalization (verify gate):")
        for r, label, sample in sorted(set(deny_hits)):
            print(f"    {r}   [{label}]  e.g. {sample!r}")

    if blocked:
        print("\n[scrub] RESULT: BLOCKED - do not commit. Resolve the items above.")
        return 2
    print("\n[scrub] RESULT: clean.")
    return 0


def is_binary(data):
    return b"\x00" in data[:8192] and not data.startswith((b"\xff\xfe", b"\xfe\xff"))


def decode(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    return data.decode("utf-8")


def load_guard(denylist, owner_repo):
    """The public push gate's personal-data denylist, through the gate module the kit ships (same semantics:
    disabled entries, repo-scoped allowlist, the repo owner's own handle). Missing pieces fail closed."""
    gate_dir = os.environ.get("GUARD_GATE_DIR") or os.path.join(HERE, "..", "framework", "_system", "skills", "guard", "scripts")
    if not os.path.isfile(denylist):
        print(f"[scrub] ERROR: guard denylist not found: {denylist}")
        raise SystemExit(2)
    if not os.path.isfile(os.path.join(gate_dir, "public_push_gate.py")):
        print(f"[scrub] ERROR: public_push_gate.py not found in {gate_dir} (set GUARD_GATE_DIR)")
        raise SystemExit(2)
    os.environ["GUARD_DENYLIST_FILE"] = os.path.abspath(denylist)
    sys.path.insert(0, os.path.abspath(gate_dir))
    sys.dont_write_bytecode = True   # --check never writes, not even a __pycache__ inside framework/
    import public_push_gate as gate
    try:
        gate.load_config()
    except gate.GateBlock as error:
        print(f"[scrub] ERROR: guard denylist: {error}")
        raise SystemExit(2)
    if not gate.DENYLIST:
        print(f"[scrub] ERROR: guard denylist has no active entries: {denylist}")
        raise SystemExit(2)
    return lambda rel, text: [f.label for f in gate.scan_text("check", rel, text, owner_repo)]


def login_names():
    """The username from $HOME plus the account's login name: a commit made with another HOME (a container, sudo, a
    sandbox) must still be checked for the real name."""
    names = {USER}
    try:
        import pwd
        names.add(pwd.getpwuid(os.getuid()).pw_name)
    except (ImportError, KeyError):
        pass
    return sorted(n for n in names if n)


def check_files(paths):
    """(display path, real path) for every file given; directories are walked, .git and the rules file skipped."""
    rules_file = os.path.realpath(LOCAL_RULES_PATH)
    for given in paths:
        if os.path.isdir(given):
            for dirpath, dirs, files in os.walk(given):
                dirs[:] = sorted(d for d in dirs if d != ".git")
                for name in sorted(files):
                    full = os.path.join(dirpath, name)
                    if os.path.realpath(full) != rules_file:
                        yield os.path.relpath(full, given), full
        elif os.path.isfile(given):
            if os.path.realpath(given) != rules_file:
                yield given, given
        else:
            print(f"[scrub] ERROR: no such file or directory: {given}")
            raise SystemExit(2)


def line_of(text, index):
    return text.count("\n", 0, index) + 1


ESCAPES = re.compile(r"\\[ntr]")   # a literal backslash-n/t/r: blanked to two spaces, so offsets and lines hold


def unique_matches(pattern, variants):
    """Matches of pattern in the text and its escape-blanked copy, each start reported once."""
    seen = set()
    for variant in variants:
        for m in pattern.finditer(variant):
            if m.start() not in seen:
                seen.add(m.start())
                yield m


def check_main(args):
    """Non-mutating: report what the scrub would change and every guard hit. 0 clean, 1 hits, 2 error."""
    rules, warnings = load_rules()
    for w in warnings:
        print(f"[scrub] ERROR: {w}")
    if warnings:
        return 2
    if rules["source"] != "local" and not args.template_ok:
        print("[scrub] ERROR: only the template rules are loaded (no tools/scrub-rules.local.json, no $SCRUB_RULES);"
              " a check against them proves nothing. Pass --template-ok to run anyway.")
        return 2
    guard = load_guard(args.guard_denylist, args.owner_repo) if args.guard_denylist else None
    pathsubs = path_rewrites(USER)
    names = login_names()
    hits = []   # (path, line or 0, label, sample)

    def hit(rel, label, text="", start=None, end=None):
        line = line_of(text, start) if start is not None else 0
        sample = text[start:end][:60] if start is not None else ""
        hits.append((rel, line, label, sample))

    def scan_name(rel):
        for part in re.split(r"[\\/]", rel):
            if part and rewrite_name(part, rules) != part:
                hit(rel, "scrub would rename this path")
                break
        for pat, label in rules["verifyDenylist"]:
            if pat.search(rel):
                hit(rel, f"verify denylist [{label}] in a file name")
        if forbidden_path(rel):
            hit(rel, "forbidden private path")

    count = 0
    for rel, full in check_files(args.paths):
        count += 1
        scan_name(rel)
        try:
            data = open(full, "rb").read()
        except OSError as error:
            hit(rel, f"unreadable: {error.strerror}")
            continue
        if is_binary(data):
            hit(rel, "binary file (cannot be scanned; the kit ships no binaries)")
            continue
        try:
            text = decode(data)
        except UnicodeDecodeError:
            hit(rel, "not valid UTF-8/UTF-16 text")
            continue
        if rewrite_text(text, full, rules, pathsubs) != text:
            # Per line, so the report names where the scrub would change something and which rule.
            before = len(hits)
            for number, line in enumerate(text.splitlines(), 1):
                if rewrite_text(line, full, rules, pathsubs) == line:
                    continue
                label = "scrub would rewrite (path or username)"
                for i, (pat, _repl, _note) in enumerate(rules["replacements"], 1):
                    if generalize(line, {"allowlist": rules["allowlist"], "replacements": [(pat, _repl, "")]}) != line:
                        label = f"scrub would rewrite (replacement rule #{i})"
                        break
                hits.append((rel, number, label, line.strip()[:60]))
            if len(hits) == before:     # a rule that only matches across lines
                hit(rel, "scrub would rewrite (multi-line match)")
        blanked = ESCAPES.sub("  ", text)
        variants = (text,) if blanked == text else (text, blanked)
        if blanked != text and rewrite_text(blanked, full, rules, pathsubs) != blanked:
            for number, (line, bline) in enumerate(zip(text.splitlines(), blanked.splitlines()), 1):
                if (bline != line and rewrite_text(bline, full, rules, pathsubs) != bline
                        and rewrite_text(line, full, rules, pathsubs) == line):
                    hits.append((rel, number, "a scrub rule matches right after a \\n, \\t or \\r escape, where the scrub"
                                 " misses it (reword it or add a rule)", line.strip()[:60]))
        for name in names:
            for m in unique_matches(re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE), variants):
                hit(rel, "real username", text, m.start(), m.end())
        for pat in SECRET_PATTERNS:
            for m in pat.finditer(text):
                hit(rel, "possible real credential", text, m.start(), m.end())
        for pat, label in rules["verifyDenylist"]:
            for m in unique_matches(pat, variants):
                hit(rel, f"verify denylist [{label}]", text, m.start(), m.end())
        if guard:
            for label in {label for variant in variants for label in guard(rel, variant)}:
                hit(rel, f"guard denylist [{label}]")

    if count == 0:
        print("[scrub] ERROR: no files to check.")
        return 2
    for rel, line, label, sample in sorted(set(hits)):
        where = f"{rel}:{line}" if line else rel
        print(f"    {where}   {label}" + (f"   e.g. {sample!r}" if args.show and sample else ""))
    if hits:
        print(f"[scrub] CHECK: {len(set(hits))} hit(s) in {count} file(s). Nothing was changed.")
        return 1
    print(f"[scrub] CHECK: clean ({count} file(s)).")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Privacy scrub for the workflow kit (rewrites in place unless --check).")
    parser.add_argument("paths", nargs="*", help="scrub: one target dir (default ../framework); --check: files or dirs")
    parser.add_argument("--check", action="store_true", help="report only, never write; exit 1 on any hit")
    parser.add_argument("--template-ok", action="store_true", help="--check: allow running on the template rules")
    parser.add_argument("--guard-denylist", metavar="FILE", help="--check: also scan with this public-push-gate denylist")
    parser.add_argument("--owner-repo", default="", help="--check: owner/repo for the gate's own-handle exemption")
    parser.add_argument("--show", action="store_true", help="--check: print the matched text (local terminal only)")
    args = parser.parse_args()
    if args.check:
        if not args.paths:
            parser.error("--check needs at least one file or directory")
        return check_main(args)
    if args.template_ok or args.guard_denylist or args.show or args.owner_repo:
        parser.error("--template-ok, --guard-denylist, --owner-repo and --show only apply to --check")
    if len(args.paths) > 1:
        parser.error("the in-place scrub takes one target directory")
    return scrub_main(args.paths[0] if args.paths else os.path.join(HERE, "..", "framework"))


if __name__ == "__main__":
    sys.exit(main())
