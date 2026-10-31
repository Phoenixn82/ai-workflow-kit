"""Offline tests for x-scroll (no browser, no network, no model). Run: python3 -m unittest discover -s tests, or
xscroll.py --self-test."""
import base64, copy, json, os, re, shutil, signal, subprocess, sys, tempfile, time, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

TOOL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOL))
import xscroll as xs  # noqa: E402

FIX = TOOL / "tests" / "fixtures"
XDS = json.loads((FIX / "extract-xds-theo.json").read_text())
LEGACY = json.loads((FIX / "extract-legacy.json").read_text())
STANCE_ROW = re.compile(r"^\| (plan|build|review|subagent|design|headless|other) \| (.*?) \| (.*?) \| (.*?) \| (use|avoid|conditional) \| (.*?) \| (.*) \|$", re.M)


N = "0123456789abcdef" * 2   # a tab nonce, as new_tab's secrets.token_hex(16) makes them


def opened(calls):
    """The planned URLs x-scroll opened tabs at, without their #xs-<nonce> fragment."""
    return [c[4].split("#")[0] for c in calls if c[:2] == ["tab", "new"]]


def ctx(source="@theo"):
    return {"sid": "s1", "source": source, "url": "https://x.com/theo", "position": 0}


class Parse(unittest.TestCase):
    def test_xds_fixture_records(self):
        recs = [xs.record(p, ctx(), True) for p in XDS["posts"]]
        self.assertEqual(len(recs), 5)
        for r in recs:
            for k in ("id", "url", "handle", "created_at", "text"):
                self.assertTrue(r[k], k)
            self.assertEqual(r["created_at"], xs.snowflake(r["id"]))   # xds has no <time>: time comes from the id
        self.assertTrue(recs[0]["is_pinned"])
        self.assertEqual(recs[3]["quote"]["handle"], "realhasanshoaib")
        self.assertEqual(recs[1]["counts"]["views"], 39000)

    def test_legacy_fixture_records(self):
        a, b = (xs.record(p, ctx(), True) for p in LEGACY["posts"])
        self.assertEqual((a["handle"], a["reposted_by"], a["is_truncated"]), ("bob", "alice", True))
        self.assertEqual(a["counts"], {"replies": 12, "reposts": 3, "likes": 1204, "bookmarks": 7, "views": 45678})
        self.assertEqual(a["links"][0]["url"], "https://t.co/abc")
        self.assertEqual(a["media"]["alt"], ["a cat on a desk"])
        self.assertEqual(a["created_at"], "2024-10-15T12:00:00.000Z")
        self.assertTrue(b["is_pinned"] and b["is_promoted"])
        self.assertFalse(any(LEGACY["flags"][k] for k in ("rate_limit", "something_wrong", "locked")))   # post text only

    def test_snowflake(self):
        self.assertEqual(xs.snowflake("2109607102188777707"), "2026-10-12T11:28:22+00:00")
        self.assertIsNone(xs.snowflake("nope"))


class Cutoff(unittest.TestCase):
    page = {"source": "@bob", "url": "https://x.com/bob", "kind": "handle", "owner": "bob"}

    def post(self, days, **kw):
        t = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return {"id": "1", "handle": "bob", "created_at": t, **kw}

    def test_fourteen_day_stop(self):
        cut = datetime.now(timezone.utc) - timedelta(days=14)
        self.assertTrue(xs.past_cutoff(self.post(20), self.page, cut))
        self.assertFalse(xs.past_cutoff(self.post(3), self.page, cut))
        self.assertFalse(xs.past_cutoff(self.post(90, is_pinned=True), self.page, cut))
        self.assertFalse(xs.past_cutoff(self.post(90, reposted_by="carol"), self.page, cut))
        self.assertFalse(xs.past_cutoff(self.post(90, handle="someone"), self.page, cut))
        self.assertFalse(xs.past_cutoff(self.post(90), dict(self.page, kind="home"), cut))
        self.assertTrue(xs.past_cutoff(self.post(20, handle="x"), {"kind": "search", "owner": None}, cut))

    def test_snowflake_fallback_when_no_time(self):
        cut = datetime.now(timezone.utc)
        self.assertTrue(xs.past_cutoff({"id": "2109607102188777707", "handle": "bob"}, self.page, cut))


class Allowlist(unittest.TestCase):
    def test_rejects_interaction(self):
        for args in (["click", "@e1"], ["type", "#x", "hi"], ["press", "Enter"], ["fill", "#x", "y"], ["keyboard", "type", "x"],
                     ["dblclick", "a"], ["hover", "a"], ["upload", "a", "f"], ["close"], ["cookies"], ["snapshot"],
                     ["eval", "document.title"], ["eval", "-b", base64.b64encode(b"1+1").decode()], ["open", "https://evil.example/"],
                     ["tab", "new", "https://x.com/home"], ["tab", "close"], ["tab", "close", "t1"], ["get", "html", "body"],
                     ["eval", "-b", base64.b64encode(xs.EXTRACT.read_bytes()).decode()],   # extract.js without a nonce
                     ["scroll", "down", "99999"], ["scroll", "down", "600"], ["wait", "--fn", "x"], []):
            with self.assertRaises(xs.NotAllowed, msg=args):
                xs.check_act(args)

    def test_allows_read_only_shapes(self):
        for args in (["open", "https://x.com/theo"], ["wait", "3000"], ["get", "url"], ["tab", "list"],
                     ["tab", "close", "x-scroll"], ["tab", "x-scroll"], ["tab", "t3"], ["tab", "new", "--label", "x-scroll", "https://x.com/home"],
                     ["tab", "new", "--label", "x-scroll", f"https://x.com/home#xs-{N}"],
                     ["eval", "-b", b64(xs.call_script("extract", N))], ["eval", "-b", b64(xs.call_script("mark", N))],
                     ["eval", "-b", b64(xs.scroll_script({"url": "https://x.com/home"}, 600, N))]):
            xs.check_act(args)

    def test_extract_hash_pinned(self):
        import hashlib
        self.assertEqual(hashlib.sha256(xs.EXTRACT.read_bytes()).hexdigest(), xs.EXTRACT_SHA256)
        self.assertEqual(hashlib.sha256(xs.SCROLL.read_bytes()).hexdigest(), xs.SCROLL_SHA256)
        self.assertEqual(hashlib.sha256(xs.MARK.read_bytes()).hexdigest(), xs.MARK_SHA256)

    def test_astra3_R1_25_only_the_pinned_scroll_script_with_a_well_formed_plan_runs(self):
        """The scroll eval is allowed only as `(<pinned scroll.js>)(<canonical plan>)`: an edited body, code smuggled in
        the plan, an oversized scroll or a malformed plan is refused."""
        good = xs.scroll_script({"url": "https://x.com/search?q=claude+code&f=live"}, 600, N)
        xs.check_act(["eval", "-b", b64(good)])
        body = xs.scroll_body()
        prefix = f"({body})("
        ok = prefix + '{"f":["top"],"lf":null,"nonce":"%s","path":"/home","pf":null,"px":600,"q":null})' % N
        xs.check_act(["eval", "-b", b64(ok)])   # the hand-written shape below is accepted when well formed
        bad = [good.replace("if (ours) window.scrollBy", "window.scrollBy"),        # check removed
               good.replace("&& mark === plan.nonce", ""),                           # tab-mark check removed
               good + ";alert(1)", good[:-1] + ",alert(1))",                          # extra code after or in the call
               ok.replace('"px":600', '"px":5000'), ok.replace('"px":600', '"px":true'),   # too far, not an int
               ok.replace(',"q":null', ''),                                         # missing key
               ok.replace('"f":["top"]', '"f":[]'),                                 # empty f
               ok.replace('"q":null}', '"q":null,"x":1}'),                          # extra key
               prefix + '{"path":"/home","nonce":"%s","f":["top"],"lf":null,"pf":null,"px":600,"q":null})' % N,  # not canonical
               ok.replace('"path":"/home"', '"path":(alert(1),"/home")'),
               ok.replace(f'"nonce":"{N}",', ''),                                  # no tab nonce
               ok.replace(f'"{N}"', 'null'), ok.replace(f'"{N}"', '""'), ok.replace(f'"{N}"', f'"{N[:-1]}"'),  # null, empty, short
               ok.replace(f'"{N}"', f'"{N.upper()}"'), ok.replace(f'"{N}"', f'["{N}"]')]  # not lowercase hex, not a string
        for src in bad:
            with self.assertRaises(xs.NotAllowed, msg=src[-120:]):
                xs.check_act(["eval", "-b", b64(src)])
        with mock.patch.object(xs, "SCROLL_SHA256", "0" * 64):   # scroll.js edited without re-pinning
            with self.assertRaises(xs.NotAllowed):
                xs.check_act(["eval", "-b", b64(good)])
            with self.assertRaises(xs.NotAllowed):
                xs.scroll_script({"url": "https://x.com/home"}, 600, N)

    def test_astra4_R1_25_extract_and_mark_run_only_pinned_with_a_nonce(self):
        """extract.js and mark.js run only as `(<pinned script>)(<a 32-hex nonce>)`: bare, edited, or with any other
        argument they are refused, and an unpinned edit is refused too."""
        for name in ("extract", "mark"):
            good = xs.call_script(name, N)
            xs.check_act(["eval", "-b", b64(good)])
            prefix = good[:-len(f'"{N}")')]
            for src in (prefix[:-1], prefix + ')', prefix + 'null)', prefix + f'"{N[:-1]}")', prefix + f'"{N.upper()}")',
                        prefix + f'["{N}"])', prefix + f' "{N}")', good + ';alert(1)', good[:-1] + ',alert(1))',
                        good.replace("sessionStorage", "localStorage", 1)):
                with self.assertRaises(xs.NotAllowed, msg=(name, src[-80:])):
                    xs.check_act(["eval", "-b", b64(src)])
            with mock.patch.object(xs, f"{name.upper()}_SHA256", "0" * 64):
                with self.assertRaises(xs.NotAllowed):
                    xs.check_act(["eval", "-b", b64(good)])
        with self.assertRaises(xs.NotAllowed):   # a scroll plan is not a mark/extract argument, nor the reverse
            xs.check_act(["eval", "-b", b64(xs.call_script("mark", xs.scroll_plan({"url": "https://x.com/home"}, 600, N)))])
        with self.assertRaises(xs.NotAllowed):
            xs.check_act(["eval", "-b", b64(xs.call_script("scroll", N))])


def b64(src):
    return base64.b64encode(src.encode()).decode()


def page_shows_plan(url, plan, mark, nav=None):
    """What scroll.js decides, in Python (ScrollJs checks the real script agrees): the page is the planned one, its
    document's in-memory mark is the plan's nonce, and its navigation entry carries #xs-<nonce> (a reload is no
    evidence). nav is the document's navigation entry (type, name); None: a page loaded at the URL it shows."""
    u = urlparse(url or "")
    _, name = nav or ("navigate", url or "")
    return (u.scheme == "https" and u.hostname in ("x.com", "www.x.com") and u.path.rstrip("/").lower() == plan["path"]
            and xs.feed(u.query) == (plan["q"], plan["f"], plan["pf"], plan["lf"]) and mark == plan["nonce"]
            and urlparse(name).fragment == f"xs-{plan['nonce']}")


