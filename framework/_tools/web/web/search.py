"""`web search`: keyless web search over DuckDuckGo's HTML endpoint, falling back to Mojeek on a block, an
error, or zero parsed results. DuckDuckGo's bot interstitial (HTTP 202, the "anomaly" challenge page) parses to no
rows; it is recorded as "ddg:blocked", not "zero-results". `hydrate()` fetches the top K ranked results' markdown through `web.read.read`, recording a
per-result error instead of aborting the whole search.
"""
from dataclasses import dataclass
from urllib.parse import quote_plus, parse_qs, unquote, urlsplit

from bs4 import BeautifulSoup

from . import read as reader
from . import netguard

DDG_URL = "https://html.duckduckgo.com/html/?q={}"
DDG_WALL = ("anomaly-modal", "anomaly.js", "bots use duckduckgo too", "challenge-form")
MOJEEK_URL = "https://www.mojeek.com/search?q={}"


class SearchError(Exception):
    """Neither engine produced a result."""
    code = 3

    def __init__(self, notes: tuple[str, ...]):
        super().__init__("no results from ddg or mojeek: " + "; ".join(notes))
        self.notes = tuple(notes)


@dataclass(frozen=True)
class SearchResult:
    rows: list[dict]
    engine: str                  # "ddg" or "mojeek": whichever produced the rows
    notes: tuple[str, ...] = ()  # trail for the footer: "<engine>:blocked|error|zero-results"


def _text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def _unwrap_ddg_href(href: str) -> str:
    qs = parse_qs(urlsplit(href).query)
    return unquote(qs["uddg"][0]) if "uddg" in qs else href


def parse_ddg(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for block in soup.select(".result"):
        a = block.select_one("a.result__a")
        if not a or not a.get("href"):
            continue
        rows.append({"title": _text(a), "url": _unwrap_ddg_href(a["href"]),
                     "snippet": _text(block.select_one(".result__snippet"))})
    return rows


def parse_mojeek(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for li in soup.select("ul.results-standard li"):
        a = li.select_one("a.title")
        if not a or not a.get("href"):
            continue
        rows.append({"title": _text(a), "url": a["href"], "snippet": _text(li.select_one("p.s"))})
    return rows


def _dedupe(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for row in rows:
        if row["url"] in seen:
            continue
        seen.add(row["url"])
        out.append(row)
    return out


_ENGINES = (("ddg", DDG_URL, parse_ddg), ("mojeek", MOJEEK_URL, parse_mojeek))


def _ddg_walled(page) -> bool:
    html = (page.html or "").lower()
    return getattr(page, "status", None) == 202 or any(m in html for m in DDG_WALL)


def search(query: str, n: int = 10) -> SearchResult:
    notes: list[str] = []
    for name, url_tmpl, parse in _ENGINES:
        url = url_tmpl.format(quote_plus(query))
        try:
            page = netguard.fetch_read(url, lane="auto")
            html = page.html
        except reader.Blocked:
            notes.append(f"{name}:blocked")
            continue
        except reader.JailFetchError:
            raise
        except reader.ReadError:
            notes.append(f"{name}:error")
            continue
        rows = _dedupe(parse(html))
        if not rows:
            notes.append(f"{name}:{'blocked' if name == 'ddg' and _ddg_walled(page) else 'zero-results'}")
            continue
        return SearchResult(rows=rows[:n], engine=name, notes=tuple(notes))
    raise SearchError(tuple(notes))


def hydrate(rows: list[dict], k: int) -> list[dict]:
    """A new list: the first `k` rows gain a "markdown" key, or "read_error" if `web.read.read` failed. Rows
    past `k` are returned unchanged."""
    out = []
    for i, row in enumerate(rows):
        if i < k:
            row = dict(row)
            try:
                row["markdown"] = netguard.fetch_read(row["url"], lane="auto").markdown
            except reader.JailFetchError:
                raise
            except reader.ReadError as e:
                row["read_error"] = str(e)
        out.append(row)
    return out
