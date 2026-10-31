"""`web read`: one URL through the read ladder (lanes.json read rungs, cheapest first), escalating on evidence.

Per rung: an error, block evidence, or a near-empty page (under SHORT_MIN chars of visible text: spec §6, never
return empty content) -> next rung. A short page (under detect.NEAR_EMPTY chars) is only accepted once another engine
in the same read returns matching text, and never when it is an HTTP >= 400 page without cf-mitigated (error walls
look the same from every engine); until then it counts as block evidence. A confirmed pair still escalates exactly one
more rung: longer accepted content there wins, otherwise the cheaper rung of the pair is returned
("short-confirmed:<a>+<b>"). The first thin (unblocked, under detect.THIN_TEXT) page is kept as a fallback and the
ladder escalates once more; a second thin page confirms it is legitimately short, and the first one is returned.
Anything else is the answer.

Walls memo: the ladder starts at the domain's memo rung. On success the memo becomes the lowest rung tried that did
not show wall evidence (detect.block_reason), so errors, timeouts and short/near-empty pages never raise it. If a
memo'd start fails without wall evidence, the skipped lower rungs are retried before giving up, and a success there
lowers the memo.

Failure: an unblocked thin fallback is returned ("thin-accepted"); otherwise Blocked (exit 3) when any rung showed
block evidence, ReadError (exit 5) when every rung failed without one (DNS, crash, missing engine).

Budget (`web read --budget`, default 110 s: agents call web under a two-minute tool timeout): a total for the whole
read. Each rung's timeout is capped to what is left (minus the scope's stop grace), and a rung that would get less
than BUDGET_FLOOR seconds isn't started: the read stops with BudgetExhausted (exit 3, the evidence so far plus
BUDGET_NOTE). A thin fallback or a confirmed short pair found before that is still returned.
"""
import contextlib
import dataclasses
import difflib
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from .clean import clean_markdown
from .config import LANES
from .detect import NEAR_EMPTY, block_reason, is_thin, visible_text
from .engines._md import to_md
from .engines.registry import ENGINES
from .fence import WEB_END, WEB_START, fence
from .runner import STOP_GRACE, run_engine, xvfb
from .walls import Walls

SHORT_MIN = 40       # fewer chars of visible text than this is never content, however many engines agree
SAME_TEXT = 0.9      # difflib ratio at which two engines' normalized visible text counts as the same page
BUDGET_FLOOR = 15    # seconds: a rung that would get less than this under --budget isn't started
BUDGET_MARGIN = 3    # seconds kept back per rung for the display and the process around it
BUDGET_NOTE = "budget exhausted; re-run with --budget 600 in the background for the stealth/zendriver rungs"


def fenced(text: str, source: str | None = None) -> str:
    """Compatibility wrapper for callers that need only the envelope text."""
    return fence(text, source).text


def _same_text(a: str, b: str) -> bool:
    a, b = " ".join(a.split()).casefold(), " ".join(b.split()).casefold()
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= SAME_TEXT


@dataclass(frozen=True)
class ReadResult:
    markdown: str
    html: str
    status: int | None
    final_url: str | None
    engine: str
    rung: int                    # 1-based position in lanes.json's read rungs
    ms: int                      # whole read, every rung tried
    blocked: str | None = None   # always None on a returned page: blocked pages escalate or raise
    thin: bool = False
    notes: tuple[str, ...] = ()  # trail for the footer: "<engine>:blocked|error|thin", "memo-retry", "thin-accepted"


class ReadError(Exception):
    """Every rung failed. `evidence` has one "<engine>/<rung>: <reason>" line per rung tried."""
    code = 5

    def __init__(self, evidence: list[str], lane: str = "-/-", notes: tuple[str, ...] = ()):
        super().__init__("; ".join(evidence))
        self.evidence, self.lane, self.notes = list(evidence), lane, tuple(notes)


class JailFetchError(ReadError):
    """A failed boundary/capture must stop the trusted parent command."""


class Blocked(ReadError):
    code = 3


class BudgetExhausted(Blocked):
    """--budget ran out before the ladder did (exit 3); the last evidence line is BUDGET_NOTE."""


class BadUrl(ReadError):
    """Not an http(s) URL with a host (exit 2). Engines are never run on file:, data:, chrome: and the like, whoever
    passed the URL in (a search result being hydrated, a crawl candidate)."""
    code = 2