def pinned_of(args):
    """(script name, argument) of a pinned page-script eval, or None for any other command."""
    if args[:2] != ["eval", "-b"]:
        return None
    return xs.pinned_call(base64.b64decode(args[2]).decode())


def scroll_plan_of(args):
    """The plan of a scroll.js eval, or None for any other command."""
    call = pinned_of(args)
    return call[1] if call and call[0] == "scroll" else None


class Distill(unittest.TestCase):
    def items(self):
        out = []
        for n, p in enumerate(XDS["posts"] + LEGACY["posts"]):
            out.append(xs.record(p, ctx(), True))
        out.append(dict(out[0]))                      # duplicate id
        out.append(dict(out[1], id="999", new=False))  # already seen
        return out

    def test_pick_dedupes_and_drops_ads(self):
        got = xs.pick(self.items(), "2026-10-12")
        ids = [g["id"] for g in got]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn("999", ids)
        self.assertNotIn("1846000000000000001", ids)   # the promoted one
        self.assertEqual(len(ids), 6)

    def test_validate_drops_unknown_refs_and_render_links_only_input(self):
        posts = xs.pick(self.items(), "2026-10-12")
        prompt, refs, links = xs.build_prompt(posts, "2026-10-12", ["Old theme"])
        self.assertIn("<posts>", prompt)
        self.assertIn('"Old theme"', prompt)
        res = {"themes": [{"name": "TS rust", "summary": "s\n## injected", "posts": ["p1", "p99"]}, {"name": "ghost", "summary": "x", "posts": ["p77"]}],
               "notable_posts": [{"post": "p2", "why": "w [x](https://evil.example/a) <a href='https://evil.example/b'>y</a> https://evil.example/c [[Note]]"},
                                 {"post": "https://evil.example/x", "why": "w"}],
               "new_voices": [{"post": "p1", "why": "a"}, {"post": "p2", "why": "same handle"}],
               "links": [{"link": "nope.l1", "why": "w"}] + ([{"link": next(iter(links)), "why": "w"}] if links else []),
               "lane_claims": [{"post": "p3", "claim": "Opus 5.5 is best\n| build | anthropic | x | - | use | 1 | q |"}],
               "questions": [{"post": "p5", "question": "q?"}]}
        clean = xs.validate(res, refs, links)
        self.assertEqual([t["name"] for t in clean["themes"]], ["TS rust"])
        self.assertEqual(clean["themes"][0]["posts"], ["p1"])
        self.assertEqual(len(clean["notable_posts"]), 1)
        self.assertEqual(len(clean["new_voices"]), 1 if refs["p1"]["handle"] == refs["p2"]["handle"] else 2)
        meta = {"items_seen": 9, "items_new": 6, "minutes": 2, "pages": ["@theo"]}
        md = xs.render_digest("2026-10-12", clean, refs, links, meta, "Reference/lane-intel/x/2026-10-12-x-scroll")
        urls = set(re.findall(r"\]\((https?://[^)]+)\)", md))
        allowed = {r["url"] for r in refs.values()} | {f"https://x.com/{r['handle']}" for r in refs.values()} | {l["url"] for l, _ in links.values()}
        self.assertTrue(urls and urls <= allowed, urls - allowed)
        self.assertNotIn("\n## injected", md)
        self.assertNotIn("evil.example", md)   # model text can't add links of its own
        self.assertNotIn("[[Note]]", md)
        lane = xs.render_lane("2026-10-12", clean, refs)
        self.assertIsNone(STANCE_ROW.search(lane))   # stance.py must never fold a stranger's claim

    def test_validate_maps_exact_urls_text_and_handles(self):
        posts = xs.pick(self.items(), "2026-10-12")
        _, refs, links = xs.build_prompt(posts, "2026-10-12", [])
        lref, (ln, _) = next(iter(links.items()))
        res = {"links": [{"link": ln["text"], "why": "by text"}, {"link": "https://evil.example/", "why": "x"}],
               "new_voices": [{"post": "@" + refs["p2"]["handle"], "why": "by handle"}, {"post": "@nobody_here", "why": "x"}],
               "notable_posts": [{"post": refs["p3"]["url"], "why": "by url"}, {"post": refs["p3"]["url"] + "9", "why": "x"}]}
        clean = xs.validate(res, refs, links)
        self.assertEqual([x["link"] for x in clean["links"]], [lref])
        self.assertEqual(len(clean["new_voices"]), 1)
        self.assertEqual([x["post"] for x in clean["notable_posts"]], ["p3"])

    def test_profile_roundtrip(self):
        posts = xs.pick(self.items(), "2026-10-12")
        _, refs, _ = xs.build_prompt(posts, "2026-10-12", [])
        clean = {"themes": [{"name": "TS Rust", "summary": "s", "posts": ["p1"]}]}
        prof = xs.update_profile({"days": {"2026-10-11": {"themes": {"ts rust": ["u"]}, "handles": {"theo": 2}}}}, "2026-10-12", clean, refs, posts)
        self.assertIn("ts rust", prof["days"]["2026-10-12"]["themes"])   # reuses the earlier spelling
        md = xs.render_profile(prof, "2026-10-12")
        self.assertIn("| ts rust | 2 |", md)
        self.assertLessEqual(len(md.splitlines()), 300)

    def test_vault_path_check(self):
        root = xs.VAULT / xs.DIGEST_DIR
        self.assertTrue(xs.under(root / "2026-10-12.md", [root]))
        self.assertFalse(xs.under(root / ".." / "evil.md", [root]))
        self.assertFalse(xs.under(Path("/tmp/x.md"), [root]))


