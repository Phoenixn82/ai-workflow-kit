#!/usr/bin/env python3
"""Scan staged vault blobs against the vault policy, then gitleaks.

Only filenames, labels, and aggregate report counts are emitted: never a
matched value.  This intentionally reads the index, not working-tree files,
so the result is exactly what git would commit.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


TOKEN_END = re.compile(r"[\s<>()\[\]\"'`]")


def allowed(allow: list[re.Pattern[str]], text: str, hit: re.Match[str]) -> bool:
    """True when a repo-scoped allowlist pattern matches the whole token (URL or word) holding the hit."""
    if not allow:
        return False
    lo = max((m.end() for m in TOKEN_END.finditer(text, max(0, hit.start() - 300), hit.start())), default=max(0, hit.start() - 300))
    end = TOKEN_END.search(text, hit.end())
    token = text[lo:end.start() if end else len(text)]
    return any(a.search(token) for a in allow)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: vault-scan.py REPO", file=sys.stderr)
        return 1
    repo = Path(sys.argv[1]).resolve()
    try:
        policy = json.loads((repo / "sync-scan-policy.json").read_text(encoding="utf-8"))
        denylist_path = Path(policy["denylist_path"]).expanduser()
        if not denylist_path.exists():
            # Per-machine copy for hosts without the AI_Projects tree (loki, thor).
            denylist_path = Path("~/.config/vault-sync/denylist.local.json").expanduser()
        denylist = json.loads(denylist_path.read_text(encoding="utf-8")).get("denylist", [])
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"vault-scan: policy error: {exc}", file=sys.stderr)
        return 1

    block_labels = set(policy.get("block_labels", []))
    # Denylist allowlist entries scoped to this repo (by "repos": ["owner/name"]) are tested against the
    # whole URL/word around a hit, so one third-party article can pass without unblocking the label
    # (the guard gate tests the bare match, so an entry like ^https://x\.blogsite\.com/p/ stays inert there).
    origin = git(repo, "remote", "get-url", "origin").stdout.decode().strip()
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", origin)
    owner_repo = m.group(1).casefold() if m else ""
    try:
        allow = [re.compile(a["regex"], 0 if a.get("case_sensitive") else re.IGNORECASE)
                 for a in json.loads(denylist_path.read_text(encoding="utf-8")).get("allowlist", [])
                 if owner_repo and owner_repo in [r.casefold() for r in a.get("repos") or []]]
    except (KeyError, TypeError, re.error) as exc:
        print(f"vault-scan: allowlist error: {exc}", file=sys.stderr)
        return 1
    rules: list[tuple[str, re.Pattern[str], bool]] = []
    try:
        for rule in denylist:
            if rule.get("disabled"):
                continue
            flags = 0 if rule.get("case_sensitive") else re.IGNORECASE  # same as the guard gate
            rules.append((rule["label"], re.compile(rule["regex"], flags), rule["label"] in block_labels))
    except (KeyError, re.error) as exc:
        print(f"vault-scan: denylist error: {exc}", file=sys.stderr)
        return 1

    names = git(repo, "diff", "--cached", "--name-only", "-z")
    if names.returncode:
        sys.stderr.buffer.write(names.stderr)
        return 1

    report_counts = {label: 0 for label, _, blocked in rules if not blocked}
    offenders: list[tuple[str, str]] = []
    for raw_name in names.stdout.split(b"\0"):
        if not raw_name:
            continue
        path = raw_name.decode("utf-8", "surrogateescape")
        blob = git(repo, "show", f":{path}")
        if blob.returncode:
            # A staged deletion has no index blob and therefore nothing to scan.
            if not git(repo, "ls-files", "-s", "--", path).stdout:
                continue
            print(f"vault-scan: cannot read staged blob: {path}", file=sys.stderr)
            return 1
        try:
            text = blob.stdout.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern, blocked in rules:
            count = sum(1 for hit in pattern.finditer(text) if not allowed(allow, text, hit))
            if not count:
                continue
            if blocked:
                offenders.append((label, path))
            else:
                report_counts[label] += count

    for label, count in report_counts.items():
        if count:  # zero-count labels stay silent so hook stderr is not noisy
            print(f"vault-scan: report {label}: {count}", file=sys.stderr)
    if offenders:
        label, path = offenders[0]
        print(f"vault-scan: blocked {label}: {path}", file=sys.stderr)
        return 1

    gitleaks = shutil.which("gitleaks")
    if not gitleaks:
        print("vault-scan: gitleaks not installed on this host; refusing to commit (fail-closed)", file=sys.stderr)
        return 1
    leaked = subprocess.run(
        [
            gitleaks,
            "git",
            "--staged",
            "--pre-commit",
            "--no-banner",
            "--log-level", "warn",
            "--redact",
            "--exit-code",
            "1",
            str(repo),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    sys.stdout.buffer.write(leaked.stdout)
    sys.stderr.buffer.write(leaked.stderr)
    return leaked.returncode


def self_test() -> int:
    """A repo-scoped allowlist entry passes one cited article URL; the label still blocks every other hit."""
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        r = Path(t)
        subprocess.run(["git", "init", "-q", str(r)], check=True)
        git(r, "remote", "add", "origin", "https://github.com/acme/vault.git")
        (r / "deny.json").write_text(json.dumps({
            "denylist": [{"label": "blogsite profile", "regex": r"blogsite\.com"}],
            "allowlist": [{"label": "cited", "regex": r"^https?://cited\.blogsite\.com/p/", "repos": ["acme/vault"]},
                          {"label": "other repo", "regex": "blogsite", "repos": ["acme/other"]}]}))
        (r / "sync-scan-policy.json").write_text(json.dumps(
            {"denylist_path": str(r / "deny.json"), "block_labels": ["blogsite profile"]}))
        def scan(body: str) -> str:
            (r / "a.md").write_text(body)
            git(r, "add", "a.md")
            p = subprocess.run([sys.executable, __file__, str(r)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            return p.stderr
        assert "blocked" not in scan("see (https://cited.blogsite.com/p/some-article) for doses\n")
        assert "blocked blogsite profile" in scan("me: https://you.blogsite.com\n")
        assert "blocked blogsite profile" in scan("ok https://cited.blogsite.com/p/x but https://blogsite.com/@me\n")
        assert "blocked blogsite profile" in scan("https://cited.blogsite.com/ (no /p/)\n")
    print("vault-scan self-test OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test() if sys.argv[1:] == ["--self-test"] else main())
