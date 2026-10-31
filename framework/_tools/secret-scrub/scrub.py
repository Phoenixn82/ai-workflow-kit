#!/usr/bin/env python3
"""Find and overwrite passwords in agent transcripts / notes. Values never leave STATE (0700); `show` prints masked contexts only.

  scrub.py find  ROOT...   scan -> STATE/candidates.jsonl
  scrub.py show  [START N] masked contexts for triage (no values)
  scrub.py apply ROOT...   overwrite every value in STATE/confirmed.json with a same-length filler, in place
                           (no size/inode change, mtime kept -> live appenders and byte-offset indexes stay valid)
  scrub.py sqlite DB...    same, inside text columns of SQLite DBs
  scrub.py check ROOT...   count files still containing a confirmed value (expect 0)
confirmed.json = ["value", ...]; build it from `show` ids with: scrub.py confirm ID [ID...]
"""
import collections, hashlib, json, os, re, subprocess, sys

STATE = os.environ.get("SCRUB_STATE") or os.path.expanduser("~/.local/share/secret-scrub")
os.makedirs(STATE, mode=0o700, exist_ok=True)
CAND, CONF = f"{STATE}/candidates.jsonl", f"{STATE}/confirmed.json"

KW = r"(?:pass(?:word|wd|phrase|code)?s?|pwd|pin|secret|security (?:answer|question)s?|answers?|credentials?|creds?|login|admin)"
TOK = r"(?:[^\s\"'`,;)\\\]\[<>{}|]|\\\\|\\\")" + "{4,64}"
SEP = r"(?:\s*[:=]\s*|\s+(?:is|are|was|were|set to|=>)\s+|\s*(?:→|->|=>)\s*)"
PATS = {
    "kw":   rf"(?i)(?:\b|_){KW}\b[^\n\"']{{0,25}}?{SEP}[\"'`(]?{TOK}(?:\s*[,/&]\s*{TOK}){{0,3}}",
    "kwq":  rf"(?i)(?:\b|_){KW}\b[^\n\"']{{0,25}}?\s+[\"'`]{TOK}[\"'`]",
    "url":  r"[a-z][a-z0-9+.-]*://[^\s/:@\\\"']{1,40}:[^\s/@\\\"']{4,64}@[^\s/\\\"']{3,80}",
    "cli":  r"(?i)(?:sshpass\s+-p|--pass(?:word)?[= ]|mysql\s+-p|chpasswd)[^\n]{0,80}",
    "pair": rf"(?i)\b(?:credentials?|creds?|logins?|user(?:name)?)\b[^\n\"']{{0,20}}?[\s:=]\s*[\"']?[A-Za-z0-9_.@-]{{2,32}}/{TOK}",
}
VAL = {
    "kw":   re.compile(rf"(?i)(?:\b|_){KW}\b[^\n\"']{{0,25}}?{SEP}[\"'`(]?({TOK}(?:\s*[,/&]\s*{TOK}){{0,3}})"),
    "kwq":  re.compile(rf"(?i)(?:\b|_){KW}\b[^\n\"']{{0,25}}?\s+[\"'`]({TOK})[\"'`]"),
    "url":  re.compile(r"://[^\s/:@\\\"']{1,40}:([^\s/@\\\"']{4,64})@"),
    "cli":  re.compile(r"(?i)(?:sshpass\s+-p|--pass(?:word)?[= ]|mysql\s+-p)\s*[\"']?([^\s\"'\\]{3,64})|chpasswd[^\n]{0,20}?\w+:([^\s\"'|\\]{4,64})"),
    "pair": re.compile(rf"/({TOK})"),
}
PLACE = re.compile(r"(?i)redact|your[_-]|<|\$|\bxxx|\*\*\*|^none|^null$|^true$|^false$|example|placeholder|changeme|^password$|^secret$|hunter2|^\.\.\.|^\{|^%|\bpassword\b|^env|_env$|^(?:input|type|name|value|field|string|str|text|hash|token)s?$")
HEX = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.I)
WORDS = set()
for wf in ("/usr/share/dict/words", "/usr/share/dict/cracklib-small"):
    if os.path.exists(wf):
        WORDS = {w.strip().lower() for w in open(wf, errors="ignore")}; break
RG = ["rg", "-uu", "--no-messages", "--max-filesize", "1G", "-g", "!node_modules", "-g", "!*.png", "-g", "!*.jpg", "-g", "!plugins/**",
      "-g", "!*.lock", "-g", "!.git/**", "-g", "!*.sqlite*", "-g", "!*.db"]


