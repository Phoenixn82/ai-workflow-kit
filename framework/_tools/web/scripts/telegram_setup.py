#!/usr/bin/env python3
"""One-time: point `web handoff` phone pings at a Telegram group, reusing Example App's bot.

The user runs it themselves (`! ~/AI_Projects/_tools/web/scripts/telegram_setup.py`), because the secret firewall keeps
Claude off the token. The token goes from heimdall straight into ~/.config/web/telegram.json (0600) and is never
printed. Before running: make a Telegram group, add the bot, and send one message in it.
"""
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG = Path.home() / ".config" / "web" / "telegram.json"
TOKEN_CMD = ["ssh", "-o", "BatchMode=yes", "heimdall", "cat ~/telegram-bot.token"]


def newest_group(updates: list[dict]) -> tuple[int, str] | None:
    """(chat_id, title) of the group the bot most recently joined or heard from; skips groups it was removed from."""
    chats: dict[int, tuple[int, str]] = {}
    for u in updates:
        for key in ("my_chat_member", "message", "edited_message"):
            ev = u.get(key)
            chat = (ev or {}).get("chat", {})
            if chat.get("type") not in ("group", "supergroup"):
                continue
            if key == "my_chat_member" and ev.get("new_chat_member", {}).get("status") in ("left", "kicked"):
                chats.pop(chat["id"], None)
                continue
            chats[ev.get("migrate_to_chat_id") or chat["id"]] = (u["update_id"], chat.get("title", ""))
    if not chats:
        return None
    cid = max(chats, key=lambda c: chats[c][0])
    return cid, chats[cid][1]


def main() -> int:
    got = subprocess.run(TOKEN_CMD, capture_output=True, text=True, timeout=20)
    token = got.stdout.strip()
    if got.returncode or not token:
        print("couldn't read the bot token on heimdall (is `ssh heimdall` working?)", file=sys.stderr)
        return 1

    def call(method: str, **params) -> dict:
        data = urllib.parse.urlencode(params).encode() if params else None
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=15) as r:
                return json.loads(r.read())
        except (OSError, ValueError):   # never echo the error: the URL carries the token
            return {"ok": False}

    ups = call("getUpdates")   # no offset: reads without acknowledging anything
    if not ups.get("ok"):
        print("Telegram getUpdates failed", file=sys.stderr)
        return 1
    pick = newest_group(ups["result"])
    if not pick:
        print("no group found: make a Telegram group, add the bot, send one message there, then rerun",
              file=sys.stderr)
        return 1
    cid, title = pick
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"bot_token": token, "chat_id": cid}, f)
    os.replace(tmp, CONFIG)
    sent = call("sendMessage", chat_id=cid, text="Odin web: phone pings will land here.").get("ok")
    print(f"saved -> group '{title}'; test message {'sent' if sent else 'FAILED'}")
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())
