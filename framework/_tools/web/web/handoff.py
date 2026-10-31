"""`web handoff`: put a human-only step (a CAPTCHA, 2FA, a Google sign-in) on the user's phone, then wait for it to
clear.

Detached: agent-browser lets go of the identity browser for the whole handoff (its CDP session is exactly what bot
checks flag). web drives the live view itself over
the browser's loopback CDP with nothing a page can detect: Page.startScreencast frames, Input.* events and one-shot
Runtime.evaluate calls, never Runtime.enable. The next `web act` reattaches agent-browser.

The relay listens on 127.0.0.1:<free port> only:
  GET /h/<token>     the phone page (handoff_page.html)
  WS  /h/<token>/ws  one phone at a time: screencast frames down; allowlisted input up (mouse press/release/move/
                     wheel, key down/up/char, inserted text) plus frame acks
  anything else      404
`tailscale serve` publishes it under /web-handoff on the plain-HTTP tailnet listener (tailnet-only; tailscale strips
the mount). The token is fresh per handoff and dies with the relay: when handoff() returns, however it returns, the
tailscale path is removed, the relay and screencast stop and the CDP sockets close.

Cleared: --until-url / --until-selector when given (a start page still loading, or a condition already true at the
start, is refused: exit 2; once the start tab closes, a tab the condition already held on counts only after it has
navigated). With neither, the start page must show a human gate (a CAPTCHA widget, a one-time-code or password
input, verification wording, or a full-page wall), else exit 2; it clears once the START tab has moved to another
http(s) URL (a bot wall may clear in place) with no gate left on it, nor on a popup the phone is showing. Either way
the page must have finished loading and show some text, on two polls in a row on the same URL.
"""
import asyncio
import contextlib
import fnmatch
import hmac
import itertools
import json
import math
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from html import unescape
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect as sync_connect

from . import act, bg, notify
from .detect import block_reason, visible_text

MOUNT = "/web-handoff"
LISTENER = ["--http", "80"]   # the plain-HTTP tailnet listener (tailnet HTTPS certs are not enabled)
POLL_SEC = 2.0
MAX_TIMEOUT = 900.0           # spec §3.4: a handoff link lives at most 15 minutes
WATCH_SEC = 1.0               # how often the relay looks for a tab that opened (a sign-in popup) or closed
_W, _H = act.SCREEN.split("x")[:2]
# Never downscaled (the cap is the whole Xvfb screen): a frame pixel is a page CSS pixel, which is how the page maps taps.
SCREENCAST = {"format": "jpeg", "quality": 80, "maxWidth": int(_W), "maxHeight": int(_H)}
MOUSE_EVENTS = {"mousePressed", "mouseReleased", "mouseMoved", "mouseWheel"}
KEY_EVENTS = {"keyDown": "keyDown", "keyUp": "keyUp", "char": "char"}
BUTTONS = {"none": 0, "left": 1, "right": 2, "middle": 4}
MAX_TEXT = 1000
PAGE = Path(__file__).with_name("handoff_page.html")
CSP = ("default-src 'none'; img-src data:; connect-src 'self'; script-src 'unsafe-inline'; "
       "style-src 'unsafe-inline'")
SELECTOR_JS = "(() => { try { return !!document.querySelector(%s); } catch (e) { return false; } })()"
# One read of a tab: where it is, whether it finished loading, which document it is (timeOrigin: a navigation, even
# to the same URL, starts a new one), how much text it shows, the --until-selector's answer, the page (no-condition).
STATE_JS = ("({url: location.href, ready: document.readyState, origin: performance.timeOrigin, "
            "text: ((document.body && document.body.innerText) || '').trim().length, sel: %s, html: %s})")
CLEAN_POLLS = 2               # clean polls in a row, on one URL, before "cleared" (one can be a page between states)
MIN_TEXT = 40                 # a page showing less text than this (a blank or a spinner) is never "done"
START_TRIES, START_TRY_SEC = 10, 0.5   # the start check waits up to 5 s for the page to finish loading
LOADING = "page still loading — retry"