class Watch(unittest.TestCase):
    def test_add_rm_list(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(xs, "CONFIG", Path(d) / "watch.json"):
            xs.main(["watch", "add", "@theo", "claude code", "@x", "bad handle!!"])
            cfg = xs.load_watch()
            self.assertEqual(cfg["handles"], ["theo", "x"])
            self.assertEqual(cfg["searches"], ["claude code", "bad handle!!"])
            xs.main(["watch", "rm", "@x", "bad handle!!"])
            xs.main(["watch", "minutes", "12"])
            cfg = xs.load_watch()
            self.assertEqual((cfg["handles"], cfg["searches"], cfg["minutes"]), (["theo"], ["claude code"], 12))
            with self.assertRaises(SystemExit):
                xs.main(["watch", "minutes", "45"])
            (Path(d) / "watch.json").write_text('{"handles": ["@a", 5, "", "i/flow/logout", "intent/follow?screen_name=x"], '
                                                '"searches": [null, "q"], "list": "https://evil/x"}')
            cfg = xs.load_watch()
            self.assertEqual((cfg["handles"], cfg["searches"], cfg["list"], cfg["minutes"]), (["a"], ["q"], None, 15))


class Plan(unittest.TestCase):
    cfg = {"handles": ["a", "b", "c"], "searches": ["q1"], "list": None, "minutes": 15}

    def test_minutes_caps(self):
        self.assertEqual(xs.plan_minutes(30, self.cfg, False, "2026-01-01", "2026-10-12"), 20)
        self.assertEqual(xs.plan_minutes(None, self.cfg, False, None, "2026-10-12"), 8)          # first two weeks
        self.assertEqual(xs.plan_minutes(None, self.cfg, False, "2026-10-01", "2026-10-12"), 8)
        self.assertEqual(xs.plan_minutes(2, self.cfg, True, None, "2026-10-12"), 2)

    def test_pages_least_recent_first_and_fit(self):
        st = {"@a": {"last_visit": "2026-10-11"}, "@b": {"last_visit": "2026-10-01"}}
        plan = xs.plan_pages(self.cfg, False, None, st, 4)   # 4 min: 0.6*240/60 = 2 extra pages
        self.assertEqual([p["source"] for p, _ in plan], ["home", "@c", "search:q1"])
        self.assertAlmostEqual(sum(w for _, w in plan), 1.0)
        self.assertIn("f=live", plan[2][0]["url"])
        self.assertEqual([p["source"] for p, _ in xs.plan_pages(self.cfg, True, None, {}, 2)], ["@a", "@b", "@c"])

    def test_block_reasons(self):
        ok = {"flags": {}, "logged_in": True}
        self.assertIsNone(xs.block_reason(ok, False))
        self.assertIsNone(xs.block_reason({"flags": {}, "logged_in": False}, False))   # weak signal: only the first page judges it
        self.assertEqual(xs.block_reason({"flags": {"login_links": True}, "logged_in": False}, False), "logged_out")
        self.assertIsNone(xs.block_reason({"flags": {"login_links": True}, "logged_in": False}, True))
        self.assertEqual(xs.block_reason({"flags": {"retry": True}, "logged_in": True}, False), "rate_limit")
        self.assertEqual(xs.block_reason({"flags": {"captcha": True, "retry": True}}, True), "captcha")


class Loop(unittest.TestCase):
    """run_session with the browser faked: only our tab is closed, the previous tab comes back, a block pauses 72 h."""

    def run_fake(self, pages, dry=False):
        seq = iter(pages)
        return self.run_fake_extract(lambda: next(seq, pages[-1]), dry)

    def run_fake_extract(self, extract, dry=False, handles=(), on_act=None, searches=()):
        """on_act(args, tabs) runs after each faked command: it plays another agent using the shared browser. The faked
        extract reports the active tab's URL unless the page dict names one."""
        calls, n = [], [1]
        tabs = [{"tabId": "t1", "label": None, "active": True, "url": "https://example.com/"}]

        def activate(tab):
            for t in tabs:
                t["active"] = t is tab

        def fake_act(args, timeout=60):
            xs.check_act(args)
            calls.append(args)
            if args[:2] == ["tab", "new"]:
                n[0] += 1
                tabs.append({"tabId": f"t{n[0]}", "label": "x-scroll", "url": args[4], "nav": ("navigate", args[4])})
                activate(tabs[-1])
            elif args == ["tab", "close", "x-scroll"]:
                tabs[:] = [t for t in tabs if t["label"] != "x-scroll"]
            elif args[0] == "tab" and len(args) == 2 and args[1] != "list":
                tab = next((t for t in tabs if args[1] in (t["tabId"], t["label"])), None)
                if tab is None:
                    raise xs.ActFail(f"no tab {args[1]}")
                activate(tab)
            elif args[0] == "open":
                active = next(t for t in tabs if t.get("active"))
                active["url"], active["nav"] = args[1], ("navigate", args[1])   # a new document in that tab...
                active.pop("mark", None)                                         # ...without the old one's memory
            call = pinned_of(args)
            if call and call[0] == "mark":   # mark.js: marks the active page (in its memory, "mark") only if it carries
                # this nonce's fragment
                active = next((t for t in tabs if t.get("active")), {})
                u = urlparse(active.get("url") or "")
                if u.scheme == "https" and u.hostname in ("x.com", "www.x.com") and u.fragment == f"xs-{call[1]}":
                    active["mark"] = call[1]
                self.marks.append((active.get("tabId"), active.get("mark") == call[1]))
            if on_act:
                on_act(args, tabs)
            if args == ["tab", "list"]:
                return {"tabs": [{k: v for k, v in t.items() if k not in ("mark", "nav")} for t in copy.deepcopy(tabs)]}
            if call and call[0] == "mark":
                return {"result": json.dumps({"v": 1, "marked": self.marks[-1][1]})}
            plan = scroll_plan_of(args)
            if plan is not None:   # scroll.js runs in whatever tab is active now, and scrolls it only if it is the plan
                active = next((t for t in tabs if t.get("active")), {})   # in x-scroll's marked tab
                ours = page_shows_plan(active.get("url"), plan, active.get("mark"), active.get("nav"))
                if ours:
                    self.scrolled.append(active["tabId"])
                return {"result": json.dumps({"v": 3, "scrolled": ours, "url": active.get("url")})}
            return {}

        def fake_extract():   # extract.js reads only x-scroll's marked tab (decided before the faked read runs)
            self.extracts += 1
            active = next((t for t in tabs if t.get("active")), {})
            marked = xs.TAB_NONCE is not None and active.get("mark") == xs.TAB_NONCE
            self.read_on.append(active.get("tabId"))
            d = dict(extract()) if marked else {}
            d.setdefault("url", active.get("url"))
            d.setdefault("marked", marked)
            return d
        # scrolled: the tabId each scroll actually landed on; marks: (tabId, marked) per mark.js run; read_on: per extract
        self.extracts, self.scrolled, self.marks, self.read_on = 0, [], [], []
        with tempfile.TemporaryDirectory() as d, mock.patch.object(xs, "STATE", Path(d)), mock.patch.object(xs, "act", fake_act), \
                mock.patch.object(xs, "extract", fake_extract), mock.patch.object(xs.time, "sleep", lambda s: None), \
                mock.patch.object(xs, "gate", lambda today: None), mock.patch.object(xs, "tg", lambda m, d: calls.append(["tg", m])), \
                mock.patch.object(xs, "load_watch", lambda: {"handles": list(handles), "searches": list(searches), "list": None, "minutes": 2}):
            args = xs.argparse.Namespace(minutes=2, dry_run=dry, url=None, scheduled=False)
            rc = xs.run_session(args)
            state = {p.name: p for p in Path(d).rglob("*") if p.is_file()}
            caps = [json.loads(l) for p in state.values() if p.suffix == ".jsonl" and p.name != "sessions.jsonl" for l in p.read_text().splitlines()]
            pause = state["pause-until"].read_text() if "pause-until" in state else None
            sess = [json.loads(l) for l in state["sessions.jsonl"].read_text().splitlines()]
            self.pages = json.loads(state["pages.json"].read_text()) if "pages.json" in state else {}
        return rc, calls, caps, pause, sess

    def logged_in(self, posts, **flags):
        return {"logged_in": True, "flags": flags, "posts": posts}   # url: the active tab's (fake_extract)

    def test_stall_then_clean_close(self):
        page = self.logged_in(LEGACY["posts"])
        rc, calls, caps, pause, sess = self.run_fake([page])
        self.assertEqual(rc, 0)
        self.assertEqual(len(caps), 2)
        self.assertIn(["tab", "close", "x-scroll"], calls)
        self.assertEqual(calls[-1], ["tab", "t1"])
        self.assertNotIn(["close"], calls)
        self.assertIsNone(pause)
        self.assertEqual(sess[-1]["pages"][0]["stop"], "no-new-posts")

    def test_block_pauses_and_notifies(self):
        rc, calls, caps, pause, sess = self.run_fake([self.logged_in(LEGACY["posts"]), self.logged_in([], rate_limit=True)])
        self.assertEqual(rc, 3)
        self.assertEqual(sess[-1]["outcome"], "blocked:rate_limit")
        self.assertTrue(pause)
        self.assertEqual(len(caps), 2)   # what was seen before the block is still written
        self.assertTrue(any(c[0] == "tg" for c in calls))
        self.assertIn(["tab", "close", "x-scroll"], calls)

    def test_needs_login_on_first_page(self):
        out = {"url": "https://x.com/i/flow/login", "logged_in": False, "flags": {"login_page": True}, "posts": []}
        rc, calls, caps, pause, sess = self.run_fake([out])
        self.assertEqual((rc, sess[-1]["outcome"], pause), (4, "needs_login", None))

    def test_waits_for_load_then_runs(self):
        loading = {"url": "https://x.com/home", "logged_in": False, "flags": {}, "posts": []}
        rc, calls, caps, pause, sess = self.run_fake([loading, loading, self.logged_in(LEGACY["posts"])])
        self.assertEqual((rc, sess[-1]["outcome"], len(caps)), (0, "ok", 2))

    def test_first_page_never_signed_in_is_needs_login(self):
        out = {"url": "https://x.com/home", "logged_in": False, "flags": {}, "posts": []}
        rc, calls, caps, pause, sess = self.run_fake([out])
        self.assertEqual((rc, sess[-1]["outcome"], pause), (4, "needs_login", None))

    def test_extract_crash_still_flushes_and_closes(self):
        pages = iter([self.logged_in(LEGACY["posts"])])

        def boom():
            try:
                return next(pages)
            except StopIteration:
                raise ValueError("bad json")
        with mock.patch.object(xs, "pace", lambda: 0):
            rc, calls, caps, pause, sess = self.run_fake_extract(boom)
        self.assertTrue(sess[-1]["outcome"].startswith("error:ValueError"))
        self.assertEqual(len(caps), 2)
        self.assertIn(["tab", "close", "x-scroll"], calls)

    def test_second_page_gets_its_own_tab(self):
        page = self.logged_in(LEGACY["posts"])
        with mock.patch.object(xs, "cpu_temp", lambda: None):
            rc, calls, caps, pause, sess = self.run_fake_extract(lambda: page, handles=["bob"])
        self.assertEqual([p["source"] for p in sess[-1]["pages"]], ["home", "@bob"])
        self.assertFalse(any(c[0] == "open" for c in calls))   # never navigates whatever tab happens to be active
        self.assertIn("https://x.com/bob", opened(calls))
        self.assertEqual(calls[-1], ["tab", "t1"])

    def test_heat_stops_mid_page(self):
        temps = iter([None, 90])   # cool before the first page, hot after the first scroll step
        with mock.patch.object(xs, "cpu_temp", lambda: next(temps, 90)):
            rc, calls, caps, pause, sess = self.run_fake([self.logged_in(LEGACY["posts"])])
        self.assertEqual((rc, sess[-1]["outcome"], len(caps), pause), (0, "heat:90C", 2, None))
        self.assertEqual(self.scrolled, [])
        self.assertIn(["tab", "close", "x-scroll"], calls)

    def test_dry_run_refuses_signed_in_browser(self):
        rc, calls, caps, pause, sess = self.run_fake([self.logged_in(LEGACY["posts"])], dry=True)
        self.assertTrue(sess[-1]["outcome"].startswith("error:dry-run expects"))
        self.assertEqual(self.scrolled, [])
        self.assertFalse(any(c[0] == "tg" for c in calls))

    def test_other_x_tab_active_is_wrong_tab(self):
        """Another agent's X tab is active and stays active: nothing is captured under the planned source, no `open` lands
        on the foreign tab, and the page records wrong-tab."""
        def other_agent(args, tabs):   # right after x-scroll marked its tab, and whenever it re-selects it
            if (pinned_of(args) or ("",))[0] == "mark" or args == ["tab", "x-scroll"]:
                if not any(t["tabId"] == "t9" for t in tabs):
                    tabs.append({"tabId": "t9", "label": None, "url": "https://x.com/someone"})
                for t in tabs:
                    t["active"] = t["tabId"] == "t9"
        with mock.patch.object(xs, "cpu_temp", lambda: None):
            rc, calls, caps, pause, sess = self.run_fake_extract(lambda: self.logged_in(LEGACY["posts"]), on_act=other_agent)
        self.assertEqual((rc, len(caps), self.extracts), (3, 0, 0))
        self.assertTrue(sess[-1]["outcome"].startswith("error:wrong-tab"))
        self.assertEqual(sess[-1]["pages"][0]["stop"], "wrong-tab")
        self.assertIn(["tab", "x-scroll"], calls)                       # tried to re-select its own tab
        self.assertFalse(any(c[0] == "open" for c in calls))   # never navigates the other agent's tab
        self.assertEqual(self.scrolled, [])                    # and never scrolls it
        self.assertNotIn("last_visit", self.pages.get("home", {}))
        self.assertIn(["tab", "close", "x-scroll"], calls)

    def test_own_tab_on_wrong_url_is_reopened(self):
        drifted = []

        def drift(args, tabs):
            if (pinned_of(args) or ("",))[0] == "mark" and not drifted:
                drifted.append(1)
                tabs[-1]["url"] = "https://x.com/someone/status/1"   # something navigated the labelled tab (it keeps its mark)
        with mock.patch.object(xs, "cpu_temp", lambda: None):
            rc, calls, caps, pause, sess = self.run_fake_extract(lambda: self.logged_in(LEGACY["posts"]), on_act=drift)
        self.assertEqual((rc, sess[-1]["outcome"], len(caps)), (0, "ok", 2))
        self.assertEqual(opened(calls).count("https://x.com/home"), 2)   # re-opened once
        self.assertFalse(any(c[0] == "open" for c in calls))
        self.assertEqual({c["source"] for c in caps}, {"home"})

    def test_own_tab_still_wrong_after_reopen_aborts(self):
        def stuck(args, tabs):
            if (pinned_of(args) or ("",))[0] == "mark" or args[0] == "open":
                next(t for t in tabs if t["label"] == "x-scroll")["url"] = "https://x.com/someone"
        with mock.patch.object(xs, "cpu_temp", lambda: None):
            rc, calls, caps, pause, sess = self.run_fake_extract(lambda: self.logged_in(LEGACY["posts"]), on_act=stuck)
        self.assertEqual((rc, len(caps), self.extracts), (3, 0, 0))
        self.assertEqual(sess[-1]["pages"][0]["stop"], "wrong-tab")
        self.assertEqual(opened(calls).count("https://x.com/home"), 2)   # re-opened once
        self.assertFalse(any(c[0] == "open" for c in calls))

    def test_astra2_25_tab_switch_after_extract_stops_before_scroll(self):
        """Another agent activates its X tab right after an extraction: the next scroll must not land on it; the session
        stops on wrong-tab with only what our own tab showed."""
        box, scrolled_on = {}, []

        def watch(args, tabs):
            box["tabs"] = tabs
            if scroll_plan_of(args) is not None:
                scrolled_on.append(next(t["tabId"] for t in tabs if t.get("active")))

        def extract_then_switch():
            tabs = box["tabs"]
            if not any(t["tabId"] == "t9" for t in tabs):
                tabs.append({"tabId": "t9", "label": None, "url": "https://x.com/someone"})
            for t in tabs:
                t["active"] = t["tabId"] == "t9"
            return dict(self.logged_in(LEGACY["posts"]), url="https://x.com/home")   # read while our tab was active
        with mock.patch.object(xs, "cpu_temp", lambda: None):
            rc, calls, caps, pause, sess = self.run_fake_extract(extract_then_switch, on_act=watch)
        self.assertEqual(scrolled_on, [])                                 # never scrolled the foreign tab
        self.assertEqual((rc, self.extracts, len(caps)), (3, 1, 2))
        self.assertTrue(sess[-1]["outcome"].startswith("error:wrong-tab"))
        self.assertEqual(sess[-1]["pages"][0]["stop"], "wrong-tab")
        self.assertFalse(any(c[0] == "open" or scroll_plan_of(c) for c in calls))
        self.assertEqual(self.scrolled, [])
        self.assertNotIn("last_visit", self.pages.get("home", {}))

    def test_astra2_25_reopen_never_navigates_a_tab_switched_in_after_the_check(self):
        """Our labelled tab drifted; tab_ok re-selects and checks it, and another agent activates its own tab right after
        that check returns. The re-open must not navigate the other agent's tab (an `open` would land on it)."""
        box, armed, drifted = {}, [], []

        def on(args, tabs):
            box["tabs"] = tabs
            if (pinned_of(args) or ("",))[0] == "mark" and not drifted:
                drifted.append(1)
                tabs[-1]["url"] = "https://x.com/someone/status/1"
            if args == ["tab", "x-scroll"]:
                armed.append(1)
        real_tabs = xs.tabs

        def tabs_then_other_agent_switches():
            out = real_tabs()
            if armed:
                armed.clear()
                box["tabs"].append({"tabId": "t9", "label": None, "url": "https://example.org/half-filled-form"})
                for t in box["tabs"]:
                    t["active"] = t["tabId"] == "t9"
            return out
        with mock.patch.object(xs, "cpu_temp", lambda: None), mock.patch.object(xs, "tabs", tabs_then_other_agent_switches):
            rc, calls, caps, pause, sess = self.run_fake_extract(lambda: self.logged_in(LEGACY["posts"]), on_act=on)
        t9 = next(t for t in box["tabs"] if t["tabId"] == "t9")
        self.assertEqual(t9["url"], "https://example.org/half-filled-form")   # the other agent's page is untouched
        self.assertFalse(any(c[0] == "open" for c in calls))
        self.assertEqual((rc, sess[-1]["outcome"], len(caps)), (0, "ok", 2))
        self.assertEqual({c["source"] for c in caps}, {"home"})

    def test_astra3_R1_25_tab_switch_after_the_pre_scroll_check_scrolls_nothing(self):
        """Astra round 3: another agent activates its X tab right after the pre-scroll tab_check returns, before the
        scroll. Before the fix a separate `web act scroll` landed on that tab; now scroll.js checks the page and scrolls
        in one evaluation, so nothing scrolls and the session stops on wrong-tab."""
        box, armed, evals_on = {}, [], []

        def on(args, tabs):
            box["tabs"] = tabs
            if scroll_plan_of(args) is not None:
                evals_on.append(next(t["tabId"] for t in tabs if t.get("active")))
        real_tabs = xs.tabs

        def tabs_then_other_agent_switches():
            out = real_tabs()
            if self.extracts == 1 and not armed:   # the first tab list after the extract is the pre-scroll tab_check
                armed.append(1)
                box["tabs"].append({"tabId": "t9", "label": None, "url": "https://x.com/someone"})
                for t in box["tabs"]:
                    t["active"] = t["tabId"] == "t9"
            return out
        with mock.patch.object(xs, "cpu_temp", lambda: None), mock.patch.object(xs, "tabs", tabs_then_other_agent_switches):
            rc, calls, caps, pause, sess = self.run_fake_extract(lambda: self.logged_in(LEGACY["posts"]), on_act=on)
        self.assertEqual(armed, [1])
        self.assertEqual(evals_on, ["t9"])          # the scroll attempt ran in the foreign tab...
        self.assertEqual(self.scrolled, [])         # ...and scrolled nothing there
        self.assertFalse(any(c[0] in ("scroll", "open") for c in calls))
        self.assertEqual((rc, self.extracts, len(caps)), (3, 1, 2))
        self.assertTrue(sess[-1]["outcome"].startswith("error:wrong-tab"))
        self.assertEqual(sess[-1]["pages"][0]["stop"], "wrong-tab")
        self.assertNotIn("last_visit", self.pages.get("home", {}))

    def test_astra3_R1_25_own_tab_scrolls_through_scroll_js(self):
        """A normal session scrolls only its own labelled tab, and only through scroll.js."""
        with mock.patch.object(xs, "cpu_temp", lambda: None):
            rc, calls, caps, pause, sess = self.run_fake([self.logged_in(LEGACY["posts"])])
        self.assertEqual((rc, sess[-1]["outcome"]), (0, "ok"))
        self.assertTrue(self.scrolled)
        self.assertEqual(set(self.scrolled), {"t2"})
        self.assertFalse(any(c[0] == "scroll" for c in calls))

    def test_astra3_R1_25_same_url_foreign_tab_switched_in_after_the_check_is_not_scrolled(self):
        """Reviewer variant: the tab another agent activates right after the pre-scroll tab_check shows the very same
        planned page (x.com/home is where any signed-in X tab lands), loaded ten minutes earlier. A URL check alone
        passed it and scrolled it (and the session went on as ok); it has no x-scroll mark, so scroll.js refuses it."""
        box, armed, evals_on = {}, [], []

        def on(args, tabs):
            box["tabs"] = tabs
            if scroll_plan_of(args) is not None:
                evals_on.append(next(t["tabId"] for t in tabs if t.get("active")))
        real_tabs = xs.tabs

        def tabs_then_other_agent_switches():
            out = real_tabs()
            if self.extracts == 1 and not armed:   # the first tab list after the extract is the pre-scroll tab_check
                armed.append(1)
                box["tabs"].append({"tabId": "t9", "label": None, "url": "https://x.com/home"})
                for t in box["tabs"]:
                    t["active"] = t["tabId"] == "t9"
            return out
        with mock.patch.object(xs, "cpu_temp", lambda: None), mock.patch.object(xs, "tabs", tabs_then_other_agent_switches):
            rc, calls, caps, pause, sess = self.run_fake_extract(lambda: self.logged_in(LEGACY["posts"]), on_act=on)
        self.assertEqual((armed, evals_on), ([1], ["t9"]))   # the scroll attempt ran in the same-URL foreign tab...
        self.assertEqual(self.scrolled, [])                  # ...and scrolled nothing there
        self.assertEqual((rc, self.extracts, len(caps)), (3, 1, 2))
        self.assertTrue(sess[-1]["outcome"].startswith("error:wrong-tab"))
        self.assertEqual(sess[-1]["pages"][0]["stop"], "wrong-tab")
        self.assertNotIn("last_visit", self.pages.get("home", {}))

    def test_astra4_R1_25_each_new_tab_gets_its_own_nonce_and_mark(self):
        """Every fresh labelled tab (each page, and tab_ok's re-open) opens at the planned URL plus a fresh #xs-<nonce>
        and is marked before anything reads or scrolls it, so x-scroll's own tabs keep working; with no marked tab,
        neither extract() nor scroll() evaluates anything."""
        page = self.logged_in(LEGACY["posts"])
        with mock.patch.object(xs, "cpu_temp", lambda: None):
            rc, calls, caps, pause, sess = self.run_fake_extract(lambda: page, handles=["bob"])
        self.assertEqual((rc, sess[-1]["outcome"]), (0, "ok"))
        self.assertEqual(sorted(set(self.scrolled)), ["t2", "t3"])   # home in t2, @bob in t3
        frags = [urlparse(c[4]).fragment for c in calls if c[:2] == ["tab", "new"]]
        self.assertTrue(all(re.fullmatch(r"xs-[0-9a-f]{32}", f) for f in frags), frags)
        self.assertEqual(len(set(frags)), len(frags))                  # a fresh nonce per tab
        self.assertEqual([m for m in self.marks if m[1]], [("t2", True), ("t3", True)])
        for i, c in enumerate(calls):   # every read or scroll comes after its tab's mark
            if pinned_of(c) and pinned_of(c)[0] == "scroll":
                self.assertTrue(any((pinned_of(d) or ("",))[0] == "mark" for d in calls[:i]))
        drifted = []

        def drift(args, tabs):
            if (pinned_of(args) or ("",))[0] == "mark" and not drifted:
                drifted.append(1)
                tabs[-1]["url"] = "https://x.com/someone/status/1"
        with mock.patch.object(xs, "cpu_temp", lambda: None):
            rc, calls, caps, pause, sess = self.run_fake_extract(lambda: page, on_act=drift)
        self.assertEqual((rc, sess[-1]["outcome"]), (0, "ok"))
        self.assertEqual(set(self.scrolled), {"t3"})   # the re-opened tab, not the drifted t2
        with mock.patch.object(xs, "TAB_NONCE", None), mock.patch.object(xs, "act", lambda *a, **k: self.fail("no eval")):
            self.assertFalse(xs.scroll({"url": "https://x.com/home"}))
            self.assertEqual(xs.extract(), {})

    def switch_in(self, foreign, when):
        """run_session where another agent activates its tab `foreign` (a dict, or a function of the tab list giving
        one) the first time when(args) holds: args is each faked command, checked right after it ran, or "listed",
        checked right after each tab list x-scroll reads has returned. The tab stays active unless x-scroll
        re-selects its own."""
        box, armed = {}, []

        def switch(args):
            if not armed and box and when(args):
                armed.append(1)
                tab = dict(foreign(box["tabs"]) if callable(foreign) else foreign)
                box["tabs"].append(tab)
                for t in box["tabs"]:
                    t["active"] = t is tab

        def on(args, tabs):
            box["tabs"] = tabs
            switch(args)
        real_tabs = xs.tabs

        def tabs_then_switch():
            out = real_tabs()
            switch("listed")
            return out
        with mock.patch.object(xs, "cpu_temp", lambda: None), mock.patch.object(xs, "MARK_TRIES", 3), \
                mock.patch.object(xs, "tabs", tabs_then_switch):
            out = self.run_fake_extract(lambda: self.logged_in(LEGACY["posts"]), on_act=on)
        self.assertEqual(armed, [1])
        return out + (next(t for t in box["tabs"] if t["tabId"] == "t9"),)

    def test_astra4_R1_25_same_url_foreign_tab_opened_with_ours_is_not_scrolled(self):
        """Astra round 4: another agent's tab on the very same URL, opened at the same moment as x-scroll's (so any
        page-age window passes it), is switched in right after the pre-scroll tab_check returns. It carries no x-scroll
        mark, so scroll.js scrolls nothing and the session stops on wrong-tab."""
        rc, calls, caps, pause, sess, t9 = self.switch_in({"tabId": "t9", "label": None, "url": "https://x.com/home"},
                                                          lambda args: args == "listed" and self.extracts == 1)
        self.assertEqual(len([c for c in calls if scroll_plan_of(c)]), 1)   # the scroll attempt ran in the foreign tab...
        self.assertEqual(self.scrolled, [])                                  # ...and scrolled nothing there
        self.assertNotIn("mark", t9)
        self.assertEqual((rc, self.extracts, len(caps)), (3, 1, 2))
        self.assertTrue(sess[-1]["outcome"].startswith("error:wrong-tab"))
        self.assertEqual(sess[-1]["pages"][0]["stop"], "wrong-tab")
        self.assertNotIn("last_visit", self.pages.get("home", {}))

    def test_astra4_R1_25_foreign_tab_switched_in_before_the_mark_is_never_marked(self):
        """The same-URL foreign tab becomes active right after `tab new`, before mark.js runs: it has no #xs-<nonce>
        fragment, so it is never marked; x-scroll reads and scrolls nothing and the session stops (Unmarked)."""
        rc, calls, caps, pause, sess, t9 = self.switch_in({"tabId": "t9", "label": None, "url": "https://x.com/home"},
                                                          lambda args: args[:2] == ["tab", "new"])
        self.assertNotIn("mark", t9)
        self.assertEqual(self.marks, [("t9", False)] * 3)   # every mark attempt landed on the foreign tab and refused it
        self.assertEqual((rc, self.extracts, len(caps), self.scrolled), (3, 0, 0, []))
        self.assertFalse(any(scroll_plan_of(c) or c[0] == "open" for c in calls))
        self.assertTrue(sess[-1]["outcome"].startswith("error:Unmarked"), sess[-1]["outcome"])
        self.assertIn(["tab", "close", "x-scroll"], calls)
        self.assertEqual(self.pages.get("home", {}), {})

    def test_astra4_R1_25_foreign_tab_with_our_copied_url_and_fragment_is_not_read_or_scrolled(self):
        """Another agent opens our tab's full URL, #xs-<nonce> included (it is visible in `tab list`), and switches to
        it after x-scroll's tab is marked and checked, before the extract. The fragment authorizes nothing: only
        mark.js, run right after x-scroll's own `tab new`, marks a tab, so nothing is read and the session stops."""
        def copy_of_ours(tabs):
            return {"tabId": "t9", "label": None, "url": next(t["url"] for t in tabs if t["label"] == "x-scroll")}
        rc, calls, caps, pause, sess, t9 = self.switch_in(copy_of_ours, lambda args: args == "listed" and xs.TAB_NONCE
                                                          and self.extracts == 0)
        self.assertEqual(t9["url"], next(c[4] for c in calls if c[:2] == ["tab", "new"]))
        self.assertTrue(t9["url"].startswith("https://x.com/home#xs-"))
        self.assertNotIn("mark", t9)
        self.assertEqual((self.extracts, self.read_on, len(caps), self.scrolled), (1, ["t9"], 0, []))
        self.assertTrue(sess[-1]["outcome"].startswith("error:wrong-tab"))
        self.assertEqual(sess[-1]["pages"][0]["stop"], "wrong-tab")

    def test_astra4_R1_25_page_another_agent_opens_in_our_tab_is_not_scrolled(self):
        """Reviewer variant: right after the pre-scroll tab_check, another agent runs `web act open https://x.com/home`
        and it lands in x-scroll's own tab (the active one). The tab keeps its label and its URL is the planned one, but
        the document is the other agent's, without x-scroll's in-memory mark: scroll.js scrolls nothing there and the
        session stops on wrong-tab."""
        box, armed = {}, []

        def on(args, tabs):
            box["tabs"] = tabs
        real_tabs = xs.tabs

        def tabs_then_other_agent_opens_in_the_active_tab():
            out = real_tabs()
            if self.extracts == 1 and not armed:   # the first tab list after the extract is the pre-scroll tab_check
                armed.append(1)
                active = next(t for t in box["tabs"] if t.get("active"))
                self.assertEqual((active["label"], active.get("mark")), ("x-scroll", xs.TAB_NONCE))
                active["url"], active["nav"] = "https://x.com/home", ("navigate", "https://x.com/home")
                active.pop("mark")   # a new document: x-scroll's in-memory mark is gone with the old one
            return out
        with mock.patch.object(xs, "cpu_temp", lambda: None), \
                mock.patch.object(xs, "tabs", tabs_then_other_agent_opens_in_the_active_tab):
            rc, calls, caps, pause, sess = self.run_fake_extract(lambda: self.logged_in(LEGACY["posts"]), on_act=on)
        self.assertEqual(armed, [1])
        self.assertEqual(len([c for c in calls if scroll_plan_of(c)]), 1)   # the scroll attempt ran in x-scroll's tab...
        self.assertEqual(self.scrolled, [])                                  # ...and scrolled nothing in the other page
        self.assertEqual((rc, self.extracts, len(caps)), (3, 1, 2))
        self.assertTrue(sess[-1]["outcome"].startswith("error:wrong-tab"))
        self.assertEqual(sess[-1]["pages"][0]["stop"], "wrong-tab")
        self.assertNotIn("last_visit", self.pages.get("home", {}))

    def test_astra5_R1_25_reloaded_foreign_tab_with_an_opener_copied_nonce_is_not_read_or_scrolled(self):
        """Astra round 5: a foreign tab opened from x-scroll's tab on the planned URL carries a copy of its tab storage
        (the nonce; "storage" here) but not its page memory, and has been reloaded. Switched in before the extract it is
        not read; switched in after the pre-scroll check it is not scrolled. Either way the session stops on wrong-tab
        and nothing re-marks it."""
        def opened_from_ours(tabs):
            return {"tabId": "t9", "label": None, "url": "https://x.com/home", "nav": ("reload", "https://x.com/home"),
                    "storage": xs.TAB_NONCE, "opener": next(t["tabId"] for t in tabs if t["label"] == "x-scroll")}
        for when, extracts, reads in ((lambda a: a == "listed" and xs.TAB_NONCE and self.extracts == 0, 1, ["t9"]),
                                      (lambda a: a == "listed" and self.extracts == 1, 1, ["t2"])):
            rc, calls, caps, pause, sess, t9 = self.switch_in(opened_from_ours, when)
            self.assertEqual((t9["storage"], t9["nav"][0]), (xs.TAB_NONCE or t9["storage"], "reload"))
            self.assertNotIn("mark", t9)
            self.assertEqual((self.extracts, self.read_on, self.scrolled), (extracts, reads, []))
            self.assertEqual(len([m for m in self.marks if m[1]]), 1)   # only x-scroll's own tab, right after `tab new`
            self.assertEqual(rc, 3)
            self.assertTrue(sess[-1]["outcome"].startswith("error:wrong-tab"), sess[-1]["outcome"])
            self.assertEqual(sess[-1]["pages"][0]["stop"], "wrong-tab")
            self.assertNotIn("last_visit", self.pages.get("home", {}))
            if reads == ["t2"]:   # it was the scroll that landed on the foreign tab
                self.assertEqual(len([c for c in calls if scroll_plan_of(c)]), 1)

    def test_astra5_R1_25_reload_of_our_own_tab_ends_the_session_unmarked(self):
        """A reload of x-scroll's own tab (by anyone) is a new document without the in-memory mark: it is no evidence
        of ownership, so the next step reads and scrolls nothing and the session stops on wrong-tab, without re-marking
        the page (only new_tab marks, right after its own `tab new`)."""
        reloaded = []

        def reload_after_first_scroll(args, tabs):   # between steps: the first command after the first scroll landed
            if self.scrolled and scroll_plan_of(args) is None and not reloaded:
                reloaded.append(1)
                active = next(t for t in tabs if t.get("active"))
                active["nav"] = ("reload", active["url"])
                active.pop("mark", None)
        page = self.logged_in(LEGACY["posts"])
        with mock.patch.object(xs, "cpu_temp", lambda: None):
            rc, calls, caps, pause, sess = self.run_fake_extract(lambda: page, on_act=reload_after_first_scroll)
        self.assertEqual(reloaded, [1])
        self.assertEqual((self.extracts, self.scrolled), (2, ["t2"]))   # one scroll before the reload, none after
        self.assertEqual(self.marks, [("t2", True)])                    # never re-marked
        self.assertEqual(rc, 3)
        self.assertTrue(sess[-1]["outcome"].startswith("error:wrong-tab"), sess[-1]["outcome"])
        self.assertEqual(sess[-1]["pages"][0]["stop"], "wrong-tab")

    def test_astra5_R1_25_unmarked_sign_in_or_lock_page_still_stops_as_a_block(self):
        """X sends x-scroll's tab to a lock or sign-in page with a full page load, which drops the in-memory mark.
        Nothing is read there (extract.js returns only its URL), but the session still stops as a block judged from
        the URL alone (unmarked_block): a lock page pauses 72 h, a sign-in page on the first page is needs_login. Any
        other unmarked page is wrong-tab."""
        for url, outcome, paused in (("https://x.com/account/access", "blocked:locked", True),
                                     ("https://x.com/i/flow/login?redirect_after_login=%2Fhome", "needs_login", False),
                                     ("https://x.com/home", "error:wrong-tab (identity browser busy)", False)):
            def x_navigates(args, tabs, url=url):
                if pinned_of(args) and pinned_of(args)[0] == "mark":
                    active = next(t for t in tabs if t.get("active"))
                    active["url"], active["nav"] = url, ("navigate", url)
                    active.pop("mark", None)
            with mock.patch.object(xs, "cpu_temp", lambda: None):
                rc, calls, caps, pause, sess = self.run_fake_extract(lambda: self.fail("nothing is read"), on_act=x_navigates)
            self.assertEqual((sess[-1]["outcome"], pause is not None, self.scrolled, len(caps)), (outcome, paused, [], 0), url)
        self.assertEqual([xs.unmarked_block(u) for u in ("https://x.com/account/locked", "https://www.x.com/login",
                                                         "https://x.com/i/flow/signup", "https://x.com/home",
                                                         "http://x.com/account/access", "https://x.com.evil.example/login",
                                                         "https://x.com/i/flow/whatever", None)],
                         ["locked", "login_wall", "login_wall", None, None, None, None, None])

    def test_astra5_R1_25_foreign_sign_in_or_lock_tab_switched_in_for_the_read_does_not_pause(self):
        """Reviewer variant (round 5): another agent's own X tab on a lock page or an X sign-in flow (not x-scroll's,
        no label, no mark) becomes active right after tab_ok's tab list, so the extract lands on it. It is unmarked and
        its URL is one unmarked_block knows, but it is not x-scroll's tab: nothing is read, and the session stops on
        wrong-tab, without a 72 h pause or a sign-in request. Before the fix it paused x-scroll for 72 h (lock URL) or
        asked the user to sign in (sign-in URL)."""
        for url in ("https://x.com/account/access", "https://x.com/i/flow/login?redirect_after_login=%2Fhome"):
            rc, calls, caps, pause, sess, t9 = self.switch_in({"tabId": "t9", "label": None, "url": url},
                                                              lambda a: a == "listed" and xs.TAB_NONCE and self.extracts == 0)
            self.assertNotIn("mark", t9)
            self.assertEqual((self.extracts, self.read_on, self.scrolled, len(caps)), (1, ["t9"], [], 0), url)
            self.assertEqual((rc, sess[-1]["outcome"], pause), (3, "error:wrong-tab (identity browser busy)", None), url)
            self.assertEqual(sess[-1]["pages"][0]["stop"], "wrong-tab", url)
            self.assertEqual([c[1][:15] for c in calls if c[0] == "tg"], ["session failed:"], url)

    def test_astra2_9_top_feed_extract_is_not_captured_as_latest(self):
        """The planned Latest search reads back as Top (f=top, same q) with an old popular post: nothing is captured under
        the search, the old post cannot end it as caught-up, and its pages.json state is not advanced."""
        old = {"id": "1740000000000000000", "handle": "someone", "created_at": "2024-01-01T00:00:00Z", "text": "old and popular"}

        def ex():
            return self.logged_in(LEGACY["posts"]) if not self.on_search else \
                dict(self.logged_in([old]), url="https://x.com/search?q=claude+code&f=top")

        def on(args, tabs):
            if args[:2] == ["tab", "new"]:
                self.on_search = "/search" in args[4]
        self.on_search = False
        with mock.patch.object(xs, "cpu_temp", lambda: None):
            rc, calls, caps, pause, sess = self.run_fake_extract(ex, on_act=on, searches=["claude code"])
        self.assertEqual([p["source"] for p in sess[-1]["pages"]], ["home", "search:claude code"])
        self.assertEqual(sess[-1]["pages"][1]["stop"], "wrong-tab")
        self.assertTrue(sess[-1]["outcome"].startswith("error:wrong-tab"))
        self.assertFalse(any(c["source"] == "search:claude code" for c in caps))
        self.assertEqual(self.pages.get("search:claude code", {}), {})


class TabCheck(unittest.TestCase):
    def test_url_matches_planned_source(self):
        m, home, theo = xs.url_matches, {"url": "https://x.com/home"}, {"url": "https://x.com/theo"}
        srch = {"url": "https://x.com/search?q=claude+code&f=live"}
        self.assertTrue(m("https://x.com/home", home))
        self.assertTrue(m("https://x.com/Theo/", theo))
        self.assertTrue(m("https://x.com/search?q=claude%20code&src=typed_query&f=live", srch))
        self.assertTrue(m("https://x.com/i/flow/login?redirect_after_login=%2Fhome", home))   # block_reason judges these
        self.assertTrue(m("https://x.com/account/access", theo))
        for url, page in (("https://x.com/someone", theo), ("https://x.com/theo/status/1", theo), ("https://x.com/home", srch),
                          ("https://x.com/search?q=other&f=live", srch), ("https://example.com/home", home), (None, home)):
            self.assertFalse(m(url, page), url)

    def test_astra2_9_search_feed_params_must_match(self):
        """f selects the feed (no f = Top); f=top, a missing f, or a pf/lf filter is not the planned f=live page."""
        m, srch = xs.url_matches, {"url": "https://x.com/search?q=claude+code&f=live"}
        for url in ("https://x.com/search?q=claude+code&f=top", "https://x.com/search?q=claude+code",
                    "https://x.com/search?q=claude+code&f=live&pf=on", "https://x.com/search?q=claude+code&f=live&lf=on",
                    "https://x.com/search?q=claude+code&f=user"):
            self.assertFalse(m(url, srch), url)
        self.assertTrue(m("https://x.com/search?f=live&src=typed_query&q=claude%20code", srch))
        self.assertTrue(m("https://x.com/search?q=claude+code&f=top", {"url": "https://x.com/search?q=claude+code"}))
        self.assertTrue(m("https://x.com/home", {"url": "https://x.com/home"}))



@unittest.skipUnless(shutil.which("node"), "node not installed")
class ScrollJs(unittest.TestCase):
    """The real page scripts, run under node with location/window/sessionStorage/performance stubbed to the page a tab
    may show."""

    def run_src(self, url, src, mark=None, storage=None, nav="loaded", mark_as=None):
        """src in a page showing url whose document's memory holds mark (window.xScrollTab, set the way mark.js sets
        it; None: no mark), whose tab's sessionStorage `x-scroll-tab` holds storage (None: nothing; "throw": access
        throws) and whose document's navigation entry is nav, (type, name) ("loaded": loaded at url; None: no entry).
        mark_as "enumerable"/"getter" plants the mark some other way than mark.js does. Returns the script's parsed
        result and the in-memory mark afterwards."""
        nav = ("navigate", url) if nav == "loaded" else nav
        stub = ("const u = new URL(process.argv[1]); globalThis.location = u;"
                "const nav = JSON.parse(process.argv[5]); const how = process.argv[6];"
                "globalThis.performance = { getEntriesByType: (t) => (t === 'navigation' && nav ? [{ type: nav[0], name: nav[1] }] : []) };"
                "globalThis.window = { scrollY: 100, scrollBy(x, y) { this.scrollY += y; } };"
                "if (process.argv[3] !== '-') Object.defineProperty(window, 'xScrollTab', how === 'getter' ? "
                "{ get: () => process.argv[3], enumerable: false, configurable: true } : "
                "{ value: process.argv[3], enumerable: how === 'enumerable', writable: false, configurable: true });"
                "const store = new Map(['-', 'throw'].includes(process.argv[4]) ? [] : [['x-scroll-tab', process.argv[4]]]);"
                "const bad = () => { throw new Error('SecurityError'); };"
                "globalThis.sessionStorage = process.argv[4] === 'throw' ? { getItem: bad, setItem: bad } : "
                "{ getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => { store.set(k, String(v)); } };"
                "const r = eval(Buffer.from(process.argv[2], 'base64').toString());"
                "const d = Object.getOwnPropertyDescriptor(window, 'xScrollTab');"
                "console.log(JSON.stringify({ r: JSON.parse(r), mark: d && 'value' in d ? d.value : null }));")
        out = subprocess.run(["node", "-e", stub, url, b64(src), "-" if mark is None else mark,
                              "-" if storage is None else storage, json.dumps(nav), mark_as or "mark.js"],
                             capture_output=True, text=True, timeout=30, check=True).stdout
        d = json.loads(out)
        return d["r"], d["mark"]

    def run_js(self, url, page, px=600, mark=N, nonce=N, storage=None, nav="ours", mark_as=None):
        """scroll.js in a page showing url whose document holds mark, for x-scroll's tab marked with nonce. nav "ours":
        the document is the one x-scroll's tab holding mark was opened with (url at #xs-<mark>; a page without a mark:
        loaded at url). Returns the result, the plan and the navigation entry used."""
        if nav == "ours":
            nav = ("navigate", f"{url.split('#')[0]}#xs-{mark}") if mark else ("navigate", url)
        src = xs.scroll_script(page, px, nonce)
        res, _ = self.run_src(url, src, mark, storage, nav, mark_as)
        return res, scroll_plan_of(["eval", "-b", b64(src)]), nav

    def test_astra3_R1_25_scrolls_only_the_planned_page(self):
        home, theo = {"url": "https://x.com/home"}, {"url": "https://x.com/theo"}
        live = {"url": "https://x.com/search?q=claude+code&f=live"}
        top = {"url": "https://x.com/search?q=claude+code"}
        cases = [("https://x.com/home", home, True), ("https://www.x.com/home/", home, True), ("https://x.com/Theo", theo, True),
                 ("https://x.com/search?f=live&src=typed_query&q=claude%20code", live, True),
                 ("https://x.com/search?q=claude+code&f=top", top, True), ("https://x.com/search?q=claude+code&q=", top, True),
                 (f"https://x.com/home#xs-{N}", home, True),
                 ("https://x.com/someone", theo, False), ("https://x.com/theo/status/1", theo, False),
                 ("https://x.com/i/flow/login", home, False), ("https://x.com/account/access", home, False),
                 ("http://x.com/home", home, False), ("https://example.org/home", home, False),
                 ("https://x.com.evil.example/home", home, False), ("https://x.com/home", live, False),
                 ("https://x.com/search?q=claude+code&f=top", live, False), ("https://x.com/search?q=claude+code", live, False),
                 ("https://x.com/search?q=claude+code&f=live&pf=on", live, False),
                 ("https://x.com/search?q=claude+code&f=live&lf=on", live, False),
                 ("https://x.com/search?q=other&f=live", live, False)]
        for url, page, want in cases:
            res, plan, nav = self.run_js(url, page)
            self.assertEqual(res["scrolled"], want, url)
            self.assertEqual(res["y1"], 700 if want else 100, url)   # scrolled by px only when it is the planned page
            self.assertEqual(page_shows_plan(url, plan, N, nav), want, url)   # the test fake agrees

    def test_astra4_R1_25_scrolls_only_the_tab_holding_this_nonce(self):
        """The planned URL is not enough: the document's memory must hold this plan's nonce. A same-URL page with no
        mark, another tab's (or an earlier tab's) nonce, the nonce only in the URL fragment or only in sessionStorage, or
        a mark not planted the way mark.js plants it (enumerable, or a getter) scrolls nothing."""
        other = "f" * 32
        for url, mark, storage, how, want in (
                ("https://x.com/home", N, None, None, True), ("https://x.com/home", N, "throw", None, True),
                ("https://x.com/home", None, None, None, False), ("https://x.com/home", other, None, None, False),
                (f"https://x.com/home#xs-{N}", None, None, None, False), ("https://x.com/home", "", None, None, False),
                ("https://x.com/home", None, N, None, False), ("https://x.com/home", N, None, "enumerable", False),
                ("https://x.com/home", N, None, "getter", False)):
            res, plan, nav = self.run_js(url, {"url": "https://x.com/home"}, mark=mark, storage=storage, mark_as=how)
            self.assertEqual((res["scrolled"], res["y1"]), (want, 700 if want else 100), (url, mark, storage, how))
            self.assertEqual(page_shows_plan(url, plan, mark if not how else None, nav), want, (url, mark, storage, how))

    def test_astra4_R1_25_mark_js_marks_only_the_page_carrying_this_nonces_fragment(self):
        other = "f" * 32
        mark = xs.call_script("mark", N)
        for url, before, want in ((f"https://x.com/home#xs-{N}", None, N), (f"https://www.x.com/theo?x=1#xs-{N}", None, N),
                                  (f"https://x.com/home#xs-{N}", other, N),      # re-marking our page replaces a stale mark
                                  ("https://x.com/home", None, None),            # the same URL without our fragment
                                  (f"https://x.com/home#xs-{other}", None, None),   # another tab's fragment
                                  (f"https://x.com/home#xs-{N}x", None, None), (f"https://x.com/home#XS-{N}", None, None),
                                  (f"http://x.com/home#xs-{N}", None, None), (f"https://x.com.evil.example/home#xs-{N}", None, None),
                                  (f"https://example.org/home#xs-{N}", None, None),
                                  ("https://x.com/home", other, other), ("https://x.com/home", N, N)):   # no fragment: left as is
            res, after = self.run_src(url, mark, before)
            self.assertEqual(after, want, (url, before))
            self.assertEqual(res["marked"], want == N, (url, before))
        for storage in (N, "throw"):   # sessionStorage is neither read nor needed
            res, after = self.run_src(f"https://x.com/home#xs-{N}", mark, None, storage=storage)
            self.assertEqual((res["marked"], after), (True, N), storage)
            res, after = self.run_src("https://x.com/home", mark, None, storage=storage)
            self.assertEqual((res["marked"], after), (False, None), storage)

    def test_astra4_R1_25_scrolls_only_the_document_our_tab_loaded(self):
        """The mark alone is not the whole check: the document's navigation entry must carry #xs-<nonce> too (still
        there after X's router drops it from the URL). A page loaded at the bare URL, a history traversal, another
        tab's fragment, a page without an entry, and (astra5) a reload are not scrolled."""
        home, other = "https://x.com/home", "f" * 32
        for nav, want in ((("navigate", f"{home}#xs-{N}"), True), (("reload", home), False),
                          (("navigate", f"https://x.com/home?x=1#xs-{N}"), True),   # HTTP redirects keep the fragment
                          (("navigate", home), False), (("navigate", f"{home}#xs-{other}"), False),
                          (("back_forward", home), False), (("navigate", f"{home}#xs-{N}x"), False),
                          (("navigate", ""), False), (None, False)):
            res, plan, _ = self.run_js(home, {"url": home}, nav=nav)
            self.assertEqual((res["scrolled"], res["doc"], res["y1"]), (want, want, 700 if want else 100), nav)
            self.assertEqual(page_shows_plan(home, plan, N, nav or ("navigate", "")), want, nav)

    def test_astra4_R1_25_mark_js_marks_our_document_after_x_drops_the_fragment(self):
        """X's router may replaceState the #xs- fragment away before mark.js runs; the document's navigation entry
        still carries it, so x-scroll's own tab is marked (no false Unmarked). A page loaded at the bare URL, at another
        nonce's fragment, or off x.com is still never marked."""
        mark, other = xs.call_script("mark", N), "f" * 32
        for url, nav, want in (("https://x.com/home", ("navigate", f"https://x.com/home#xs-{N}"), N),
                               ("https://x.com/i/flow/login", ("navigate", f"https://x.com/home#xs-{N}"), N),
                               ("https://x.com/home", ("navigate", "https://x.com/home"), None),
                               ("https://x.com/home", ("reload", "https://x.com/home"), None),
                               ("https://x.com/home", ("navigate", f"https://x.com/home#xs-{other}"), None),
                               ("https://x.com/home", None, None),
                               ("https://example.org/home", ("navigate", f"https://example.org/home#xs-{N}"), None)):
            res, after = self.run_src(url, mark, None, nav=nav)
            self.assertEqual((res["marked"], after), (want == N, want), (url, nav))

    def test_astra4_R1_25_extract_js_reads_nothing_on_an_unmarked_tab(self):
        """On a page without this nonce in memory, extract.js returns only {marked: false, url} before touching the
        document (the node stub has no document: reading it would throw), whatever sessionStorage holds."""
        src = xs.call_script("extract", N)
        for mark, storage, how in ((None, None, None), ("f" * 32, None, None), (None, N, None), (None, "throw", None),
                                   (N, None, "enumerable"), (N, None, "getter")):
            res, _ = self.run_src("https://x.com/home", src, mark, storage, mark_as=how)
            self.assertEqual(res, {"v": 3, "marked": False, "url": "https://x.com/home"}, (mark, storage, how))
        with self.assertRaises(subprocess.CalledProcessError):   # with the mark it goes on to read the (absent) document
            self.run_src("https://x.com/home", src, N)

    def test_astra5_R1_25_foreign_tab_with_an_opener_copied_nonce_is_not_read_or_scrolled_even_after_reload(self):
        """Astra round 5's fixture: a foreign tab opened from x-scroll's tab on the planned URL carries a copy of its
        sessionStorage (the nonce) but not its memory. Before this fix extract.js read it, and after a reload (navigation
        type reload) scroll.js scrolled it from 100 to 700. Now neither reads nor scrolls it, before or after the
        reload, and a reload of x-scroll's own tab is not x-scroll's page any more either (a new document: no mark)."""
        home, extract = "https://x.com/home", xs.call_script("extract", N)
        for nav in (("navigate", home), ("reload", home)):   # opened from ours at the bare URL, then reloaded
            res, _ = self.run_src(home, extract, None, storage=N, nav=nav)
            self.assertEqual(res, {"v": 3, "marked": False, "url": home}, nav)
            res, plan, _ = self.run_js(home, {"url": home}, mark=None, storage=N, nav=nav)
            self.assertEqual((res["scrolled"], res["marked"], res["y0"], res["y1"]), (False, False, 100, 100), nav)
            self.assertFalse(page_shows_plan(home, plan, None, nav), nav)
        # even a copy opened at our full URL (fragment included) and reloaded: no memory, nothing read or scrolled
        res, _ = self.run_src(f"{home}#xs-{N}", extract, None, storage=N, nav=("reload", f"{home}#xs-{N}"))
        self.assertFalse(res["marked"])
        res, _, _ = self.run_js(f"{home}#xs-{N}", {"url": home}, mark=None, storage=N, nav=("reload", f"{home}#xs-{N}"))
        self.assertEqual((res["scrolled"], res["y1"]), (False, 100))
        # x-scroll's own tab after a reload: sessionStorage kept the nonce, the new document has no mark
        res, _, _ = self.run_js(home, {"url": home}, mark=None, storage=N, nav=("reload", home))
        self.assertEqual((res["scrolled"], res["y1"]), (False, 100))
        # and the very same page before the reload, marked by mark.js in memory, is read and scrolled
        res, _, _ = self.run_js(home, {"url": home}, mark=N, storage=N)
        self.assertEqual((res["scrolled"], res["y1"]), (True, 700))


def cdp_pipe_browser(profile):
    """A throwaway headless Chromium driven over --remote-debugging-pipe (stdlib only). Offline: every host name fails
    to resolve and https://x.com/ is served from CDP Fetch with a tall blank page. Returns (call, close)."""
    cmd_r, cmd_w = os.pipe()
    res_r, res_w = os.pipe()

    def fds():   # Chromium reads commands on fd 3 and writes replies on fd 4
        a, b = os.dup(cmd_r), os.dup(res_w)
        os.dup2(a, 3)
        os.dup2(b, 4)
    proc = subprocess.Popen(["chromium", "--headless=new", "--remote-debugging-pipe", f"--user-data-dir={profile}",
                             "--no-first-run", "--disable-background-networking", "--host-resolver-rules=MAP * ~NOTFOUND",
                             "about:blank"], preexec_fn=fds, pass_fds=(3, 4), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    os.close(cmd_r)
    os.close(res_w)
    out, buf, ids = os.fdopen(cmd_w, "wb"), b"", [0]
    page = base64.b64encode(b"<!doctype html><title>t</title><body style='height:20000px;margin:0'>x</body>").decode()

    def send(method, params=None, session=None):
        ids[0] += 1
        out.write(json.dumps({"id": ids[0], "method": method, "params": params or {},
                              **({"sessionId": session} if session else {})}).encode() + b"\0")
        out.flush()
        return ids[0]

    def call(method, params=None, session=None):
        nonlocal buf
        want = send(method, params, session)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            while b"\0" not in buf:
                chunk = os.read(res_r, 65536)
                if not chunk:
                    raise RuntimeError("chromium closed the pipe")
                buf += chunk
            raw, buf = buf.split(b"\0", 1)
            msg = json.loads(raw)
            if msg.get("method") == "Fetch.requestPaused":
                send("Fetch.fulfillRequest", {"requestId": msg["params"]["requestId"], "responseCode": 200, "body": page,
                                              "responseHeaders": [{"name": "Content-Type", "value": "text/html"}]},
                     msg.get("sessionId"))
            elif msg.get("id") == want:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result") or {}
        raise TimeoutError(method)

    def close():
        out.close()
        os.close(res_r)
        proc.terminate()
        proc.wait(15)
        for _ in range(50):   # its helper processes can outlive the browser and still write to the profile being removed
            try:
                os.killpg(proc.pid, signal.SIGKILL)   # its own session's process group only
            except ProcessLookupError:
                break
            time.sleep(0.1)
    # browser-wide as well: a window.open tab requests its URL before a test can attach to it and enable Fetch there
    try:
        call("Fetch.enable", {"patterns": [{"urlPattern": "https://x.com/*"}]})
    except BaseException:
        close()
        raise
    return call, close


class RoutedModel(unittest.TestCase):
    def test_call_claude_takes_model_and_effort_from_the_bulk_sweep_route(self):
        if not xs.LANES.is_file():
            self.skipTest(f"no lanes.json at {xs.LANES} (set ROUTE_LANES or KIT_HOME)")
        table = json.loads(xs.LANES.read_text())
        lane = table["lanes"][table["routes"]["bulk-sweep"]["lane"]]
        seen = []
        def run(cmd, **kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"structured_output": {"a": 1}, "usage": {}}), stderr="")
        with mock.patch.object(xs.subprocess, "run", side_effect=run), mock.patch.object(xs, "log"):
            self.assertEqual(xs.call_claude("posts", {"type": "object"}), {"a": 1})
        cmd = seen[0]
        self.assertEqual(xs.ROUTE, "bulk-sweep")
        self.assertEqual((cmd[cmd.index("--model") + 1], cmd[cmd.index("--effort") + 1]), (lane["model"], lane["effort"]))


