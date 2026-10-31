"""`web crawl`: bounded, sequential crawl of one host. Discovery tries, in order: the site's robots.txt
`Sitemap:` lines, then a default `/sitemap.xml`, recursing through sitemap indexes; if neither yields anything,
same-host links from the start page stand in (that read of the start page is also its result: it isn't read
twice). Every candidate URL is read through `web.read.read` (so the walls
memo and the engine ladder are reused exactly as `web read` uses them), one at a time - no parallel requests to
the same host. `robots.txt` Disallow rules are honoured for every candidate via `urllib.robotparser`.

Output: `<out>/manifest.jsonl` (one line per attempted URL: url, status, engine, rung, ms, and either `file` or
`error`) plus one `<NNNN>.md` per page successfully read. Re-running on the same `--out` resumes: any URL
already present in the manifest (success or failure) is skipped, and new file numbers continue after the
highest one already on disk.

Budget (`--budget`): a total for the whole crawl. Each read gets what is left of it (read's own budget), a page that
couldn't get read.BUDGET_FLOOR seconds isn't started, and a read cut short by the budget isn't written to the
manifest, so the next run picks it up. `budget_exhausted` says the crawl stopped early.
"""
import contextlib
import fnmatch
import json
import time
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from defusedxml import ElementTree

from . import read as reader
from .fence import fence

FETCH_TIMEOUT = 15
MAX_SITEMAP_DEPTH = 5


def _fetch_text(url: str) -> str | None:
    """A plain GET, no JS engines - robots.txt and sitemap.xml are always static. None on any failure."""
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - DNS, timeout, HTTP error: all just "no sitemap/robots here"
        return None


def _same_host(url: str, host: str) -> bool:
    return (urlsplit(url).hostname or "").lower() == host


def _parse_sitemap_locs(xml_text: str) -> tuple[bool, list[str]]:
    """(is_index, locs). Any parse failure - malformed XML or a defused-XML bomb (billion laughs, external
    entities) - comes back as (False, []): "no sitemap data here", never an exception."""
    try:
        root = ElementTree.fromstring(xml_text)
    except Exception:  # noqa: BLE001 - defusedxml raises its own exception types, not just ParseError
        return False, []
    locs = [loc.text.strip() for loc in root.iter() if loc.tag.rsplit("}", 1)[-1] == "loc" and loc.text]
    return root.tag.rsplit("}", 1)[-1] == "sitemapindex", locs


def _sitemap_urls(loc: str, fetch, visited: set[str] | None = None, depth: int = 0) -> list[str]:
    """URLs from the sitemap (or sitemap index) at `loc`, recursing into child sitemaps. `visited` and
    MAX_SITEMAP_DEPTH both bound the recursion, so a self-referencing or cyclic sitemapindex terminates."""
    visited = visited if visited is not None else set()
    if loc in visited or depth >= MAX_SITEMAP_DEPTH:
        return []
    visited.add(loc)
    xml = fetch(loc)
    if not xml:
        return []
    is_index, locs = _parse_sitemap_locs(xml)
    if not is_index:
        return locs
    urls = []
    for child in locs:
        urls += _sitemap_urls(child, fetch, visited, depth + 1)
    return urls


def robots_parser(start_url: str, fetch=_fetch_text) -> urllib.robotparser.RobotFileParser:
    parts = urlsplit(start_url)
    rp = urllib.robotparser.RobotFileParser()
    text = fetch(f"{parts.scheme}://{parts.netloc}/robots.txt")
    rp.parse((text or "").splitlines())
    return rp


def discover(start_url: str, rp: urllib.robotparser.RobotFileParser, fetch=_fetch_text, read_page=None) -> list[str]:
    """Candidate same-host URLs: sitemap(s) if any are found, else same-host links off the start page. The
    start URL itself always leads the list."""
    read_page = read_page or reader.read
    parts = urlsplit(start_url)
    host = (parts.hostname or "").lower()
    base = f"{parts.scheme}://{parts.netloc}"

    sitemaps = list(rp.site_maps() or []) or [f"{base}/sitemap.xml"]
    visited: set[str] = set()
    found = []
    for loc in sitemaps:
        found += _sitemap_urls(loc, fetch, visited)
    candidates = [u for u in found if _same_host(u, host)]

    if not candidates:
        with contextlib.suppress(reader.ReadError):
            page = read_page(start_url)
            soup = BeautifulSoup(page.html, "html.parser")
            seen = set()
            for a in soup.find_all("a", href=True):
                u = urljoin(start_url, a["href"]).split("#", 1)[0]
                if _same_host(u, host) and u not in seen:
                    seen.add(u)
                    candidates.append(u)

    if start_url in candidates:
        candidates.remove(start_url)
    return [start_url] + candidates