# The human gate a no-condition handoff waits out (an embedded CAPTCHA or a 2FA prompt reads as "not a wall" to
# detect.block_reason, which only knows full-page walls). Widgets: an iframe or script loaded from the vendor.
GATE_WIDGETS = (("recaptcha", r"(?:google\.com|recaptcha\.net)/recaptcha"), ("hcaptcha", r"hcaptcha\.com"),
                ("turnstile", r"challenges\.cloudflare\.com"), ("datadome", r"captcha-delivery\.com"),
                ("duo", r"duosecurity\.com"), ("arkose", r"arkoselabs\.com|funcaptcha"),
                ("perimeterx", r"perimeterx|px-captcha"), ("geetest", r"geetest"), ("mtcaptcha", r"mtcaptcha"))
_SRC = r"(?<![\w-])src\s*=\s*[\"']?"   # src itself, not a lazy loader's data-src (nothing loaded from it yet)
_WIDGET_RES = [(n, re.compile(r"<(?:iframe|script)\b[^>]*" + _SRC + r"[^\"'>\s]*(?:" + h + ")", re.I))
               for n, h in GATE_WIDGETS]
_IFRAME_SRC = re.compile(r"<iframe\b[^>]*?" + _SRC + r"([^\"'>\s]+)", re.I)
_INPUT = re.compile(r"<input\b[^>]*>", re.I)
_ATTR = r"(?<![\w-]){}\s*=\s*[\"']?([^\"'>\s]*)"
# One-time-code fields by name/id: code, otp, totp or pin anywhere in it (totpPin, idvPin, smsotpfield,
# verificationcode), unless a word in it makes it something else (zipcode, postCode, shipping, spinner, promo_code,
# countryCode, areaCode, referralCode, giftCode, voucherCode, barCode, inviteCode).
_OTP_NAME = re.compile(r"code|otp|totp|pin")
NOT_OTP = ("zip", "postal", "post", "ship", "spinner", "promo", "coupon", "discount", "country", "gift", "voucher",
           "area", "referral", "invite", "bar")
TEXT_INPUTS = {"", "text", "number", "tel", "password"}
SPLIT_OTP = 4                 # this many one-digit numeric boxes in a row are one code split across inputs
GATE_TEXT = ("2-step verification", "verify it's you", "verification code", "approve sign in", "approve sign-in",
             "number matching", "enter the number", "check your phone", "selfie video")
_ENTER_CODE = re.compile(r"enter (?:the )?(?:\d+[- ]digit )?(?:verification |security )?code")
# A full-page bot wall (Cloudflare's interstitial) is solved at the same URL, so its going away is enough; these
# block_reason values are not bot walls, and like every other gate they also need the URL to move on.
NOT_SOLVED_IN_PLACE = ("login wall", "csr shell")
NO_GATE = "give --until-url or --until-selector; this page doesn't look like a human gate"
ALREADY = "the until condition is already true — give a condition that becomes true only after the human step"


class HandoffError(Exception):
    def __init__(self, message: str, code: int = 5):
        super().__init__(message)
        self.code = code


def _script_json(value) -> str:
    """JSON safe inside <script type="application/json">: no `</script` or `<!--` can form."""
    return json.dumps(value).replace("<", "\\u003c")


# --- CDP, read side (plain HTTP or one-shot evaluate: nothing stays attached) ---

def _page_targets(port: str) -> list[dict]:
    """Only this caller's pages and popups (no persistent CDP attachment)."""
    from . import identities
    return identities.page_targets(port)


def _tab_ws(port: str, target_id: str) -> str:
    return f"ws://127.0.0.1:{port}/devtools/page/{target_id}"


def _evaluate(port: str, target_id: str, expression: str):
    """One Runtime.evaluate on a tab over a short-lived CDP socket (no Runtime.enable: nothing for a page to see)."""
    with sync_connect(_tab_ws(port, target_id), open_timeout=5, close_timeout=2, max_size=None) as ws:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": expression, "returnByValue": True}}))
        while True:
            msg = json.loads(ws.recv(timeout=10))
            if msg.get("id") == 1:
                return msg.get("result", {}).get("result", {}).get("value")


def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)   # an int too large for a float raises OverflowError
    except OverflowError:
        return None
    return max(-100000.0, min(100000.0, f)) if math.isfinite(f) else None


