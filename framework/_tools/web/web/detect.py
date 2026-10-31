"""Block evidence: is the FINAL document a bot wall, a login wall or an empty shell rather than content?

The verdict keys on the document itself:
- status codes and the cf-mitigated header only corroborate a body that already reads as a denial. Browser engines
  report the FIRST navigation response, so a challenge that was cleared still carries 403 + cf-mitigated while the
  body is the real page;
- challenge-page structure counts, vendors' always-loaded scripts don't (Cloudflare's
  /cdn-cgi/challenge-platform/scripts/jsd/main.js, Incapsula/PerimeterX sensors ride along on ordinary pages);
- a short page is not a blocked page: is_thin() is the separate, non-blocking "suspect" signal.
"""
import re
from html.parser import HTMLParser

LONG_TEXT = 3000   # walls are short; real pages *about* captchas are long
THIN_TEXT = 512    # below this a page is "thin": suspect (escalate once), never blocked on length alone
NEAR_EMPTY = 200   # "otherwise empty": vendor widgets and CSR shells only count below this

# Challenge-page structure in the raw HTML (the challenge itself, not the vendor's tracking script).
WALL_HTML = ("cf_chl_opt", "/cdn-cgi/challenge-platform/h/", "px-captcha")
WALL_TITLE = re.compile(r"<title[^>]*>\s*(just a moment\.\.\.|attention required! \| cloudflare)\s*</title>", re.I)
# Vendor widgets/scripts that also appear on ordinary pages: wall evidence only on an otherwise-empty page.
WALL_HTML_EMPTY = ("cf-turnstile", "captcha-delivery.com", "_incapsula_resource")
# Wall wording in the visible text.
WALL_TEXT = (
    "verify you are human", "are you a robot", "whoa there, pardner", "blocked by network security",
    "prove your humanity", "please enable js and disable any ad blocker", "performing security verification",
    "checking your browser before accessing", "pardon our interruption", "incapsula incident id",
    "access denied", "you have been blocked",
)
# Login wording is only wall evidence where it dominates the page (title/first h1), not wherever it occurs in the
# body: a real article can say "please log in to comment" in a footer without being a login wall.
LOGIN_TEXT = ("log in to continue", "login to continue", "sign in to continue", "log in to use old reddit")
LOGIN_SHORT_TEXT = 1500   # a page this short, titled/headed by a login prompt, is the login wall itself
PASSWORD_SHORT_TEXT = 400  # a bare password input only counts on a page this short: a login widget on a real,
                            # longer page (e.g. a sidebar member-login box) isn't the page's reason for being
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
PASSWORD_INPUT = re.compile(r"<input\b[^>]*\btype=[\"']?password\b", re.I)
LOGIN_HTML = re.compile(r"page[-_]?type[\"'=:\s]+login\b", re.I)   # login-route metadata of a JS shell
# Denial wording that needs a 403/429/503 or cf-mitigated header to count.
DENIAL_TEXT = ("forbidden", "too many requests", "rate limit", "unusual traffic", "request blocked", "not authorized")
MOUNT = re.compile(r"<(?:div|main)\b[^>]*\bid=[\"'](?:root|app|__next|__nuxt|___gatsby|svelte)[\"']|<app-root\b|data-reactroot", re.I)
BOT_STATUS = {403, 429, 503}
_SKIP = {"script", "style", "noscript", "template"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip, self.scripts = [], 0, 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
        if tag == "script":
            self.scripts += 1

    def handle_endtag(self, tag):
        if tag in _SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def visible_text(html: str) -> tuple[str, int]:
    p = _Text()
    p.feed(html or "")
    p.close()
    return re.sub(r"\s+", " ", " ".join(p.parts)).strip(), p.scripts


def is_thin(html: str) -> bool:
    """Little visible text: suspect, not blocked. Callers escalate once and accept the page if the next rung
    returns the same thin content (product grids and anti-bot success pages are legitimately short)."""
    return len(visible_text(html)[0]) < THIN_TEXT


def block_reason(status: int | None, headers: dict | None, html: str) -> str | None:
    """Why the final document looks like a wall or an empty shell, or None if it looks like content."""
    text, scripts = visible_text(html)
    n = len(text)
    if n >= LONG_TEXT:
        return None
    low, tlow = (html or "").lower(), text.lower()
    for m in WALL_HTML:
        if m in low:
            return f"marker: {m}"
    if t := WALL_TITLE.search(html or ""):
        return f"marker: {t.group(1).lower()}"
    for m in WALL_TEXT:
        if m in tlow:
            return f"marker: {m}"
    if n < NEAR_EMPTY:
        for m in WALL_HTML_EMPTY:
            if m in low:
                return f"marker: {m}"
    def _has_login_phrase(tag_match):
        if not tag_match:
            return False
        inner = re.sub(r"<[^>]+>", " ", tag_match.group(1)).lower()
        return any(m in inner for m in LOGIN_TEXT)

    phrase_dominant = (_has_login_phrase(TITLE_RE.search(html or "")) or _has_login_phrase(H1_RE.search(html or "")))
    password_dominant = n < PASSWORD_SHORT_TEXT and bool(PASSWORD_INPUT.search(html or ""))
    if (phrase_dominant and n < LOGIN_SHORT_TEXT) or password_dominant or (n < THIN_TEXT and LOGIN_HTML.search(html or "")):
        return "login wall"
    cf = any(k.lower() == "cf-mitigated" for k in (headers or {}))
    if (cf or status in BOT_STATUS) and n < THIN_TEXT and (not text or any(m in tlow for m in DENIAL_TEXT)):
        return "cf-mitigated header" if cf else f"http {status}"
    if n < NEAR_EMPTY and scripts >= 3 and (MOUNT.search(html or "") or "enable javascript" in tlow):
        return "csr shell"
    return None