@dataclass
class CrawlStats:
    ok: int = 0
    blocked: int = 0
    error: int = 0
    skipped: int = 0
    budget_exhausted: bool = False
    hidden: int = 0
    tags: int = 0


def _load_manifest(path: Path) -> tuple[set[str], int]:
    """URLs already recorded, and the highest existing file number (0 if none)."""
    done, file_n = set(), 0
    if not path.exists():
        return done, file_n
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        with contextlib.suppress(ValueError, KeyError):
            rec = json.loads(line)
            done.add(rec["url"])
            if rec.get("file"):
                with contextlib.suppress(ValueError):
                    file_n = max(file_n, int(Path(rec["file"]).stem))
    return done, file_n


def crawl(start_url: str, out_dir: Path, max_pages: int = 50, match: str | None = None,
          fetch=_fetch_text, read_page=None, budget: float | None = None) -> CrawlStats:
    raw_read = read_page or reader.read
    t0 = time.monotonic()

    def left() -> float | None:
        return None if budget is None else budget - (time.monotonic() - t0)

    def read_page(url):   # every read (the start page's link discovery too) gets what is left of the budget
        return raw_read(url) if budget is None else raw_read(url, budget=left())

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.jsonl"

    done, file_n = _load_manifest(manifest_path)
    rp = robots_parser(start_url, fetch=fetch)
    seen: dict = {}   # what discovery's read of the start page got (a page or the ReadError): never read it twice

    def discover_read(url):
        try:
            seen[url] = read_page(url)
        except reader.ReadError as e:
            seen[url] = e
            raise
        return seen[url]

    candidates = discover(start_url, rp, fetch=fetch, read_page=discover_read)
    if match:
        candidates = [u for u in candidates if fnmatch.fnmatch(u, match)]
    start_host = (urlsplit(start_url).hostname or "").lower()

    stats = CrawlStats()
    attempted = 0
    with manifest_path.open("a", encoding="utf-8") as manifest:
        for url in candidates:
            if attempted >= max_pages:
                break
            if url in done:
                continue
            if not rp.can_fetch("*", url):
                stats.skipped += 1
                continue
            if budget is not None and left() < reader.BUDGET_FLOOR:
                stats.budget_exhausted = True
                break
            attempted += 1
            done.add(url)
            t1 = time.monotonic()
            try:
                r = seen.pop(url) if url in seen else read_page(url)
                if isinstance(r, reader.ReadError):
                    raise r
            except reader.BudgetExhausted:   # not this page's fault: leave it out of the manifest for the next run
                stats.budget_exhausted = True
                break
            except reader.Blocked as e:
                stats.blocked += 1
                row = {"url": url, "status": None, "engine": None, "rung": None,
                       "ms": int((time.monotonic() - t1) * 1000), "error": str(e)}
            except reader.ReadError as e:
                stats.error += 1
                row = {"url": url, "status": None, "engine": None, "rung": None,
                       "ms": int((time.monotonic() - t1) * 1000), "error": str(e)}
            else:
                final_host = (urlsplit(r.final_url or url).hostname or "").lower()
                if final_host != start_host:
                    stats.skipped += 1
                    row = {"url": url, "status": r.status, "engine": r.engine, "rung": r.rung, "ms": r.ms,
                           "error": f"redirected off-host: {final_host}"}
                else:
                    file_n += 1
                    fname = f"{file_n:04d}.md"
                    page = fence(r.markdown, source=r.final_url or url)
                    (out_dir / fname).write_text(page.text, encoding="utf-8")
                    stats.hidden += page.hidden
                    stats.tags += page.tags
                    row = {"url": url, "status": r.status, "engine": r.engine, "rung": r.rung, "ms": r.ms,
                           "file": fname}
                    stats.ok += 1
            manifest.write(json.dumps(row, ensure_ascii=False) + "\n")
            manifest.flush()
    return stats