def _str(m: dict, key: str) -> str | None:
    v = m.get(key)
    return v if isinstance(v, str) else None   # a list or dict here must be dropped, not hashed into a TypeError


def _int(v, lo: int, hi: int) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else None


def phone_command(raw) -> tuple[str, dict] | None:
    """One phone message -> one allowlisted CDP command, or None (dropped). A field that is present but malformed
    drops the whole message; nothing here raises on what a client sends."""
    try:
        m = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(m, dict):
        return None
    kind, event = _str(m, "type"), _str(m, "eventType")
    if kind == "ack" and (seq := _int(m.get("seq"), 0, 2 ** 31 - 1)) is not None:
        return "Page.screencastFrameAck", {"sessionId": seq}
    if kind == "input_mouse" and event in MOUSE_EVENTS:
        x, y = _num(m.get("x")), _num(m.get("y"))
        button, count = m.get("button", "none"), _int(m.get("clickCount", 0), 0, 3)
        if x is None or y is None or count is None or not isinstance(button, str) or button not in BUTTONS:
            return None
        params = {"type": event, "x": x, "y": y, "button": button, "clickCount": count,
                  "buttons": BUTTONS[button] if event in ("mousePressed", "mouseMoved") else 0}
        if event == "mouseWheel":
            dx, dy = _num(m.get("deltaX", 0)), _num(m.get("deltaY", 0))
            if dx is None or dy is None:
                return None
            params.update(deltaX=dx, deltaY=dy)
        return "Input.dispatchMouseEvent", params
    if kind == "input_keyboard" and event in KEY_EVENTS:
        params = {"type": KEY_EVENTS[event]}
        for k in ("key", "code", "text"):
            if k in m:
                if not (v := _str(m, k)) or len(v) > 32:
                    return None
                params[k] = v
        if "windowsVirtualKeyCode" in m:
            if (code := _int(m["windowsVirtualKeyCode"], 1, 255)) is None:
                return None
            params["windowsVirtualKeyCode"] = params["nativeVirtualKeyCode"] = code
        if event == "keyDown" and "text" not in params:
            params["type"] = "rawKeyDown"   # a key with no text (Backspace, Tab): CDP's form for editing keys
        return "Input.dispatchKeyEvent", params
    if kind == "input_text" and (text := _str(m, "text")) and len(text) <= MAX_TEXT:
        return "Input.insertText", {"text": text}
    return None


