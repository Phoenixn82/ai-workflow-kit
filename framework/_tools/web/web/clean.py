"""Markdown hygiene for scraped pages: strip inline base64 images and the leading nav-menu/cookie-banner
lines that sit ahead of the real content in most converted pages."""
import re

DATA_IMAGE = re.compile(r"!\[[^\]]*\]\(data:[^)]*\)")
# `* [text](url)` or a bare `[text](url)` alone on a line: nothing but a nav/menu link.
NAV_LINE = re.compile(r"^(?:\*\s+)?\[[^\]]+\]\([^)]+\)\s*$")
HEADING_LINE = re.compile(r"^#{1,6}\s")
LONG_PARAGRAPH = 40


def clean_markdown(md: str) -> str:
    md = DATA_IMAGE.sub("", md)
    lines = md.split("\n")

    boundary = None
    for i, line in enumerate(lines):
        s = line.strip()
        if NAV_LINE.match(s):
            continue
        if HEADING_LINE.match(s) or len(s) >= LONG_PARAGRAPH:
            boundary = i
            break

    if boundary is not None:
        lines = [line for i, line in enumerate(lines) if i >= boundary or not NAV_LINE.match(line.strip())]
    md = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", md)
