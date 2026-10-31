"""Phone ping for `web handoff`: Telegram, configured only by a 0600 file the user owns.

~/.config/web/telegram.json = {"bot_token": ..., "chat_id": ...}. Until it exists (Phase C: the bot token lives on
heimdall and is copied only with the user's OK), ping() returns False and the handoff link is only printed.
"""
import json
import os
import stat
import sys
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG = Path.home() / ".config" / "web" / "telegram.json"
API = "https://api.telegram.org/bot{token}/sendMessage"


def _load() -> dict | None:
    try:
        fd = os.open(CONFIG, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())   # the mode of the file actually opened, not of whatever the path names later
        if stat.S_IMODE(st.st_mode) != 0o600 or st.st_uid != os.getuid():
            print(f"web: not reading {CONFIG}: it must be mode 0600 and owned by you", file=sys.stderr)
            return None
        try:
            cfg = json.load(f)
        except ValueError:
            return None
    return cfg if isinstance(cfg, dict) else None


def configured() -> bool:
    cfg = _load()
    return cfg is not None and {"bot_token", "chat_id"} <= cfg.keys()


def ping(text: str, button: tuple[str, str] | None = None) -> bool:
    """button: (label, url) shown as a tappable Telegram URL button under the message."""
    cfg = _load()
    if not cfg or not cfg.get("bot_token") or not cfg.get("chat_id"):
        return False
    fields = {"chat_id": cfg["chat_id"], "text": text,
              "disable_notification": "true",  # silent; nothing buzzes (2026-10-21)
              # no link preview: Telegram would fetch the (tailnet-only) link from its servers
              "link_preview_options": json.dumps({"is_disabled": True})}
    if button:
        fields["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": button[0], "url": button[1]}]]})
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(API.format(token=cfg["bot_token"]), data=data)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200 and json.loads(r.read()).get("ok") is True
    except (OSError, ValueError):   # never echo the error: the request URL carries the bot token
        return False
