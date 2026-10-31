#!/usr/bin/env python3
"""check_docs.py: the top-level docs hold together. Stdlib only, offline.

- README.md follows the house order: H1, a one-line description right under it, then the sections
  What it is, Features, Tech stack, Getting started, How it works, License (in that order), with at least
  one fenced code example.
- Every relative link (and #anchor into a .md file) in README.md, implementation-prompt.md, CHANGELOG.md
  and docs/*.md points at something that exists in the repo.

Usage: python3 tools/check_docs.py [--self-test]. Exit 0 when clean, 1 with one line per problem.
"""
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORDER = ["What it is", "Features", "Tech stack", "Getting started", "How it works", "License"]
LINK = re.compile(r"\]\(([^)\s]+)\)")


def slug(heading):
    """GitHub's anchor for a heading: lowercase, drop punctuation except - and _, spaces to -."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def strip_code(text):
    return re.sub(r"```.*?```", "", text, flags=re.S)


def readme_problems(path):
    text = open(path, encoding="utf-8").read()
    lines = text.splitlines()
    out = []
    if not lines or not lines[0].startswith("# "):
        out.append(f"{path}: first line must be the H1 title")
    elif len(lines) < 3 or lines[1].strip() or not lines[2].strip() or lines[2].startswith("#"):
        out.append(f"{path}: a one-line description must sit right under the title")
    h2 = [h.strip() for h in re.findall(r"^## (.+)$", strip_code(text), re.M)]
    pos = []
    for name in ORDER:
        if name not in h2:
            out.append(f"{path}: missing section '## {name}'")
        else:
            pos.append(h2.index(name))
    if pos != sorted(pos):
        out.append(f"{path}: sections out of order, want: {', '.join(ORDER)}")
    if "```" not in text:
        out.append(f"{path}: needs at least one fenced code example")
    return out


def link_problems(path, root):
    out = []
    text = strip_code(open(path, encoding="utf-8").read())
    base = os.path.dirname(path)
    for target in LINK.findall(text):
        if re.match(r"^[a-z]+:", target):
            continue
        rel, _, anchor = target.partition("#")
        dest = os.path.normpath(os.path.join(base, rel)) if rel else path
        shown = os.path.relpath(path, root)
        if not os.path.exists(dest):
            out.append(f"{shown}: broken link {target}")
        elif anchor and dest.endswith(".md"):
            heads = [slug(h) for h in re.findall(r"^#+ (.+)$", strip_code(open(dest, encoding="utf-8").read()), re.M)]
            if anchor not in heads:
                out.append(f"{shown}: no heading for #{anchor} in {os.path.relpath(dest, root)}")
    return out


def check(root):
    files = [os.path.join(root, f) for f in ("README.md", "implementation-prompt.md", "CHANGELOG.md")]
    docs = os.path.join(root, "docs")
    if os.path.isdir(docs):
        files += sorted(os.path.join(docs, f) for f in os.listdir(docs) if f.endswith(".md"))
    out = readme_problems(os.path.join(root, "README.md"))
    for f in files:
        if os.path.exists(f):
            out += link_problems(f, root)
    return out


def self_test():
    with tempfile.TemporaryDirectory() as t:
        os.makedirs(os.path.join(t, "docs"))
        good = "# Kit\n\nOne line.\n\n" + "".join(f"## {s}\n\ntext\n\n" for s in ORDER) + "```bash\nx\n```\n[d](docs/a.md#two-words)\n"
        open(os.path.join(t, "README.md"), "w").write(good)
        open(os.path.join(t, "docs", "a.md"), "w").write("# A\n\n## Two words\n\n[back](../README.md)\n")
        assert check(t) == [], check(t)
        open(os.path.join(t, "docs", "b.md"), "w").write("[x](../nope.md) [y](a.md#missing) [z](https://e.x)\n")
        got = check(t)
        assert any("broken link ../nope.md" in p for p in got) and any("#missing" in p for p in got) and len(got) == 2, got
        bad = good.replace("## Features\n", "## Feats\n").replace("One line.", "## What")
        open(os.path.join(t, "README.md"), "w").write(bad)
        got = readme_problems(os.path.join(t, "README.md"))
        assert any("one-line description" in p for p in got) and any("'## Features'" in p for p in got), got
    print("check_docs self-test: ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
        sys.exit(0)
    problems = check(ROOT)
    for p in problems:
        print(p)
    print(f"check_docs: {'clean' if not problems else f'{len(problems)} problem(s)'}")
    sys.exit(1 if problems else 0)
