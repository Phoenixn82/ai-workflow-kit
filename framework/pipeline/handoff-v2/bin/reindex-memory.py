#!/usr/bin/env python3
"""Rebuild MEMORY.md: feedback rules verbatim, everything else as a grep table.

The 2026-09-12 audit showed feedback_* one-liners ARE the delivery mechanism for
behavioral rules (unopened != unapplied), while project/reference/infra facts are
looked up by name when needed. So rules keep their full index line; facts collapse
to `slug — 6-word hook` rows grouped by type. No file moves. Dry run by default.
"""
import re, sys
from pathlib import Path

M = Path.home() / ".claude/projects/-home-you-AI-Projects/memory"
IDX = M / "MEMORY.md"
apply = "--apply" in sys.argv

def fm(text):
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    out = {}
    if m:
        for ln in m.group(1).splitlines():
            if ":" in ln:
                k, v = ln.split(":", 1); out[k.strip()] = v.strip().strip('"')
    return out

current = {}
for ln in IDX.read_text().splitlines():
    mt = re.match(r"^- \[[^\]]*\]\(([^)]+)\)", ln)
    if mt: current[mt.group(1)] = ln

rules, facts = [], {}
for p in sorted(M.glob("*.md")):
    if p.name == "MEMORY.md": continue
    meta = fm(p.read_text(errors="replace"))
    typ = meta.get("type") or ("feedback" if p.name.startswith("feedback") else "project")
    if typ == "feedback" or p.name.startswith(("feedback", "error_")):
        rules.append(current.get(p.name, f"- [{p.stem}]({p.name}) — {meta.get('description','')[:90]}"))
    else:
        hook = re.sub(r"\s+", " ", meta.get("description", "")).strip()
        hook = " ".join(hook.split()[:7])
        facts.setdefault(typ, []).append(f"{p.stem} — {hook}")

lines = ["# Memory index — AI_Projects", "",
         "> Rules below are instructions: read them. Facts are a lookup table: `Read memory/<slug>.md` only when the slug matches your task.", "",
         f"## Rules ({len(rules)})"] + rules + [""]
for typ in sorted(facts):
    lines += [f"## {typ} ({len(facts[typ])}) — grep, don't read", ""]
    lines += [f"- {f}" for f in facts[typ]] + [""]
new = "\n".join(lines).rstrip() + "\n"
old = IDX.read_text()
print(f"MEMORY.md: {len(old.encode())//4:,} tok -> {len(new.encode())//4:,} tok  ({len(rules)} rules verbatim, {sum(map(len,facts.values()))} facts compressed)")
if apply:
    IDX.with_suffix(".md.pre-reindex").write_text(old); IDX.write_text(new); print("applied (backup MEMORY.md.pre-reindex)")
