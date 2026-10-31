#!/usr/bin/env python3
"""test_telegram_setup.py (S16): run the documented `tg-admin` setup block of the kit's profiles/telegram/README.md verbatim
(bash -e, one shell, in order) in a fresh HOME with no tg config, no legacy chat file and no network
(unshare -c -n), against a fake Telegram (tools/telegram-fakes/sitecustomize.py: Bot API for tg, Telethon/opentele for tg-admin).
The only human step, opening the `tg --claim` link and tapping Start, is simulated from the owner's account.
usage: replay.py [README.md TOOLS_DIR]   (TOOLS_DIR holds tg and tg-admin; default: the kit around this file,
profiles/telegram/README.md and framework/_tools/telegram). Exit 0 only when the owner is established, every
topic is mapped to an owned group, and the test notification is routed silently."""
import json, os, re, shutil, stat, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if len(sys.argv) == 3:
    README, TOOLS = Path(sys.argv[1]), Path(sys.argv[2]).resolve()
else:
    KIT = next(d for d in HERE.parents if (d / "profiles/telegram/README.md").exists())
    README, TOOLS = KIT / "profiles/telegram/README.md", KIT / "framework/_tools/telegram"
OWNER = 4242


def documented_block(text):
    i = text.index("**With `tg-admin`")
    return re.search(r"```sh\n(.*?)```", text[i:], re.S).group(1)


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


tmp = Path(tempfile.mkdtemp(prefix="s16-replay-"))
try:
    home = tmp / "home"
    (home / ".local/bin").mkdir(parents=True)
    for n in ("tg", "tg-admin"):  # what install/install.sh --profile telegram links
        os.symlink(TOOLS / n, home / ".local/bin" / n)
    tdata = home / ".local/share/TelegramDesktop/tdata"  # the prerequisite: a logged-in Telegram Desktop
    tdata.mkdir(parents=True)
    (tdata / "key_datas").write_bytes(b"fake")
    world = tmp / "world.json"
    world.write_text(json.dumps({"user": {"id": OWNER, "first_name": "Tester", "username": "tester", "started": []},
                                 "bots": {}, "groups": {}, "updates": {}, "sent": [], "api_calls": [], "side_calls": [],
                                 "botfather": {}, "issued": [], "next_group": 0, "update_id": 0}))
    script = tmp / "setup.sh"
    script.write_text(documented_block(README.read_text()))
    print("documented block replayed verbatim:\n" + "".join("  | " + l + "\n" for l in script.read_text().splitlines()))
    env = {"HOME": str(home), "PATH": f"{home}/.local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
           "PYTHONPATH": str(HERE / "telegram-fakes"), "FAKE_WORLD": str(world)}
    # tg defaults to its maintainer's home (_OWNER = ~name) where that user exists: on such a box keep tg off the
    # real files with TG_HOME; anywhere else tg uses $HOME, as it does for every other user
    owner = re.search(r'_OWNER = os\.path\.expanduser\("(~\w+)"\)', (TOOLS / "tg").read_text())
    if owner and os.path.expanduser(owner.group(1)).startswith("/"):
        env["TG_HOME"] = str(home)
    print("tg home: " + ("TG_HOME (this box has tg's maintainer account)" if "TG_HOME" in env else "$HOME, no TG_HOME (as for any user)"))
    # no network when the kernel allows an unprivileged namespace (some CI kernels do not: the fakes still
    # replace every Telegram call, and the tools read only this HOME)
    isolate = ["unshare", "-c", "-n"] if subprocess.run(["unshare", "-c", "-n", "true"], capture_output=True).returncode == 0 else []
    print("network: " + ("none (unshare -c -n)" if isolate else "not isolated (unshare unavailable); fakes only"))
    p = subprocess.Popen(isolate + ["bash", "-e", str(script)], cwd=home, env=env, text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = []
    for line in p.stdout:
        log.append(line)
        m = re.search(r"https://t\.me/(\w+)\?start=([A-Za-z0-9_-]+)", line)
        if m:  # the owner opens the deep link (Telegram sends "/start CODE" from their private chat) and taps Start
            import fcntl
            with open(str(world) + ".lock", "a") as lk:
                fcntl.flock(lk, fcntl.LOCK_EX)
                w = json.loads(world.read_text())
                bot = next(b for b in w["bots"].values() if b["username"] == m.group(1))
                w["update_id"] += 1
                w["updates"].setdefault(str(bot["id"]), []).append({"update_id": w["update_id"], "message": {
                    "message_id": 1, "date": 0, "text": "/start " + m.group(2),
                    "chat": {"id": OWNER, "type": "private", "first_name": "Tester"},
                    "from": {"id": OWNER, "is_bot": False, "first_name": "Tester"}}})
                w["user"]["started"].append(bot["id"])
                world.write_text(json.dumps(w, indent=1))
            log.append("  [simulated owner: opened the claim link from their account and tapped Start]\n")
    rc = p.wait(timeout=600)
    print("replay output:\n" + "".join("  > " + l for l in log) + f"exit {rc}\n")

    w = json.loads(world.read_text())
    cfgf, tokf = home / ".config/telegram/topics.json", home / "telegram-bot.token"
    cfg = json.loads(cfgf.read_text()) if cfgf.exists() else {}
    issued = [t for t, b in w["bots"].items() if b["username"] == "my_alerts_bot"]
    checks = []
    def check(name, ok):
        checks.append(ok)
        print(("PASS " if ok else "FAIL ") + name)
    check("documented block exits 0", rc == 0)
    check("no legacy chat file and no tg config before the run (fresh HOME); legacy file still absent",
          not (home / "services/uptime-kuma/telegram-chat-id").exists())
    check("sender token placed: ~/telegram-bot.token is the token BotFather issued, mode 600",
          tokf.exists() and bool(issued) and tokf.read_text().strip() == issued[0]
          and stat.S_IMODE(tokf.stat().st_mode) == 0o600)
    check("owner established: default_chat_id is the claiming account's private chat",
          str(cfg.get("default_chat_id")) == str(OWNER) and stat.S_IMODE(cfgf.stat().st_mode) == 0o600)
    topics = cfg.get("topics", {})
    mapped = {t: v.get("chat_id") for t, v in topics.items()}
    good = {t: c for t, c in mapped.items() if c and str(c) in w["groups"] and w["groups"][str(c)]["creator"] == OWNER
            and norm(w["groups"][str(c)]["title"]) == norm(t)}
    check(f"groups discovered: {len(good)}/{len(topics)} topics mapped to groups the owner created via tg-admin",
          bool(topics) and len(good) == len(topics))
    fleet = [s for s in w["sent"] if s["group"] == "Fleet" and s["text"].startswith("test from")]
    check("test notification routed: `tg --test fleet` posted in the Fleet group, silently",
          len(fleet) == 1 and fleet[0]["silent"] and str(fleet[0]["chat_id"]) == str(mapped.get("fleet")))
    check("claim confirmation went to the owner's private chat, silently",
          any(s["chat_id"] == str(OWNER) and s["silent"] for s in w["sent"]))
    check("nothing buzzed (every bot message silent; heat untouched)", all(s["silent"] for s in w["sent"]))
    print(f"side calls intercepted (no real systemctl/logger): {w['side_calls']}")
    print(f"Bot API calls: {len(w['api_calls'])} ({', '.join(sorted(set(w['api_calls'])))})")
    print("RESULT:", "PASS" if all(checks) else "FAIL")
    sys.exit(0 if all(checks) else 1)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