def read(url: str, lane: str = "auto", timeout: float | None = None, budget: float | None = None) -> ReadResult:
    """`lane` is "auto" (the ladder, from the walls memo's start rung) or one engine name (that rung only, memo
    untouched). `timeout` overrides every rung's own timeout (seconds); `budget` caps the whole read (seconds)."""
    try:
        parts = urlsplit(url)
        ok = parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        ok = False
    if not ok:
        raise BadUrl([f"not an http(s) URL: {url!r}"])
    rungs = LANES["read"]["rungs"]
    domain = (urlsplit(url).hostname or "").lower()
    walls = None
    if lane == "auto":
        walls = Walls()
        start = min(walls.start_rung(domain), len(rungs))
        plan = range(start, len(rungs) + 1)
    elif lane in rungs:
        start = rungs.index(lane) + 1
        plan = [start]
    else:
        raise ValueError(f"unknown read lane {lane!r}: auto or one of {', '.join(rungs)}")

    t0 = time.monotonic()
    out_of_budget = False
    evidence, notes, tried = [], [], []
    walled, blocked = set(), set()   # walled: block_reason fired (moves the memo); blocked: any block evidence (exit 3)
    fallback, last, shorts = None, "-/-", []   # shorts: (page, visible text) awaiting a second engine's agreement
    # Upper bound on the private display's life, even if web is killed (the memo retry can run every rung).
    display_max = sum((timeout or ENGINES[n]["timeout"]) + STOP_GRACE for n in rungs if ENGINES[n]["headed"]) + 60

    with contextlib.ExitStack() as stack:
        display = display_error = None

        def rung_timeout(name: str) -> float | None:
            """This rung's timeout under the budget, or None when it can't get BUDGET_FLOOR seconds."""
            full = timeout or ENGINES[name]["timeout"]
            if budget is None:
                return full
            left = budget - (time.monotonic() - t0) - STOP_GRACE - BUDGET_MARGIN
            return min(full, left) if left >= BUDGET_FLOOR else None

        def run(name: str, rung_t: float) -> dict:
            nonlocal display, display_error
            eng = ENGINES[name]
            if eng["headed"] and display is None:
                if display_error is None:   # one display per read, started on the first headed rung, tried once
                    try:
                        display = stack.enter_context(xvfb(max_sec=display_max))
                    except Exception as e:  # noqa: BLE001 - a broken display is these rungs' error, not a crash
                        display_error = f"display: {e}"
                if display is None:
                    return {"error": display_error}
            return run_engine(eng["argv"], url, rung_t, display=display if eng["headed"] else None,
                              headed=eng["headed"])

        def assess(rung: int) -> tuple[str, object]:
            """Run one rung and judge it: ("error"|"blocked"|"short", None), ("confirmed", (page, note)),
            or ("thin"|"content", page)."""
            nonlocal last, out_of_budget
            name = rungs[rung - 1]
            if (rung_t := rung_timeout(name)) is None:
                out_of_budget = True
                return "budget", None
            last = f"{name}/{rung}"
            tried.append(rung)
            rec = run(name, rung_t)
            if rec["error"]:
                evidence.append(f"{last}: error: {' '.join(str(rec['error']).split())}")
                notes.append(f"{name}:error")
                return "error", None
            html, status = rec["html"], rec["status"]
            text = visible_text(html)[0]
            reason = block_reason(status, rec["headers"], html)
            if reason:
                walled.add(rung)
            elif len(text) < SHORT_MIN:
                reason = f"near-empty page ({len(text)} chars of text)"
            if reason:
                blocked.add(rung)
                evidence.append(f"{last}: {reason}")
                notes.append(f"{name}:blocked")
                return "blocked", None
            page = ReadResult(markdown=clean_markdown(rec["markdown"] or to_md(html)), html=html, status=status,
                              final_url=rec["final_url"], engine=name, rung=rung, ms=0, thin=is_thin(html))
            if len(text) < NEAR_EMPTY:
                notes.append(f"{name}:short")
                # A short error page (an Apache 404, a 429 checkpoint, a WAF 405) reads the same from every engine,
                # so agreement proves nothing. Cleared Cloudflare challenges are the exception: the first response
                # still carries 403 + cf-mitigated.
                if (status or 0) >= 400 and not any(k.lower() == "cf-mitigated" for k in rec["headers"]):
                    blocked.add(rung)
                    evidence.append(f"{last}: short page ({len(text)} chars of text) with HTTP {status}, not confirmable")
                    return "short", None
                for other, other_text in shorts:
                    if _same_text(other_text, text):
                        a, b = sorted((other, page), key=lambda p: p.rung)   # the cheaper rung's page stands
                        return "confirmed", (a, f"short-confirmed:{a.engine}/{a.rung}+{b.engine}/{b.rung}")
                shorts.append((page, text))
                blocked.add(rung)   # evidence until another engine agrees
                evidence.append(f"{last}: short page ({len(text)} chars of text), no second engine agreed")
                return "short", None
            if page.thin:
                notes.append(f"{name}:thin")
                return "thin", page
            return "content", page

        def climb(rung_numbers) -> ReadResult | None:
            nonlocal fallback
            confirmed = None   # (page, note): a short page two engines agree on, held while ONE more rung is tried
            for rung in rung_numbers:
                kind, got = assess(rung)
                if kind == "budget":
                    break
                if confirmed:
                    if kind in ("thin", "content"):   # accepted longer content beats the short pair
                        return got
                    break
                if kind == "confirmed":
                    confirmed = got
                elif kind == "content":
                    return got
                elif kind == "thin":
                    if fallback is None:
                        fallback = got
                        continue
                    notes.append("thin-accepted")   # confirmed short: the cheaper first thin page stands
                    return fallback
            if confirmed:
                notes.append(confirmed[1])
                return confirmed[0]
            return None

        page = climb(plan)
        if page is None and fallback is None and walls and start > 1 and not walled and not out_of_budget:
            notes.append("memo-retry")   # the memo'd rungs failed without a wall in sight: try the cheaper ones
            page = climb(range(1, start))
        if page is None and fallback is not None:
            notes.append("thin-accepted")
            page = fallback

    if page is None and out_of_budget:
        raise BudgetExhausted([*evidence, BUDGET_NOTE], lane=last, notes=(*notes, "budget"))
    if page is None:
        raise (Blocked if blocked else ReadError)(evidence, lane=last, notes=tuple(notes))
    if walls and domain:
        lowest = min([page.rung] + [r for r in tried if r < page.rung and r not in walled])
        with contextlib.suppress(OSError):   # the memo is an optimisation; never fail a good read over it
            walls.record(domain, lowest)
    return dataclasses.replace(page, ms=int((time.monotonic() - t0) * 1000), notes=tuple(notes))
