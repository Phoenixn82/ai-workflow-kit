#!/usr/bin/env python3
"""Read-only/fixable mechanical formatter for the second-brain Markdown vault."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3] / "second-brain"
EXCLUDED = {"_archive", "Blog", "log-archive", ".obsidian"}
FENCE = re.compile(r"^\s*```\s*(\S*)", re.I)
LINK = re.compile(r"\[\[([^\]]+)\]\]")
STOP = r"[^\s`\"')]*"
WINDOWS = [
    (re.compile(r"C:\\Users\\<you>\\Desktop\\AI_Projects" + STOP), "C:\\Users\\<you>\\Desktop\\AI_Projects", __import__('os').path.expanduser("~/AI_Projects")),
    (re.compile(r"C:/Users/<you>/Desktop/AI_Projects" + STOP), "C:/Users/<you>/Desktop/AI_Projects", __import__('os').path.expanduser("~/AI_Projects")),
    (re.compile(r"C:\\Users\\<you>\\\.claude" + STOP), "C:\\Users\\<you>\\.claude", __import__('os').path.expanduser("~/.claude")),
    (re.compile(r"C:/Users/<you>/\.claude" + STOP), "C:/Users/<you>/.claude", __import__('os').path.expanduser("~/.claude")),
    (re.compile(r"C:\\Users\\<you>\\Desktop(?!\\AI_Projects)" + STOP), "C:\\Users\\<you>\\Desktop", __import__('os').path.expanduser("~/Desktop-old-windows")),
    (re.compile(r"C:/Users/<you>/Desktop(?!/AI_Projects)" + STOP), "C:/Users/<you>/Desktop", __import__('os').path.expanduser("~/Desktop-old-windows")),
    (re.compile(r"C:\\Users\\<you>" + STOP), "C:\\Users\\<you>", __import__('os').path.expanduser("~")),
]
HUMAN = [
    re.compile(r"[A-Za-z]:[\\/][^\s`\"')]*OneDrive[^\s`\"')]*", re.I),
    re.compile(r"C:\\Users\\<other-user>\\[^\s`\"')]*claude_vault[^\s`\"')]*", re.I),
]


def tracked_files() -> list[str]:
    result = subprocess.check_output(["git", "-C", str(ROOT), "ls-files"], text=True)
    files = []
    for rel in result.splitlines():
        parts = Path(rel).parts
        in_manifest = (
            rel.endswith(".md")
            and (parts[0] in {"Actions", "Projects", "Reference", "Mechanics", "People", "dev-registry", "harness-memory"}
                 or rel in {"index.md", "boot.md", "session-state.md", "log.md"}
                 or Path(rel).name.startswith(("_CLAUDE", "START-HERE")))
        )
        if in_manifest and not any(part in EXCLUDED for part in parts) and not re.search(r"\.conflict-[^/]*\.md$", rel):
            files.append(rel)
    return sorted(files)


def read(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def write(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def author_date(rel: str) -> str:
    try:
        value = subprocess.check_output(["git", "-C", str(ROOT), "log", "-1", "--format=%as", "--", rel], text=True).strip()
        return value or dt.date.today().isoformat()
    except subprocess.CalledProcessError:
        return dt.date.today().isoformat()


def is_harness(rel: str) -> bool:
    return rel.startswith("harness-memory/")


def inferred_type(rel: str) -> str:
    name = Path(rel).name
    special = {"decisions.md": "decisions", "errors.md": "errors", "next-prompt.md": "next-prompt",
               "open-loops.md": "open-loops", "prefs.md": "prefs", "history.md": "history"}
    if name in special:
        return special[name]
    if name.startswith("HANDOFF_"):
        return "handoff"
    if rel.startswith("Actions/"):
        return "action"
    if rel.startswith("People/"):
        return "person"
    if rel.startswith("Reference/"):
        return "reference"
    if rel.startswith("Mechanics/"):
        return "mechanic-state"
    if rel.startswith("Projects/"):
        return "status" if name == "status.md" else "project"
    if rel.startswith("dev-registry/"):
        return "reference"
    if name == "index.md":
        return "index"
    if name == "boot.md":
        return "boot"
    if name == "session-state.md":
        return "session-state"
    if name == "log.md":
        return "history"
    return "reference"


def project_slug(rel: str) -> str | None:
    parts = Path(rel).parts
    return parts[1] if len(parts) > 2 and parts[0] == "Projects" else None


def frontmatter_end(text: str) -> int | None:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].lstrip("\ufeff").rstrip("\r\n") != "---":
        return None
    for index, line in enumerate(lines[1:], 1):
        if line.rstrip("\r\n") == "---":
            return index
    return -1


def ensure_frontmatter(rel: str, text: str, fix: bool, findings: list[str]) -> str:
    if rel == "harness-memory/MEMORY.md" or is_harness(rel):
        return text
    if text.startswith("\ufeff---"):
        findings.append(f"RULE1 bom-before-frontmatter {rel}")
        if fix:
            text = text.lstrip("\ufeff")
    end = frontmatter_end(text)
    if end is None:
        findings.append(f"RULE1 no-frontmatter {rel}")
        if fix:
            nl = "\r\n" if "\r\n" in text else "\n"
            fields = ["---", f"type: {inferred_type(rel)}"]
            slug = project_slug(rel)
            if slug:
                fields.append(f"project: {slug}")
            fields += ["ai-first: true", "importance: 5", f"last-touched: {author_date(rel)}", "---", ""]
            return nl.join(fields) + text
        return text
    if end == -1:
        findings.append(f"RULE2 unclosed-frontmatter {rel}")
        return text
    lines = text.splitlines(keepends=True)
    has_date = any(re.match(r"^last-touched\s*:", line) for line in lines[1:end])
    if not has_date:
        findings.append(f"RULE2 missing-last-touched {rel}")
        if fix:
            nl = "\r\n" if "\r\n" in text else "\n"
            lines.insert(end, f"last-touched: {author_date(rel)}{nl}")
            return "".join(lines)
    return text


def in_history_fence(line: str, active: str | None) -> tuple[str | None, bool]:
    match = FENCE.match(line)
    if not match:
        return active, active in {"diff", "powershell"}
    if active:
        return None, active in {"diff", "powershell"}
    language = match.group(1).lower()
    return language, language in {"diff", "powershell"}


def human_paths(line: str) -> list[str]:
    return [match.group(0) for pattern in HUMAN for match in pattern.finditer(line)]


def replace_windows(text: str, fix: bool, rel: str, findings: list[str], human: list[str]) -> str:
    if Path(rel).name.startswith("HANDOFF_") and any(rel.startswith(f"Projects/{p}/") for p in os.environ.get("VAULT_LINT_KEEP_WINDOWS_PATHS", "").split(",") if p): return text
    lines, active, output = text.splitlines(keepends=True), None, []
    for number, line in enumerate(lines, 1):
        active, skip = in_history_fence(line, active)
        trapped = human_paths(line)
        human.extend(f"{rel}:{number} → {item}" for item in trapped)
        if skip or trapped:
            output.append(line)
            continue
        changed = line
        for pattern, prefix, replacement in WINDOWS:
            def repl(match: re.Match[str]) -> str:
                suffix = match.group(0)[len(prefix):].replace("\\", "/")
                return replacement + suffix
            changed = pattern.sub(repl, changed)
        if changed != line:
            findings.append(f"RULE3 windows-path {rel}:{number}")
            output.append(changed if fix else line)
        else:
            output.append(line)
    return "".join(output)


def indexes(files: list[str]) -> tuple[set[str], dict[str, list[str]], dict[str, list[str]]]:
    exact = set(files); base, folded = defaultdict(list), defaultdict(list)
    for rel in files:
        stem = Path(rel).stem
        base[stem].append(rel); folded[stem.lower()].append(rel)
    return exact, base, folded


def resolve(target: str, rel: str, exact: set[str], base: dict[str, list[str]], folded: dict[str, list[str]]) -> str | None:
    path_target = target.replace("\\", "/")
    candidates = []
    for candidate in (Path(rel).parent / path_target, Path(path_target)):
        candidate = os.path.normpath(candidate.as_posix())
        candidates.append(candidate if candidate.endswith(".md") else candidate + ".md")
    for candidate in candidates:
        if candidate in exact:
            return candidate
    stem = Path(path_target).stem if path_target.endswith(".md") else Path(path_target).name
    matches = base.get(stem, []) or folded.get(stem.lower(), [])
    return matches[0] if len(matches) == 1 else None


def fix_links(text: str, rel: str, exact: set[str], base: dict[str, list[str]], folded: dict[str, list[str]], fix: bool, unresolved: list[str], findings: list[str]) -> str:
    line_starts = [0]
    for match in re.finditer("\n", text): line_starts.append(match.end())
    def replacement(match: re.Match[str]) -> str:
        inner = match.group(1); target, *alias = inner.split("|", 1); path, *heading = target.split("#", 1)
        found = resolve(path, rel, exact, base, folded)
        line = sum(start <= match.start() for start in line_starts)
        if not found:
            unresolved.append(f"{rel}:{line} → {path}")
            findings.append(f"RULE4 unresolved {rel}:{line} → {path}")
            return match.group(0)
        # Rule 4 is REPORT-ONLY since 2026-09-04: an earlier --fix rewrote links into
        # ../harness-memory relative form (invalid for Obsidian, wrong targets).
        # Wikilinks are vault-root-relative; prefix repairs are a human decision.
        return match.group(0)
    return LINK.sub(replacement, text)
def run(fix: bool) -> int:
    files = tracked_files(); exact, base, folded = indexes(files)
    findings: list[str] = []; unresolved: list[str] = []; human: list[str] = []
    for rel in files:
        path = ROOT / rel; text = read(path)
        changed = ensure_frontmatter(rel, text, fix, findings)
        changed = replace_windows(changed, fix, rel, findings, human)
        changed = fix_links(changed, rel, exact, base, folded, fix, unresolved, findings)
        if fix and changed != text:
            write(path, changed)
    for item in dict.fromkeys(human): print("HUMAN " + item)
    for item in dict.fromkeys(unresolved): print("UNRESOLVED " + item)
    for item in findings:
        if not item.startswith("RULE4 unresolved"):
            print(item)
    return 1 if findings else 0
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--fix", action="store_true")
    args = parser.parse_args()
    if args.check == args.fix:
        parser.error("choose exactly one of --check or --fix")
    sys.exit(run(args.fix))