def classes(v):
    return "".join(c for c, r in (("l", "[a-z]"), ("u", "[A-Z]"), ("d", "[0-9]"), ("s", r"[^A-Za-z0-9]")) if re.search(r, v))


def junk(v):
    if len(v) < 5 or len(v) > 64 or PLACE.search(v) or HEX.match(v): return True
    if v.startswith(("/", "./", "~", "http", "-", "@", "#", "(", ")")) or re.search(r"[(){}\[\]=;]", v): return True
    if v.lower().rstrip(".,:!?") in WORDS: return True
    if re.fullmatch(r"[a-z]+(?:_[a-z0-9]+)+|[A-Z]+(?:_[A-Z0-9]+)+|[a-z]+(?:\.[a-z]+)+", v): return True
    return bool(re.fullmatch(r"[a-z]+[A-Z][A-Za-z]+", v))


def add(cands, v, kind, path, ctx):
    h = hashlib.sha256(v.encode()).hexdigest()[:12]
    c = cands.setdefault(h, {"id": h, "value": v, "len": len(v), "classes": classes(v), "kinds": set(), "files": collections.Counter(), "ctx": []})
    c["kinds"].add(kind); c["files"][path] += 1
    ctx = re.sub(r"\s+", " ", ctx)
    if len(c["ctx"]) < 3 and ctx not in c["ctx"]: c["ctx"].append(ctx)


def window(path, off, n, before=110, after=70):
    try:
        with open(path, "rb") as f:
            f.seek(max(0, off - before)); return f.read(before + n + after).decode("utf-8", "replace")
    except OSError:
        return ""


