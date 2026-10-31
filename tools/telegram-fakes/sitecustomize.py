"""Fake Telegram for the S16 replay: loaded into every `tg` / `tg-admin` process through PYTHONPATH.
One world (JSON at $FAKE_WORLD, flock-guarded) shared by the Bot API fake (urllib, for tg) and the Telethon /
opentele / PyQt5 fakes (for tg-admin). Only active when FAKE_WORLD is set. Never prints a token."""
import os
if os.environ.get("FAKE_WORLD"):
    import asyncio, contextlib, fcntl, io, json, secrets, subprocess, sys, types, urllib.error, urllib.parse, urllib.request
    from pathlib import Path

    WORLD = Path(os.environ["FAKE_WORLD"])

    @contextlib.contextmanager
    def world():
        with open(str(WORLD) + ".lock", "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            w = json.loads(WORLD.read_text())
            yield w
            WORLD.write_text(json.dumps(w, indent=1))

    def push(w, bot_id, upd):
        w["update_id"] += 1
        w["updates"].setdefault(str(bot_id), []).append({"update_id": w["update_id"], **upd})

    # ---- subprocess: no systemctl / logger side effects on the test box -------------------------------
    _run = subprocess.run
    def run(args, *a, **k):
        if isinstance(args, (list, tuple)) and args and args[0] in ("systemctl", "logger"):
            with world() as w:
                w["side_calls"].append(list(args))
            return subprocess.CompletedProcess(args, 3)
        return _run(args, *a, **k)
    subprocess.run = run

    # ---- Bot API (tg) -------------------------------------------------------------------------------------
    class Resp(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *e):
            return False

    def bot_api(url, data=None, timeout=None):
        u = urllib.parse.urlsplit(url if isinstance(url, str) else url.full_url)
        if u.hostname != "api.telegram.org":
            raise urllib.error.URLError("fake world: no network")
        tok, method = u.path[len("/bot"):].rsplit("/", 1)
        args = dict(urllib.parse.parse_qsl((data or b"").decode()))
        def fail(code, desc):
            raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(json.dumps({"ok": False, "description": desc}).encode()))
        with world() as w:
            bot = w["bots"].get(tok)
            if not bot:
                fail(401, "Unauthorized")
            w["api_calls"].append(method)
            if method == "getMe":
                out = {"id": bot["id"], "is_bot": True, "username": bot["username"]}
            elif method == "getUpdates":
                out = w["updates"].get(str(bot["id"]), [])
            elif method == "getChatMember":
                g = w["groups"].get(args["chat_id"])
                if not g or bot["id"] not in g["members"]:
                    fail(400, "Bad Request: chat not found")
                uid = int(args["user_id"])
                out = {"status": "creator" if g["creator"] == uid else "member" if uid in g["members"] else "left"}
            elif method == "sendMessage":
                cid = args["chat_id"]
                g = w["groups"].get(cid)
                if g is None and not (cid == str(w["user"]["id"]) and w["user"]["started"].count(bot["id"])):
                    fail(400, "Bad Request: chat not found")
                if g is not None and bot["id"] not in g["members"]:
                    fail(403, "Forbidden: bot is not a member")
                w["sent"].append({"by": bot["username"], "chat_id": cid, "group": g and g["title"],
                                  "silent": args.get("disable_notification") == "true", "text": args["text"]})
                out = {"message_id": len(w["sent"])}
            else:
                fail(404, "Not Found")
        return Resp(json.dumps({"ok": True, "result": out}).encode())
    urllib.request.urlopen = bot_api

    # ---- Telethon / opentele / PyQt5 (tg-admin) -----------------------------------------------------------
    def mod(name, **kw):
        m = types.ModuleType(name)
        m.__dict__.update(kw)
        sys.modules[name] = m
        return m

    class RPCError(Exception):
        pass
    class FloodWaitError(RPCError):
        seconds = 0

    class Req:
        def __init__(self, *a, **k):
            self.a, self.k = a, k
    CreateChannelRequest = type("CreateChannelRequest", (Req,), {})
    InviteToChannelRequest = type("InviteToChannelRequest", (Req,), {})
    AddChatUserRequest = type("AddChatUserRequest", (Req,), {})
    GetAuthorizationsRequest = type("GetAuthorizationsRequest", (Req,), {})

    class Channel:
        def __init__(self, gid, g, me):
            self.id, self.title, self.creator = int(gid), g["title"], g["creator"] == me
            self.left = self.deactivated = False
            self.admin_rights = None

    def ns(**kw):
        return types.SimpleNamespace(**kw)

    class Conversation:
        def __init__(self, client):
            self.c, self.replies = client, []
        async def __aenter__(self):
            return self
        async def __aexit__(self, *e):
            return False
        async def send_message(self, text):
            with world() as w:
                bf = w["botfather"]
                if text == "/newbot":
                    bf.clear(); bf["step"] = "name"
                    r = "Alright, a new bot. How are we going to call it? Please choose a name for your bot."
                elif bf.get("step") == "name":
                    bf.update(step="username", name=text)
                    r = "Good. Now let's choose a username for your bot. It must end in `bot`."
                elif bf.get("step") == "username":
                    if any(b["username"].lower() == text.lower() for b in w["bots"].values()):
                        r = "Sorry, this username is already taken. Please try something different."
                    else:
                        tok = str(7000000000 + secrets.randbelow(10**9)) + ":" + "AA" + secrets.token_urlsafe(33)[:33]
                        w["bots"][tok] = {"id": 8000 + len(w["bots"]), "username": text, "name": bf["name"]}
                        w["issued"].append(text)
                        bf.clear()
                        r = ("Done! Congratulations on your new bot. You will find it at t.me/" + text + ". Use this "
                             "token to access the HTTP API:\n" + tok + "\nKeep your token secure and store it safely.")
                else:
                    r = "I don't understand."
            self.replies.append(ns(raw_text=r, buttons=None))
        async def get_response(self):
            return self.replies.pop(0)

    class TelegramClient:
        def __init__(self, session=None, api_id=None, api_hash=None, **kw):
            self.session, self.connected = session, False
        async def connect(self):
            self.connected = True
        async def is_user_authorized(self):
            return Path(self.session).exists()
        async def disconnect(self):
            self.connected = False
        def _me(self):
            with world() as w:
                return w["user"]
        async def get_me(self):
            u = self._me()
            return ns(first_name=u["first_name"], username=u["username"], id=u["id"])
        async def __call__(self, req):
            assert self.connected, "Cannot send requests while disconnected"
            with world() as w:
                me = w["user"]["id"]
                if isinstance(req, GetAuthorizationsRequest):
                    return ns(authorizations=[ns(current=True, app_name="Telegram Desktop", app_version="7.2.5",
                                                 device_model="tg-admin", platform="Linux")])
                if isinstance(req, CreateChannelRequest):
                    w["next_group"] += 1
                    gid = str(-1000000000000 - w["next_group"])
                    w["groups"][gid] = {"title": req.k["title"], "creator": me, "members": [me], "by_user": []}
                    return ns(chats=[Channel(gid, w["groups"][gid], me)])
                if isinstance(req, InviteToChannelRequest):
                    ch, bots = req.a
                    g = w["groups"][str(ch.id)]
                    for b in bots:
                        if b.id not in g["members"]:
                            g["members"].append(b.id)
                            push(w, b.id, {"my_chat_member": {
                                "chat": {"id": ch.id, "title": g["title"], "type": "supergroup"},
                                "from": {"id": me, "is_bot": False, "first_name": w["user"]["first_name"]}, "date": 0,
                                "old_chat_member": {"status": "left", "user": {"id": b.id, "is_bot": True}},
                                "new_chat_member": {"status": "member", "user": {"id": b.id, "is_bot": True}}}})
                    return ns()
            raise NotImplementedError(type(req).__name__)
        async def iter_dialogs(self):
            with world() as w:
                me, gs = w["user"]["id"], dict(w["groups"])
            for gid, g in gs.items():
                if me in g["members"]:
                    yield ns(is_group=True, entity=Channel(gid, g, me))
        async def get_participants(self, ch):
            with world() as w:
                me, g = w["user"]["id"], w["groups"][str(ch.id)]
                bot_ids = {b["id"] for b in w["bots"].values()}
            return [ns(id=m, is_self=m == me, bot=m in bot_ids, participant=None) for m in g["members"]]
        async def get_entity(self, handle):
            with world() as w:
                b = next((b for b in w["bots"].values() if b["username"].lower() == str(handle).lstrip("@").lower()), None)
            if not b:
                raise ValueError("No user has \"%s\" as username" % handle)
            return ns(id=b["id"], bot=True, username=b["username"])
        async def get_messages(self, ch, limit=100, from_user=None):
            with world() as w:
                return [ns(message=t) for t in w["groups"][str(ch.id)]["by_user"]][-limit:]
        async def send_message(self, ch, text):
            with world() as w:
                w["groups"][str(ch.id)]["by_user"].append(text)  # bots in privacy mode do not see plain text
        def conversation(self, peer, timeout=60):
            assert peer == "BotFather"
            return Conversation(self)

    mod("telethon", TelegramClient=TelegramClient, errors=mod("telethon.errors", RPCError=RPCError, FloodWaitError=FloodWaitError))
    mod("telethon.tl"); mod("telethon.tl.functions")
    mod("telethon.tl.functions.channels", CreateChannelRequest=CreateChannelRequest, InviteToChannelRequest=InviteToChannelRequest)
    mod("telethon.tl.functions.messages", AddChatUserRequest=AddChatUserRequest)
    mod("telethon.tl.functions.account", GetAuthorizationsRequest=GetAuthorizationsRequest)
    mod("telethon.tl.types", Channel=Channel)

    class OpenTeleException(Exception):
        pass
    NewSession, CurrentSession = object(), object()

    class TDesktop:
        def __init__(self, path, passcode=None):
            if not (Path(path) / "key_datas").exists():
                raise OpenTeleException("no logged-in Telegram Desktop at " + path)
        async def ToTelethon(self, session, flag, api=None, password=None):
            Path(session).touch()
            c = TelegramClient(session)
            c.connected = flag is NewSession
            return c

    mod("PyQt5"); mod("PyQt5.QtCore", QByteArray=lambda *a: None)
    mod("opentele")
    mod("opentele.api", API=ns(TelegramDesktop=lambda **kw: kw), CreateNewSession=NewSession, UseCurrentSession=CurrentSession)
    mod("opentele.exception", NoPasswordProvided=type("NoPasswordProvided", (Exception,), {}),
        PasswordIncorrect=type("PasswordIncorrect", (Exception,), {}), OpenTeleException=OpenTeleException,
        TDataBadDecryptKey=type("TDataBadDecryptKey", (Exception,), {}))
    mod("opentele.td", TDesktop=TDesktop)
    mod("opentele.td.account", StorageAccount=type("StorageAccount", (), {}))