@unittest.skipUnless(shutil.which("chromium"), "chromium not installed")
class ScrollJsChromium(unittest.TestCase):
    """The real page scripts, evaluated the way `web act eval` does (Runtime.evaluate) in a real headless Chromium."""

    def tab_helpers(self, call):
        """(load, js, settle, run) over a cdp_pipe_browser: load(url) opens a fresh tab at url (served offline) and
        returns its session; run(session, src) evaluates a page script x-scroll is allowed to send."""
        def load(url):
            tid = call("Target.createTarget", {"url": "about:blank"})["targetId"]
            return navigate(call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"], url)

        def navigate(sid, url):
            call("Fetch.enable", {"patterns": [{"urlPattern": "https://x.com/*"}]}, sid)
            call("Page.navigate", {"url": url}, sid)
            return sid

        def js(sid, expr, **kw):
            return call("Runtime.evaluate", {"expression": expr, "returnByValue": True, **kw}, sid)["result"].get("value")

        def settle(sid, url):
            for _ in range(100):   # wait for the served document to be the one in the tab
                if js(sid, "location.href") == url and js(sid, "document.readyState") == "complete":
                    return
                time.sleep(0.1)
            self.fail(f"{url} never loaded")

        def run(sid, src):
            xs.check_act(["eval", "-b", b64(src)])   # exactly what x-scroll is allowed to send
            return json.loads(js(sid, src))
        load.navigate = navigate
        return load, js, settle, run

    def wait_new_document(self, js, sid, doc):
        for _ in range(100):   # a new document: the navigation happened
            if js(sid, "performance.timeOrigin") != doc:
                return
            time.sleep(0.1)
        self.fail("no new document")

    def popup_from(self, call, js, sid, url):
        """window.open(url) from the page in sid (as a click would); the new tab's target id."""
        before = {t["targetId"] for t in call("Target.getTargets")["targetInfos"]}
        js(sid, f"window.open({json.dumps(url)}, '_blank'); 1", userGesture=True)
        for _ in range(50):
            popup = next((t for t in call("Target.getTargets")["targetInfos"]
                          if t["targetId"] not in before and t["type"] == "page"), None)
            if popup:
                return popup["targetId"]
            time.sleep(0.1)
        self.fail("window.open made no tab")

    def test_astra4_R1_25_nonce_mark_identifies_our_tab_across_in_app_navigation_not_reload(self):
        """x-scroll's tab (opened at x.com/home#xs-<nonce>) is marked by mark.js and then read and scrolled; another
        tab on the same URL opened at the same moment, and one opened at our full URL (fragment copied) after the mark,
        are never marked, read or scrolled. The mark lives in the page's memory (non-enumerable window.xScrollTab): it
        survives X-style in-app navigation (pushState/replaceState, which also drops the fragment) but not a reload
        (astra5: a reload is no evidence of ownership), after which mark.js does not re-mark the page either."""
        with tempfile.TemporaryDirectory() as prof:
            call, close = cdp_pipe_browser(prof)
            try:
                load, js, settle, run = self.tab_helpers(call)
                home, ours_url = "https://x.com/home", f"https://x.com/home#xs-{N}"
                scroll = lambda page="https://x.com/home": xs.scroll_script({"url": page}, 600, N)
                extract, mark = xs.call_script("extract", N), xs.call_script("mark", N)
                has_mark = "Object.getOwnPropertyDescriptor(window, 'xScrollTab') !== undefined"
                foreign, ours = load(home), load(ours_url)   # opened at the same moment
                settle(foreign, home)
                settle(ours, ours_url)
                self.assertFalse(run(foreign, mark)["marked"])   # the eval landing on the foreign tab marks nothing
                self.assertTrue(run(ours, mark)["marked"])
                self.assertFalse(js(foreign, has_mark))
                self.assertTrue(js(ours, has_mark))
                self.assertFalse(js(ours, "Object.keys(window).includes('xScrollTab')"))   # non-enumerable
                self.assertIsNone(js(ours, "sessionStorage.getItem('x-scroll-tab')"))      # nothing in tab storage
                res = run(foreign, scroll())
                self.assertEqual((res["scrolled"], res["y0"], res["y1"]), (False, 0, 0), res)
                self.assertEqual(run(foreign, extract), {"v": 3, "marked": False, "url": home})
                res = run(ours, scroll())
                self.assertEqual((res["scrolled"], res["y0"], res["y1"]), (True, 0, 600), res)
                res = run(ours, extract)
                self.assertEqual((res["marked"], res["posts"]), (True, []), res)
                copied = load(ours_url)   # another agent opens our tab's URL, fragment and all, after the mark
                settle(copied, ours_url)
                self.assertFalse(run(copied, scroll())["scrolled"])
                self.assertFalse(run(copied, extract)["marked"])
                js(ours, "history.pushState({}, '', '/explore'); history.pushState({}, '', '/home'); "
                         "history.replaceState({}, '', '/home')")   # in-app navigation; the fragment is gone
                self.assertEqual(js(ours, "location.href"), home)
                self.assertTrue(run(ours, scroll())["scrolled"])
                doc = js(ours, "performance.timeOrigin")
                call("Page.reload", {}, ours)
                self.wait_new_document(js, ours, doc)
                settle(ours, home)
                self.assertEqual(js(ours, "performance.getEntriesByType('navigation')[0].type"), "reload")
                res = run(ours, scroll())   # (the reload restores the scroll position; it must not move)
                self.assertEqual((res["scrolled"], res["marked"], res["y1"]), (False, False, res["y0"]), res)
                self.assertFalse(run(ours, extract)["marked"])
                self.assertFalse(run(ours, mark)["marked"])            # and it is not re-marked after the fact
                self.assertFalse(run(foreign, scroll())["scrolled"])   # and the foreign tab still has none
            finally:
                close()

    def test_astra4_R1_25_page_another_agent_loads_into_our_tab_or_a_window_opened_from_it_is_not_scrolled(self):
        """Reviewer variants (round 4). (1) another agent's `web act open https://x.com/home` while x-scroll's tab is the
        active one loads its page into x-scroll's tab; (2) a window opened from x-scroll's tab on the same URL. Both are
        new documents without x-scroll's in-memory mark, so neither is read or scrolled. X's in-app route to a lock page
        (same document) keeps the mark and is read for block_reason; a full page load to it is unmarked (xscroll.py then
        judges it by URL alone, unmarked_block). mark.js also marks x-scroll's document when X's router has already
        dropped the #xs- fragment from the URL."""
        with tempfile.TemporaryDirectory() as prof:
            call, close = cdp_pipe_browser(prof)
            try:
                load, js, settle, run = self.tab_helpers(call)
                home, ours_url = "https://x.com/home", f"https://x.com/home#xs-{N}"
                scroll, extract = xs.scroll_script({"url": home}, 600, N), xs.call_script("extract", N)
                ours = load(ours_url)
                settle(ours, ours_url)
                js(ours, "history.replaceState({}, '', '/home')")   # X's router drops the fragment before the mark runs
                self.assertEqual(js(ours, "location.href"), home)
                self.assertTrue(run(ours, xs.call_script("mark", N))["marked"])
                self.assertTrue(run(ours, scroll)["scrolled"])
                # (2) a window opened from x-scroll's tab, at the same URL
                copy_sid = load.navigate(call("Target.attachToTarget", {"targetId": self.popup_from(call, js, ours, "about:blank"),
                                                                        "flatten": True})["sessionId"], home)
                settle(copy_sid, home)
                res = run(copy_sid, scroll)
                self.assertEqual((res["scrolled"], res["marked"], res["y1"]), (False, False, 0), res)
                self.assertFalse(run(copy_sid, extract)["marked"])
                # (1) another agent's `web act open` of the same URL in x-scroll's own (active) tab: a new document
                doc = js(ours, "performance.timeOrigin")
                call("Page.navigate", {"url": home}, ours)
                self.wait_new_document(js, ours, doc)
                settle(ours, home)
                res = run(ours, scroll)
                self.assertEqual((res["scrolled"], res["marked"], res["doc"], res["y1"]), (False, False, False, 0), res)
                self.assertFalse(run(ours, extract)["marked"])
                # a fresh tab of ours: X routes it in-app to a lock page (same document): still read, never scrolled
                ours = load(ours_url)
                settle(ours, ours_url)
                self.assertTrue(run(ours, xs.call_script("mark", N))["marked"])
                js(ours, "history.pushState({}, '', '/account/access')")
                self.assertTrue(run(ours, extract)["marked"])
                self.assertFalse(run(ours, scroll)["scrolled"])
                # X sends it to the lock page with a full page load (a new document): unmarked, judged by URL in xscroll.py
                js(ours, "setTimeout(() => { location.href = '/account/access'; }, 10); 1")
                doc = js(ours, "performance.timeOrigin")
                self.wait_new_document(js, ours, doc)
                settle(ours, "https://x.com/account/access")
                res = run(ours, extract)
                self.assertEqual(res, {"v": 3, "marked": False, "url": "https://x.com/account/access"})
                self.assertEqual(xs.unmarked_block(res["url"]), "locked")
            finally:
                close()

    def test_astra5_R1_25_foreign_tab_opened_from_ours_with_a_copied_nonce_is_not_read_or_scrolled_after_reload(self):
        """Astra round 5, in a real browser: a tab opened from x-scroll's (window.open, as a click would) gets a copy of
        its sessionStorage, here holding the nonce the way earlier versions stored it, and after a reload earlier
        versions read and scrolled it (100 to 700). It gets no copy of the page's memory, so it is never read or
        scrolled, before or after a reload, at the bare URL or at our full #xs- URL; x-scroll's own tab still is."""
        with tempfile.TemporaryDirectory() as prof:
            call, close = cdp_pipe_browser(prof)
            try:
                load, js, settle, run = self.tab_helpers(call)
                home, ours_url = "https://x.com/home", f"https://x.com/home#xs-{N}"
                scroll, extract = xs.scroll_script({"url": home}, 600, N), xs.call_script("extract", N)
                ours = load(ours_url)
                settle(ours, ours_url)
                self.assertTrue(run(ours, xs.call_script("mark", N))["marked"])
                js(ours, f"sessionStorage.setItem('x-scroll-tab', {json.dumps(N)}); 1")   # the old store, to be copied
                for url in (home, ours_url):
                    sid = call("Target.attachToTarget", {"targetId": self.popup_from(call, js, ours, url),
                                                         "flatten": True})["sessionId"]
                    call("Fetch.enable", {"patterns": [{"urlPattern": "https://x.com/*"}]}, sid)
                    settle(sid, url)
                    self.assertTrue(js(sid, "window.opener !== null"), url)
                    self.assertEqual(js(sid, "sessionStorage.getItem('x-scroll-tab')"), N)   # the opener-copied nonce
                    js(sid, "window.scrollTo(0, 100); 1")
                    for step in ("opened", "reloaded"):
                        if step == "reloaded":
                            doc = js(sid, "performance.timeOrigin")
                            call("Page.reload", {}, sid)
                            self.wait_new_document(js, sid, doc)
                            settle(sid, url)
                            self.assertEqual(js(sid, "performance.getEntriesByType('navigation')[0].type"), "reload")
                            js(sid, "window.scrollTo(0, 100); 1")
                        self.assertEqual(run(sid, extract), {"v": 3, "marked": False, "url": url}, (url, step))
                        res = run(sid, scroll)
                        self.assertEqual((res["scrolled"], res["marked"], res["y0"], res["y1"]), (False, False, 100, 100),
                                         (url, step, res))
                res = run(ours, scroll)
                self.assertEqual((res["scrolled"], res["y1"]), (True, 600), res)
            finally:
                close()

    def test_astra5_R1_25_blank_window_opened_from_ours_then_sent_to_our_full_url_is_not_read_or_scrolled(self):
        """Reviewer variant (round 5): x-scroll's marked page opens an empty window (window.open with no URL: its
        initial about:blank shares x.com's origin and opener, the case where browsers may carry the window object over
        to the first same-origin page) and then sends it to x-scroll's full URL, #xs-<nonce> included, so its navigation
        entry carries the fragment (scroll.js's doc check passes). It still has no in-memory mark: it is not read or
        scrolled, before or after a reload, and mark.js was never run there. x-scroll's own tab still is."""
        with tempfile.TemporaryDirectory() as prof:
            call, close = cdp_pipe_browser(prof)
            try:
                load, js, settle, run = self.tab_helpers(call)
                home, ours_url = "https://x.com/home", f"https://x.com/home#xs-{N}"
                scroll, extract = xs.scroll_script({"url": home}, 600, N), xs.call_script("extract", N)
                ours = load(ours_url)
                settle(ours, ours_url)
                self.assertTrue(run(ours, xs.call_script("mark", N))["marked"])
                before = {t["targetId"] for t in call("Target.getTargets")["targetInfos"]}
                js(ours, "window.__xsw = window.open('', '_blank'); 1", userGesture=True)
                tid = None
                for _ in range(50):
                    tid = next((t["targetId"] for t in call("Target.getTargets")["targetInfos"]
                                if t["targetId"] not in before and t["type"] == "page"), None)
                    if tid:
                        break
                    time.sleep(0.1)
                self.assertIsNotNone(tid, "window.open made no tab")
                sid = call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
                call("Fetch.enable", {"patterns": [{"urlPattern": "https://x.com/*"}]}, sid)
                self.assertEqual(js(sid, "location.href"), "about:blank")
                self.assertFalse(run(sid, xs.call_script("mark", N))["marked"])   # about:blank is never marked
                js(ours, f"window.__xsw.location.href = {json.dumps(ours_url)}; 1")
                settle(sid, ours_url)
                self.assertTrue(js(sid, "window.opener !== null"))
                js(sid, "window.scrollTo(0, 100); 1")
                for step in ("opened", "reloaded"):
                    if step == "reloaded":
                        doc = js(sid, "performance.timeOrigin")
                        call("Page.reload", {}, sid)
                        self.wait_new_document(js, sid, doc)
                        settle(sid, ours_url)
                        js(sid, "window.scrollTo(0, 100); 1")
                    self.assertEqual(run(sid, extract), {"v": 3, "marked": False, "url": ours_url}, step)
                    res = run(sid, scroll)
                    self.assertEqual((res["scrolled"], res["marked"], res["doc"], res["y0"], res["y1"]),
                                     (False, False, True, 100, 100), (step, res))
                res = run(ours, scroll)
                self.assertEqual((res["scrolled"], res["y1"]), (True, 600), res)
            finally:
                close()


if __name__ == "__main__":
    unittest.main()