def find_repo(cands, repo):
    """Full history of a bare mirror: every added/removed line, keyword patterns only."""
    git = subprocess.Popen(["git", "--git-dir", repo, "log", "--all", "-p", "-U0", "--no-color", "--no-ext-diff"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    args = ["rg", "-a", "-o", "--no-line-number", "--no-filename"] + [x for p in PATS.values() for x in ("-e", ".{0,90}" + p + ".{0,50}")]
    for raw in subprocess.Popen(args, stdin=git.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout:
        snip = raw.decode("utf-8", "replace").rstrip("\n")
        for kind, rx in VAL.items():
            for g in (x for mm in rx.finditer(snip) for x in mm.groups() if x):
                for v in re.split(r"\s*[,/&]\s*", g):
                    v = v.strip("\"'`.,:;")
                    if not junk(v): add(cands, v, "repo-" + kind, os.path.basename(repo), snip)


def find(roots):
    cands = {}
    for r in [r for r in roots if r.endswith(".git")]: find_repo(cands, r)
    roots = [r for r in roots if not r.endswith(".git")]
    args = RG + ["-o", "-H", "-b", "--no-line-number"] + [x for p in PATS.values() for x in ("-e", p)] + roots
    for raw in (subprocess.Popen(args, stdout=subprocess.PIPE).stdout if roots else []):
        m = re.match(r"^(.*?):(\d+):(.*)$", raw.decode("utf-8", "replace").rstrip("\n"))
        if not m: continue
        path, off, snip = m.group(1), int(m.group(2)), m.group(3)
        for kind, rx in VAL.items():
            for g in (x for mm in rx.finditer(snip) for x in mm.groups() if x):
                for v in re.split(r"\s*[,/&]\s*", g):
                    if "\\" in v:
                        try: v = json.loads('"' + v + '"')
                        except ValueError: pass
                    v = v.strip("\"'`.,:;")
                    if not junk(v): add(cands, v, kind, path, window(path, off, len(snip.encode())))
    if roots: bare(cands, roots)
    with open(os.environ.get("SCRUB_OUT", CAND), "w") as f:
        for c in cands.values():
            c["kinds"] = sorted(c["kinds"]); c["files"] = dict(c["files"].most_common(6)); f.write(json.dumps(c, ensure_ascii=False) + "\n")
    os.chmod(CAND, 0o600)
    print(f"candidates={len(cands)} -> {CAND}")


def bare(cands, roots):
    """A user message (or a credential-file read) that is *only* a password-shaped token."""
    pat = r'"(?:content|text|display|output|message|stdout)":"[^"\s\\]{6,40}(?:\\n)?"'
    PW = re.compile(r"(?i)pass(?:word|wd|code|phrase)?|credential|\bcreds?\b|\blogin\b|\bpin\b|sign[ -]?in|secret|unlock|keyring|ssh|sudo")
    TXT = re.compile(r'"(?:text|content|command|cmd|arguments|display|output|message)":"((?:[^"\\]|\\.){1,3000})"')
    hits = collections.defaultdict(dict)
    args = RG + ["-n", "-o", "-H", "-g", "*.jsonl", "-e", pat] + roots
    for raw in subprocess.Popen(args, stdout=subprocess.PIPE).stdout:
        m = re.match(r"^(.*?):(\d+):\"\w+\":\"(.+?)(?:\\n)?\"$", raw.decode("utf-8", "replace").rstrip("\n"))
        if not m: continue
        v = m.group(3)
        c = classes(v)
        if junk(v) or not ("d" in c or "s" in c or ("l" in c and "u" in c and len(v) >= 8)): continue
        hits[m.group(1)][int(m.group(2))] = v
    for path, want in hits.items():
        prev, hist = [], path.endswith("history.jsonl")
        with open(path, "rb") as f:
            for i, rawl in enumerate(f, 1):
                line = rawl.decode("utf-8", "replace")
                if i in want:
                    kind = "user" if hist or '"role":"user"' in line or '"type":"user"' in line or "user_message" in line else \
                           "tool" if '"tool_result"' in line or "function_call_output" in line else "other"
                    ptxt = " ".join(prev[-3:])
                    if hist or (kind in ("user", "tool") and PW.search(ptxt)):
                        add(cands, want[i], "bare-" + kind, path, "[bare %s message ⟦V⟧; previous: …%s]" % (kind, ptxt[-300:]))
                    prev.append("")
                else:
                    prev.append(" ".join(t.replace("\\n", " ") for t in TXT.findall(line))[-700:])
                if len(prev) > 4: prev.pop(0)


def load():
    return [json.loads(l) for l in open(CAND)]


def masked(c, all_vals):
    out = []
    for x in c["ctx"]:
        for f in sorted(forms(c["value"]), key=len, reverse=True): x = x.replace(f.decode(), "⟦V⟧")
        for v in all_vals:
            for f in forms(v):
                if f.decode() in x: x = x.replace(f.decode(), "⟦other⟧")
        out.append(x[:300])
    return out


def show(start=0, n=40):
    rows = sorted(load(), key=lambda c: (-len(c["kinds"]), -len(c["files"]), c["id"]))
    vals = sorted({c["value"] for c in rows}, key=len, reverse=True)
    print(f"{len(rows)} candidates; showing {start}..{start+n}")
    for c in rows[start:start + n]:
        print(f"{c['id']} len={c['len']} {c['classes']} {','.join(c['kinds'])} files={len(c['files'])} | {' || '.join(masked(c, vals))[:420]}")


def confirm(ids):
    import glob
    rows = {}
    for f in [CAND] + glob.glob(f"{STATE}/hunt*.jsonl"):
        for l in open(f): c = json.loads(l); rows[c["id"]] = c["value"]
    cur = json.load(open(CONF)) if os.path.exists(CONF) else []
    cur = sorted(set(cur) | {rows[i] for i in ids if i in rows})
    json.dump(cur, open(CONF, "w")); os.chmod(CONF, 0o600)
    print(f"confirmed values: {len(cur)}")


def filler(n):
    return ("[REDACTED]" + "*" * (n - 10))[:n] if n >= 10 else "#" * n


def forms(v):
    return {f for f in {v.encode(), json.dumps(v)[1:-1].encode(), json.dumps(v, ensure_ascii=False)[1:-1].encode()} if len(f) >= 6}


DIGITCTX = lambda v: rb"(?i)((?:pass|pwd|code|pin)[^\n]{0,100}?)(" + re.escape(v.encode()) + rb")"


def scan(roots):
    values = json.load(open(CONF))
    plain = [v for v in values if not v.isdigit()]
    digits = [v for v in values if v.isdigit()]
    allf = sorted({f for v in plain for f in forms(v)}, key=len, reverse=True)
    pf = f"{STATE}/.patterns"
    with open(pf, "wb") as t:
        for f in allf: t.write(f + b"\n")
    os.chmod(pf, 0o600)
    base = RG[:3] + ["-a", "-l", "-g", "!*.sqlite*", "-g", "!.git/**"]
    files = set(p for p in subprocess.run(base + ["-F", "-f", pf] + roots, capture_output=True, text=True).stdout.split("\n") if p)
    for v in digits:
        files |= set(p for p in subprocess.run(base + ["-e", DIGITCTX(v).decode()] + roots, capture_output=True, text=True).stdout.split("\n") if p)
    return allf, sorted(files), digits


def apply(roots):
    allf, files, digits = scan(roots)
    dry = bool(os.environ.get("SCRUB_DRY"))
    changed = reps = 0
    for p in files:
        try: st = os.stat(p); data = open(p, "rb").read()
        except OSError: continue
        edits = [(i, len(f)) for f in allf for i in (m.start() for m in re.finditer(re.escape(f), data))]
        for v in digits: edits += [(m.start(2), len(v)) for m in re.finditer(DIGITCTX(v), data)]
        merged = []
        for s_, n in sorted(edits):
            if not merged or s_ >= merged[-1][0] + merged[-1][1]: merged.append((s_, n))
        if not merged: continue
        changed += 1; reps += len(merged)
        if dry: continue
        with open(p, "r+b") as fh:
            for s_, n in merged: fh.seek(s_); fh.write(filler(n).encode())
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    print(f"files_changed={changed} replacements={reps} dry_run={dry}")


def apply_sqlite(dbs):
    """Same-length REPLACE inside text columns of live SQLite DBs (Codex keeps a copy of every thread there)."""
    import sqlite3
    forms_ = sorted({f.decode() for v in json.load(open(CONF)) for f in forms(v)}, key=len, reverse=True)
    for db in dbs:
        c = sqlite3.connect(db, timeout=60); c.execute("pragma secure_delete=on"); n = 0
        for (t,) in c.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' and name!='_sqlx_migrations'").fetchall():
            for _, col, typ, *_ in c.execute(f'pragma table_info("{t}")').fetchall():
                if "CHAR" in typ.upper() or typ.upper() in ("TEXT", "") or "JSON" in typ.upper():
                    for f in forms_:
                        n += c.execute(f'update "{t}" set "{col}"=replace("{col}",?,?) where instr("{col}",?)>0', (f, filler(len(f)), f)).rowcount
        c.commit()
        try: c.execute("pragma wal_checkpoint(truncate)"); c.execute("vacuum")
        except sqlite3.Error as e: print("  checkpoint/vacuum skipped:", e)
        print(f"{os.path.basename(db)}: {n} cell(s) rewritten")


def hunt(rx, roots):
    """Targeted: REGEX with ONE capture group = the value. Adds candidates (kind 'hunt') to STATE/hunt.jsonl; prints only shapes."""
    cands = {}
    args = RG + ["-o", "-H", "-b", "--no-line-number", "-e", rx] + roots
    cre = re.compile(rx)
    for raw in subprocess.Popen(args, stdout=subprocess.PIPE).stdout:
        m = re.match(r"^(.*?):(\d+):(.*)$", raw.decode("utf-8", "replace").rstrip("\n"))
        if not m: continue
        mm = cre.search(m.group(3))
        for v in (mm.groups() if mm else ()):
            if not v: continue
            if "\\" in v:
                try: v = json.loads('"' + v + '"')
                except ValueError: pass
            add(cands, v.strip("\"'`.,:;)"), "hunt", m.group(1), window(m.group(1), int(m.group(2)), len(m.group(3).encode())))
    out = f"{STATE}/hunt.jsonl"
    with open(out, "w") as f:
        for c in cands.values():
            c["kinds"] = sorted(c["kinds"]); c["files"] = dict(c["files"].most_common(6)); f.write(json.dumps(c, ensure_ascii=False) + "\n")
    os.chmod(out, 0o600)
    shp = lambda v: re.sub(r"[a-z]", "a", re.sub(r"[A-Z]", "A", re.sub(r"\d", "9", v)))
    print(f"{len(cands)} hunted values -> {out}")
    for c in sorted(cands.values(), key=lambda c: -len(c["files"]))[:40]: print(c["id"], shp(c["value"]), "files=%d" % len(c["files"]))


if __name__ == "__main__":
    cmd, rest = sys.argv[1], sys.argv[2:]
    {"find": lambda: find(rest), "show": lambda: show(*map(int, rest)), "confirm": lambda: confirm(rest),
     "apply": lambda: apply(rest), "hunt": lambda: hunt(rest[0], rest[1:]), "sqlite": lambda: apply_sqlite(rest), "check": lambda: print(f"files_still_containing_a_confirmed_value={len(scan(rest)[1])}")}[cmd]()
