"""Common HTML->markdown for engines without their own converter (same converter = fair comparison)."""
import re
from markdownify import markdownify

_DROP = re.compile(r"<(script|style|noscript|template|svg)\b.*?</\1>", re.S | re.I)


def to_md(html: str) -> str:
    return markdownify(_DROP.sub("", html or ""), heading_style="ATX")