class Relay:
    """The phone page + WS relay to the identity browser's CDP, on 127.0.0.1 only, in a thread with its own loop."""

    host = "127.0.0.1"

    def __init__(self, token: str, cdp_port: str, target_id: str, reason: str = "",
                 origins: list | tuple = (None,)):
        self._page_path = f"/h/{token}".encode()
        self._ws_path = f"/h/{token}/ws".encode()
        self._cdp_port = cdp_port
        self.target_id = target_id   # the tab on the phone; follows a tab that opens (a sign-in popup) or closes
        self._origins = list(origins)
        self._html = PAGE.read_text().replace("__REASON_JSON__", _script_json(reason))
        self._loop = self._stop = self._switched = self._server = None
        self._phone = None           # the one phone connection; a newer one replaces it
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="web-handoff-relay", daemon=True)
        self.port: int | None = None

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(10) or self.port is None:
            raise HandoffError("the handoff relay did not start")

    def stop(self) -> None:
        if self._loop is not None and self._stop is not None:
            with contextlib.suppress(RuntimeError):   # the loop already ended
                self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread.is_alive():
            self._thread.join(15)

    def sockname(self):
        return self._server.sockets[0].getsockname()

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        finally:
            self._ready.set()

    async def _main(self) -> None:
        self._loop, self._stop, self._switched = asyncio.get_running_loop(), asyncio.Event(), asyncio.Event()
        watcher = asyncio.create_task(self._watch_tabs())
        try:
            async with serve(self._session, self.host, 0, process_request=self._route, origins=self._origins,
                             max_size=2 ** 16) as server:
                self._server = server
                self.port = server.sockets[0].getsockname()[1]
                self._ready.set()
                await self._stop.wait()
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    def _route(self, connection, request):
        path = request.path.split("?", 1)[0].encode("utf-8", "replace")
        if hmac.compare_digest(path, self._page_path):
            resp = connection.respond(HTTPStatus.OK, self._html)
            del resp.headers["Content-Type"]
            resp.headers["Content-Type"] = "text/html; charset=utf-8"
            resp.headers["Content-Security-Policy"] = CSP
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["Referrer-Policy"] = "no-referrer"   # the token is in the URL
            return resp
        if hmac.compare_digest(path, self._ws_path):
            return None   # carry on with the WebSocket handshake (the Origin check comes next)
        return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")

    async def _watch_tabs(self) -> None:
        known, logged = None, False
        while True:
            try:
                ids = [t["id"] for t in await asyncio.to_thread(_page_targets, self._cdp_port)]
                if ids:
                    new = [i for i in ids if known is not None and i not in known]
                    target = new[0] if new else (self.target_id if self.target_id in ids else ids[0])
                    known = set(ids)
                    if target != self.target_id:
                        self.target_id = target
                        self._switched.set()
            except Exception as e:  # noqa: BLE001 - a watcher hiccup must not end the live view: keep the current tab
                if not logged:
                    print(f"web handoff: tab watcher: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                    logged = True
            await asyncio.sleep(WATCH_SEC)

    async def _session(self, phone) -> None:
        if self._phone is not None:   # one phone at a time: a reload or a second device replaces the old socket
            with contextlib.suppress(WebSocketException, OSError):
                await self._phone.close(1000, "replaced by a newer connection")
        self._phone = phone
        try:
            while True:
                self._switched.clear()
                if not await self._screencast(phone, self.target_id):
                    break
        except (OSError, WebSocketException, TimeoutError, ValueError):
            pass   # the browser or the tab went away; the phone page shows "disconnected"
        finally:
            if self._phone is phone:
                self._phone = None

    async def _screencast(self, phone, target_id: str) -> bool:
        """Stream one tab to the phone until the phone leaves (False) or the shown tab changes (True)."""
        async with connect(_tab_ws(self._cdp_port, target_id), max_size=2 ** 25, open_timeout=10) as cdp:
            ids = itertools.count(1)

            async def call(method: str, params: dict) -> None:
                await cdp.send(json.dumps({"id": next(ids), "method": method, "params": params}))

            async def down():
                async for raw in cdp:
                    msg = json.loads(raw)
                    if msg.get("method") == "Page.screencastFrame":
                        p = msg["params"]
                        await phone.send(json.dumps({"type": "frame", "seq": p["sessionId"], "data": p["data"],
                                                     "metadata": p.get("metadata", {})}))

            async def up():
                async for raw in phone:
                    if cmd := phone_command(raw):
                        await call(*cmd)

            await call("Page.startScreencast", SCREENCAST)
            tasks = {"down": asyncio.create_task(down()), "up": asyncio.create_task(up()),
                     "switch": asyncio.create_task(self._switched.wait())}
            done, pending = await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)   # also collects a finished task's error
            with contextlib.suppress(WebSocketException, OSError):
                await call("Page.stopScreencast", {})
        if tasks["up"] in done:
            return False   # the phone left
        if tasks["switch"] in done:
            return True
        # The tab's CDP socket ended (the tab closed): give the watcher a moment to pick the next tab.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._switched.wait(), 3 * WATCH_SEC)
        return self._switched.is_set()


def _ab_data(port: str, *args: str) -> dict | None:
    """`agent-browser --json <args>` → its data object, or None on any failure."""
    try:
        p = act.agent_browser(["--json", *args], port=port, capture=True, timeout=30)
        out = json.loads(p.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return out.get("data") if isinstance(out, dict) and out.get("success") else None


def _active_target(port: str) -> str:
    """Select the caller's saved target, including after its daemon restarts."""
    from . import identities
    return identities.activate(port)


def _tailscale(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tailscale", *args], capture_output=True, text=True, timeout=30)


def _tailnet_name() -> str:
    try:
        name = json.loads(_tailscale("status", "--json").stdout)["Self"]["DNSName"].rstrip(".")
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as e:
        raise HandoffError(f"tailscale status failed: {e}") from None
    if not name:
        raise HandoffError("tailscale reports no MagicDNS name for this host")
    return name


def _serve_on(port: int) -> None:
    p = _tailscale("serve", "--bg", *LISTENER, "--set-path", MOUNT, f"http://127.0.0.1:{port}")
    if p.returncode != 0:
        raise HandoffError(f"tailscale serve failed: {p.stderr.strip()[-300:]}")


def _serve_off(sweep: bool = False) -> None:
    """Remove the /web-handoff path. sweep=True clears what a hard-killed handoff (SIGKILL, OOM, reboot) left behind:
    quietly, since "handler does not exist" is the normal case."""
    try:
        failed = _tailscale("serve", *LISTENER, "--set-path", MOUNT, "off").returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        failed = True
    if failed and not sweep:   # the path then points at a stopped relay (a dead link), but say so
        print(f"web handoff: could not remove the tailscale path {MOUNT}; run: "
              f"tailscale serve {' '.join(LISTENER)} --set-path {MOUNT} off", file=sys.stderr)


def _attr(tag: str, name: str, lower: bool = True) -> str:
    m = re.search(_ATTR.format(name), tag, re.I)
    v = unescape(m.group(1)) if m else ""
    return v.lower() if lower else v


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _otp_like(tag: str) -> bool:
    if _attr(tag, "type") not in TEXT_INPUTS:
        return False
    if _attr(tag, "autocomplete") == "one-time-code":
        return True
    maxlen = _attr(tag, "maxlength")
    if _attr(tag, "inputmode") == "numeric" and maxlen.isdigit() and 4 <= int(maxlen) <= 8:
        return True
    names = (_attr(tag, "name"), _attr(tag, "id"))
    return any(_OTP_NAME.search(n) and not any(w in n for w in NOT_OTP) for n in names)


def _one_digit_box(tag: str) -> bool:
    """A maxlength=1 numeric input: one box of a code split across several."""
    numeric = (_attr(tag, "inputmode") in ("numeric", "decimal") or _attr(tag, "type") in ("number", "tel")
               or "\\d" in _attr(tag, "pattern") or "0-9" in _attr(tag, "pattern"))
    return _attr(tag, "maxlength") == "1" and numeric


def _challenge_iframes(html: str, url: str | None) -> set[str]:
    """Cross-origin iframes from a host named like a CAPTCHA or a challenge: "iframe:<host>"."""
    here, found = _host(url or ""), set()
    for src in _IFRAME_SRC.findall(html):
        try:
            host = _host(urljoin(url or "", unescape(src)))
        except ValueError:
            continue
        if host and host != here and ("captcha" in host or "challenge" in host):
            found.add(f"iframe:{host}")
    return found


def gate_signals(html: str, url: str | None = None) -> list[str]:
    """What on this page (at url) needs a human: vendor CAPTCHA / challenge widgets, a cross-origin iframe from a
    captcha/challenge host, one-time-code / password inputs, verification wording, and detect.block_reason's
    full-page walls ("wall:<reason>"). [] = nothing a human has to do."""
    html = html or ""
    found = {f"widget:{name}" for name, rx in _WIDGET_RES if rx.search(html)} | _challenge_iframes(html, url)
    boxes = 0   # one-digit numeric inputs in a row
    for tag in _INPUT.findall(html):
        kind = _attr(tag, "type")
        if kind == "hidden":
            continue
        boxes = boxes + 1 if _one_digit_box(tag) else 0
        if _otp_like(tag) or boxes >= SPLIT_OTP:
            found.add("otp-input")
        elif kind == "password":
            found.add("password-input")
    text = visible_text(html)[0].lower().replace("’", "'")
    found |= {f"text:{t}" for t in GATE_TEXT if t in text}
    if _ENTER_CODE.search(text):
        found.add("text:enter the code")
    if reason := block_reason(None, None, html):
        found.add(f"wall:{reason}")
    return sorted(found)


def _no_fragment(url: str) -> str:
    return url.split("#", 1)[0]


def _page_state(port: str, target_id: str, selector: str | None = None, html: bool = True) -> dict | None:
    """{url, ready, origin, text, sel, html} of a tab (one Runtime.evaluate), or None when it can't be read right
    now (a closed tab, a navigation in flight). sel: whether `selector` matches (None without one); html: '' unless
    asked; text: how many characters of visible text the page shows."""
    js = STATE_JS % (SELECTOR_JS % json.dumps(selector) if selector else "null",
                     "document.documentElement.outerHTML" if html else "''")
    try:
        state = _evaluate(port, target_id, js)
    except (OSError, ValueError, KeyError, WebSocketException, TimeoutError):
        return None
    if isinstance(state, dict) and all(isinstance(state.get(k), str) for k in ("url", "ready", "html")):
        return state
    return None


def _settled(state: dict) -> bool:
    """Finished loading, on a real page that shows something: about:blank, chrome-error://, a blank document or a
    lone spinner never count as done."""
    text = state.get("text")
    return (state["ready"] == "complete" and state["url"].split(":", 1)[0].lower() in ("http", "https")
            and isinstance(text, (int, float)) and text >= MIN_TEXT)


def _matches(state: dict, until_url: str | None, until_selector: str | None) -> bool:
    return ((not until_url or fnmatch.fnmatchcase(state["url"], until_url))
            and (not until_selector or state.get("sel") is True))


def _matched_elsewhere(port: str, target_id: str, until_url: str | None, until_selector: str | None) -> dict:
    """The OTHER tabs on which the condition already holds at the start: {tab: (url, timeOrigin)}. If the start tab
    closes, such a tab proves nothing until it has navigated (another URL or a new document). A tab that can't be
    read counts as matching (with no timeOrigin: then only another URL frees it)."""
    try:
        tabs = _page_targets(port)
    except (OSError, ValueError):
        tabs = []
    matched = {}
    for t in tabs:
        if t["id"] == target_id:
            continue
        state = _page_state(port, t["id"], until_selector, html=False)
        if state is None:
            matched[t["id"]] = (str(t.get("url", "")), None)
        elif _matches(state, until_url, until_selector):
            matched[t["id"]] = (state["url"], state.get("origin"))
    return matched


def _start_check(port: str, target_id: str, until_url: str | None, until_selector: str | None) -> dict:
    """Read the start tab (once it has finished loading, up to START_TRIES) and refuse (exit 2) a handoff that
    couldn't end honestly: with a --until-* condition, a page still loading or a condition already true; with
    neither, a page with nothing on it for a human to do. Returns the poll's starting point: {matched} (other tabs
    the condition already holds on), or the no-condition default's {url (no fragment), in_place, signals}."""
    condition = bool(until_url or until_selector)
    state = None
    for _ in range(START_TRIES):   # a tab mid-navigation can't be read for a moment; a loading page isn't final
        state = _page_state(port, target_id, until_selector, html=not condition) or state
        if state and state["ready"] == "complete":
            break
        time.sleep(START_TRY_SEC)
    if state is None:
        raise HandoffError("couldn't read the page the handoff would start on; try again once it has loaded",
                           code=2)
    if condition:
        if state["ready"] != "complete":   # what it says now may not be what it says once loaded
            raise HandoffError(LOADING, code=2)
        if _matches(state, until_url, until_selector):
            raise HandoffError(ALREADY, code=2)
        return {"matched": _matched_elsewhere(port, target_id, until_url, until_selector)}
    signals = gate_signals(state["html"], state["url"])
    if not signals:
        raise HandoffError(NO_GATE, code=2)
    reason = block_reason(None, None, state["html"])
    return {"url": _no_fragment(state["url"]), "signals": signals,
            "in_place": reason is not None and reason not in NOT_SOLVED_IN_PLACE}


def _cleared(port: str, start: str, shown: str, until_url: str | None, until_selector: str | None,
             gate: dict | None = None) -> str | None:
    """One poll: the URL of the page that looks done, or None. start is the tab the handoff began on, shown the tab
    on the phone (the relay follows a popup). _poll wants CLEAN_POLLS of these in a row, on one URL."""
    gate = gate or {}
    try:
        if until_url or until_selector:
            # The start tab; once it has closed (a sign-in popup that finished), the tab on the phone, unless the
            # condition held there before the human step and it hasn't navigated since.
            alive = any(t["id"] == start for t in _page_targets(port))
            subject = start if alive else shown
            state = _page_state(port, subject, until_selector, html=False)
            if not state or not _settled(state) or not _matches(state, until_url, until_selector):
                return None
            before = gate.get("matched", {}).get(subject) if subject != start else None
            if before and state["url"] == before[0] and before[1] in (None, state.get("origin")):
                return None
            return state["url"]
        # No condition: the START tab moved on (a bot wall may clear in place) and nothing on it needs a human; a
        # popup on the phone only matters for its own gate. A start tab that closed never clears.
        state = _page_state(port, start)
        if not state or not _settled(state) or gate_signals(state["html"], state["url"]):
            return None
        if shown != start:
            popup = _page_state(port, shown)
            if not popup or not _settled(popup) or gate_signals(popup["html"], popup["url"]):
                return None
        moved = gate.get("in_place") or _no_fragment(state["url"]) != gate.get("url")
        return state["url"] if moved else None
    except (OSError, ValueError, KeyError, WebSocketException, TimeoutError):
        return None   # e.g. a tab mid-navigation: look again next time


GONE_AFTER = 3   # consecutive failed browser probes before a handoff gives up (one slow probe isn't a dead browser)


def _poll(port: str, relay: Relay, until_url: str | None, until_selector: str | None, timeout: float,
          gate: dict | None = None, start: str | None = None) -> str:
    start = start or relay.target_id
    deadline = time.monotonic() + timeout
    misses = clean = 0
    last = None   # the URL the clean polls so far were on: they must all be on one
    while True:
        if gate and gate.get("window_target"):
            from . import identities
            identities.renew_handoff(gate["window_target"])
        else:
            act.acquire()
        misses = 0 if act.browser_port() == port else misses + 1
        if misses >= GONE_AFTER:
            raise HandoffError("the identity browser went away during the handoff")
        url = None if misses else _cleared(port, start, relay.target_id, until_url, until_selector, gate)
        clean, last = (clean + 1 if url == last else 1, url) if url is not None else (0, None)
        if clean >= CLEAN_POLLS:
            return "cleared"
        if time.monotonic() >= deadline:
            return "timeout"
        time.sleep(POLL_SEC)


def handoff(reason: str, until_url: str | None = None, until_selector: str | None = None,
            timeout: float = MAX_TIMEOUT) -> str:
    """Publish the identity browser's live view on the tailnet, ping the user, wait. Returns "cleared" | "timeout"."""
    timeout = max(0.0, min(float(timeout), MAX_TIMEOUT))
    with contextlib.ExitStack() as stack:   # unwinds in reverse: tailscale path off, relay (and screencast), marker
        try:   # one handoff at a time, foreground or background; `web act` stays detached until this one ends
            stack.enter_context(bg.claim("handoff"))
        except bg.Busy as e:
            raise HandoffError(f"a handoff is already running (pid {e.pid}); wait for it: `web handoff --wait`",
                               code=2) from None
        from . import identities
        with identities.operation():
            act.acquire()
            port = act.browser_port()
            if port is None:
                identity_flag = "" if act.IDENTITY == "personal" else f" --as {act.IDENTITY}"
                raise HandoffError(f"the identity browser isn't running; start it with `web act{identity_flag} open <url>`", code=2)
            host = _tailnet_name()
            target = _active_target(port)
            gate = _start_check(port, target, until_url, until_selector)   # refuses a start it couldn't end honestly
            gate["window_target"] = target
            if gate.get("signals"):
                print(f"web handoff: no --until-*: waiting for the page to move on with no gate left "
                      f"({', '.join(gate['signals'])})", file=sys.stderr, flush=True)
            act.detach()   # no agent-browser CDP session while the user is on the page
        token = secrets.token_urlsafe(24)
        _serve_off(sweep=True)
        relay = Relay(token, port, target, reason, origins=[None, f"http://{host}", f"http://{host.split('.')[0]}"])
        stack.callback(relay.stop)
        relay.start()
        stack.callback(_serve_off)
        _serve_on(relay.port)
        link = f"http://{host}{MOUNT}/h/{token}"
        bg.announce(f"handoff: {link}")   # stdout + stderr + last-link: the caller has it before the wait
        notify.ping(f"web handoff: {reason}\n{link}")
        return _poll(port, relay, until_url, until_selector, timeout, gate=gate, start=target)
