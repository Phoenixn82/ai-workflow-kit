#!/usr/bin/env python3
"""
vault-rag: hybrid retrieval over the user's second-brain vault, so agents fetch
notes by query at prompt time instead of reading an index of titles.

  - Embeddings: Ollama nomic-embed-text (768-dim) on heimdall (always on, CPU-only Docker,
    _tools/vault-rag/heimdall/; same weights blob as thor's, so vectors are identical), with
    nomic's search_document:/search_query: task prefixes. The host is EMBED_URL in
    ~/.config/ai-workflow-kit/hosts.env (KIT_HOSTS_ENV overrides the path); VAULT_RAG_OLLAMA_HOST overrides both.
    No host configured: queries answer FTS-only and `index` aborts with "set EMBED_URL".
  - Store: sqlite-vec vec0 (cosine) + FTS5/BM25 (porter), fused with
    vector-favored Reciprocal Rank Fusion; recency/importance is only a <5%
    tie-breaker (mtime is a touch signal, not factual freshness).
  - Incremental: content-hashed chunks. Everything is embedded and validated
    before any row changes, and counts are checked before commit (fail-closed).
    `index --rebuild` drops and recreates all three tables (this is what
    migrates the old L2 vec table to cosine). Embedded batches are also appended
    to embed-cache.jsonl beside the DB, so a run that is killed or times out
    resumes where it stopped (a full rebuild on a small CPU takes hours).
  - One embedding model per index: the fingerprint (weights blob digest from /api/show, model, dim, prefixes) is
    stored in the DB's meta table and on line 1 of embed-cache.jsonl. A cache from another fingerprint is discarded,
    an incremental run against a different one aborts (needs --rebuild), and a server that reports no digest gets
    no vector writes (queries still fall back to FTS). Every embedded batch is sealed in the cache by a fresh
    fingerprint check; a resume reuses only sealed vectors (a run killed after a mid-run model switch leaves its
    unsealed tail, which is dropped). The commit re-reads the stored fingerprint, so a concurrent --rebuild with
    another model makes this run abort instead of mixing.
  - Queries never wait on a down host: TCP probe of :11434 (300 ms), else FTS-only.
  - Each hit carries `strong` (cosine sim, or query-term coverage in FTS-only
    mode) so the prompt hook can stay silent when nothing is relevant.

Augments grep; it does not replace it.

Usage:
  vault_rag.py index [--rebuild] [--dry-run]
  vault_rag.py query "<q>" [-k N] [--json] [--fts-only]   (~/.local/bin/vault-recall)
  vault_rag.py stats
  vault_rag.py --self-test
"""
from __future__ import annotations
import argparse, hashlib, json, math, os, re, socket, sqlite3, sys, time, urllib.parse
from pathlib import Path

# ---- config ---------------------------------------------------------------
_AIP = Path(os.environ.get("AI_PROJECTS_ROOT", __import__('os').path.expanduser("~/AI_Projects")))
VAULT = _AIP / "_system" / "second-brain"
EXCLUDE_DIRS = {"log-archive", "Blog", "__pycache__"}  # plus every hidden path (.git .obsidian .sync ...)
EXCLUDE_FILES = {"log.md", "_archive/log.md"}  # vault-relative: the live and the rotated raw vault log
# vault AGENTS.md: the archive tier (_archive/, Projects/<p>/_archive/) stays searchable. Other _archive dirs
# (Actions/, harness-memory/) hold superseded rules, kept out so the prompt hook never injects them.
def archived_ok(parts: tuple) -> bool:
    i = parts.index("_archive")
    return i == 0 or (parts[0] == "Projects" and i <= 2)
# harness memory lives inside the vault (harness-memory/, symlinked from every
# project), so it is indexed once as part of VAULT. Rule files sit outside it.
RULE_FILES = [Path.home() / ".claude" / "CLAUDE.md", _AIP / "CLAUDE.md", _AIP / "AGENTS.md"]
DB_PATH = Path.home() / ".local" / "state" / "vault-rag" / "vault_index.db"
KIT_HOSTS = Path(os.environ.get("KIT_HOSTS_ENV") or Path.home() / ".config" / "ai-workflow-kit" / "hosts.env")
def hosts_env(key: str) -> str:
    """KEY from the environment, else from the kit's hosts.env (KEY=value lines, last one wins); "" when missing."""
    if os.environ.get(key):
        return os.environ[key]
    try:
        vals = [v.strip().strip("'\"") for k, _, v in (l.partition("=") for l in KIT_HOSTS.read_text().splitlines())
                if k.strip() == key]
    except OSError:
        return ""
    return vals[-1] if vals else ""
NO_HOST = f"no embed host: set EMBED_URL in {KIT_HOSTS} (or VAULT_RAG_OLLAMA_HOST)"
# Own variable, not OLLAMA_HOST: ~/.bashrc and ~/.claude/settings.json export OLLAMA_HOST=thor for the big models,
# and the hook inherits it. Rollback to thor: EMBED_URL=<thor's Ollama URL> in hosts.env (reindex.sh then runs thor-up).
def _ollama_host(raw: str | None) -> str:
    """Empty (or blank) counts as unset, as in reindex.sh's ${VAULT_RAG_OLLAMA_HOST:-}: "" (no host), not "http://".
    No scheme means Ollama's OLLAMA_HOST rule, http on :11434 ("thor", which reindex.sh wakes thor for, used to
    send requests to thor:80 while ollama_up() probed :11434)."""
    host = (raw or "").strip().rstrip("/")
    if not host or "://" in host:
        return host
    return "http://" + host + ("" if re.search(r":\d+$", host) else ":11434")
OLLAMA = _ollama_host(os.environ.get("VAULT_RAG_OLLAMA_HOST") or hosts_env("EMBED_URL"))
EMBED_MODEL, DIM = "nomic-embed-text", 768
DOC_PREFIX, QUERY_PREFIX = "search_document: ", "search_query: "
# The weights blob that built every index before fingerprints existed (heimdall's and thor's nomic-embed-text, the same
# blob): an index with no stored fingerprint adopts it once, without a re-embed, only if the server reports exactly this.
LEGACY_WEIGHTS = "sha256:970aa74c0a90ef7482477cf803618e776e173c007bf957f635f1015bfcfef0e6"
MAX_SECTION, WINDOW = 2000, 1500  # chars; longer sections get windowed (markdown-aware chunking deferred)
RRF_K, W_VEC, W_LEX = 60, 1.0, 0.6
CANDIDATES, BATCH = 40, 2  # BATCH 2 (was 24): heimdall's Ollama (NUM_PARALLEL 4) then always has a free slot for a hook query
# during a reindex, so hook queries stay hybrid instead of falling back to FTS
SIM_MIN = 0.70              # hybrid: cosine sim a hit needs to count as "strong" (calibrated in EVAL-2026-10-12.md)
COVER_MIN = 0.6             # FTS-only: share of query terms (and at least 3 of them) a hit must contain to be "strong"
STOP = {"the", "a", "an", "to", "of", "on", "in", "is", "it", "and", "or",
        "for", "how", "do", "does", "my", "me", "what", "that", "with",
        "without", "can", "am", "are", "was", "not", "you", "your", "i",
        "we", "our", "about", "this", "should", "which", "did", "when", "where", "why"}
SCHEMA = [
    "CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, path TEXT, heading TEXT, text TEXT,"
    " hash TEXT UNIQUE, importance REAL, mtime REAL)",
    f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(embedding float[{DIM}] distance_metric=cosine)",
    'CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, heading, path, tokenize="porter unicode61")',
    "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)",  # embed_fingerprint: what built the vectors
]

# ---- ollama ---------------------------------------------------------------
def _post(path: str, payload: dict, timeout: float) -> dict:
    import urllib.request  # ponytail: lazy, ~50 ms of imports the FTS-only path never needs
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def embed(texts: list[str], timeout: float = 120) -> list[list[float]]:
    """Batch embed via /api/embed; fall back to /api/embeddings (one at a time)."""
    try:
        d = _post("/api/embed", {"model": EMBED_MODEL, "input": texts}, timeout)
        if d.get("embeddings"):
            return d["embeddings"]
    except Exception as e:
        if isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError):
            raise  # a slow host costs one timeout, not two (else the hook's 5 s KILL beats the FTS fallback)
    return [_post("/api/embeddings", {"model": EMBED_MODEL, "prompt": t}, timeout)["embedding"] for t in texts]

def _fp(weights: str) -> str:
    return json.dumps({"model": EMBED_MODEL, "weights": weights, "dim": DIM,
                       "doc_prefix": DOC_PREFIX, "query_prefix": QUERY_PREFIX}, sort_keys=True)

def fingerprint(timeout: float = 10) -> str:
    """Identity of the vectors the server would produce: the weights blob digest (from /api/show's FROM line; the tag
    digest also covers runtime params like num_thread, which differ between heimdall and thor), model name, dimension
    and task prefixes. Raises if the server cannot report a digest, so writes fail closed instead of mixing models."""
    d = _post("/api/show", {"model": EMBED_MODEL}, timeout)
    m = re.search(r"(?m)^FROM\s+\S*?sha256[-:]([0-9a-f]{64})\s*$", d.get("modelfile") or "")
    if not m:
        raise RuntimeError(f"{OLLAMA} reported no weights digest for {EMBED_MODEL}")
    dims = {v for k, v in (d.get("model_info") or {}).items() if k.endswith(".embedding_length")}
    if dims != {DIM}:
        raise RuntimeError(f"{OLLAMA} reports {EMBED_MODEL} embedding_length {sorted(dims)}, expected {DIM}")
    return _fp("sha256:" + m.group(1))

def ollama_up(timeout: float = 0.3) -> bool:
    u = urllib.parse.urlsplit(OLLAMA)
    if not u.hostname:  # no host configured: never probe localhost by accident
        return False
    try:
        socket.create_connection((u.hostname, u.port or 11434), timeout).close()
        return True
    except OSError:
        return False

def _valid(v) -> bool:
    return isinstance(v, list) and len(v) == DIM and all(math.isfinite(x) for x in v)

# ---- vault parsing --------------------------------------------------------
def display(p: Path) -> str:
    """Vault files: path relative to the vault. Rule files: ~/... path."""
    try:
        return p.relative_to(VAULT).as_posix()
    except ValueError:
        return str(p).replace(str(Path.home()), "~", 1)

def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm = {}
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m:
            fm[m.group(1).strip().lower()] = m.group(2).strip().strip('"')
    return fm, text[end + 4:]

def chunk_file(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8", errors="ignore")  # OSError propagates: walk() keeps the old chunks
    fm, body = parse_frontmatter(raw)
    title = fm.get("title") or fm.get("name") or path.stem
    desc = fm.get("description", "")
    try:
        importance = float(fm.get("importance", 5))
    except ValueError:
        importance = 5.0
    rel = display(path)
    ctx = f"[{rel}] {title}" + (f" — {desc}" if desc else "")

    parts = re.split(r"(?m)^(#{1,3}\s+.*)$", body)
    sections = [("", parts[0])] if parts[0].strip() else []
    for i in range(1, len(parts), 2):
        sections.append((parts[i].strip("# ").strip(), parts[i + 1] if i + 1 < len(parts) else ""))
    chunks = []
    for heading, sbody in sections:
        sbody = sbody.strip()
        if not sbody:
            continue
        windows = [sbody] if len(sbody) <= MAX_SECTION else [sbody[j:j + WINDOW] for j in range(0, len(sbody), WINDOW)]
        for w in windows:
            embed_text = DOC_PREFIX + f"{ctx}\n{heading}\n{w}".strip()
            chunks.append({"path": rel, "heading": heading or title, "text": w, "embed_text": embed_text,
                           "importance": importance,
                           "hash": hashlib.sha1(embed_text.encode("utf-8")).hexdigest()})
    return chunks

def _indexable(parts: tuple) -> bool:
    """walk()'s filter for a vault-relative path: a .md file outside hidden/excluded dirs, files and archives."""
    return (parts[-1].endswith(".md") and not any(x.startswith(".") or x in EXCLUDE_DIRS for x in parts)
            and "/".join(parts) not in EXCLUDE_FILES and not ("_archive" in parts[:-1] and not archived_ok(parts)))

def walk() -> tuple[list[dict], int, set[str], set[str]]:
    """(chunks, eligible file count, failed, seen). Fail-closed: a missing vault or rule file aborts.
    failed = display paths of files that could not be read and of directories that could not be listed
    (a dir ends in "/"); cmd_index keeps their existing chunks instead of treating them as deleted.
    seen = display paths of the files actually read (a note emptied to zero chunks is in seen)."""
    missing = [str(p) for p in [VAULT, *RULE_FILES] if not p.exists()]
    if missing:
        print(f"ABORT: missing {missing}; index UNCHANGED")
        sys.exit(1)
    files, failed, seen = [], set(), set()
    def unlistable(e: OSError):  # rglob swallows these silently, so a chmod'd dir looked like a deleted one
        failed.add(display(Path(e.filename)).rstrip("/") + "/")
    for root, dirs, names in os.walk(VAULT, onerror=unlistable):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in EXCLUDE_DIRS]
        for name in names:
            p = Path(root, name)
            if _indexable(p.relative_to(VAULT).parts):
                files.append(p)
    if "./" in failed:
        print("ABORT: vault root unreadable; index UNCHANGED")
        sys.exit(1)
    files += RULE_FILES
    chunks = []
    for p in files:
        try:
            mtime, cs = p.stat().st_mtime, chunk_file(p)
        except FileNotFoundError:
            continue  # deleted (or a dangling symlink) since os.walk listed it: genuinely gone
        except OSError as e:
            print(f"WARN: cannot read {display(p)} ({e.__class__.__name__}); keeping its existing chunks")
            failed.add(display(p))
            continue
        seen.add(display(p))
        for c in cs:
            c["mtime"] = mtime
            chunks.append(c)
    for d in sorted(x for x in failed if x.endswith("/")):
        print(f"WARN: cannot list {d} ({VAULT / d}); keeping its existing chunks")
    return chunks, len(files), failed, seen

def _protected(path: str, failed: set[str]) -> bool:
    return path in failed or any(d.endswith("/") and path.startswith(d) for d in failed)

def _not_gone(seen: set[str], failed: set[str]) -> set[str]:
    """Indexed paths walk() did not read although the file is still there and still indexable: an enumeration or
    read loss that raised nothing (os.walk files an is_dir() error as "not a dir"; a transient ENOENT mid-rewrite).
    Checked against disk, independently of the walk, so a silent loss can never pass as a deletion."""
    if not DB_PATH.exists():
        return set()
    try:
        indexed = {r[0] for r in connect().execute("SELECT DISTINCT path FROM chunks")}
    except sqlite3.OperationalError:
        return set()  # no chunks table yet
    rules, out = {display(r): r for r in RULE_FILES}, set()
    for p in indexed - seen:
        if _protected(p, failed):
            continue  # already kept and reported by walk()
        if p in rules:
            f = rules[p]
        elif not p.startswith(("~/", "/")) and _indexable(Path(p).parts):
            f = VAULT / p
        else:
            continue  # no longer indexable (exclusion or rule-file change): a real removal
        try:
            f.stat()
        except (FileNotFoundError, NotADirectoryError):
            continue  # really deleted (or a dangling symlink)
        except OSError:
            pass  # cannot tell: keep
        print(f"WARN: {p} is indexed and not provably gone from disk, yet was not read; keeping its existing chunks")
        out.add(p)
    return out

# ---- db --------------------------------------------------------------------
def connect(write: bool = False) -> sqlite3.Connection:
    import sqlite_vec
    if write:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(DB_PATH)
        db.execute("PRAGMA journal_mode=WAL")  # hook queries keep reading while the reindex commits
    else:
        db = sqlite3.connect("file:" + urllib.parse.quote(str(DB_PATH)) + "?mode=ro", uri=True)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    return db

def _counts(db) -> tuple[int, int, int, int]:
    return tuple(db.execute(q).fetchone()[0] for q in (
        "SELECT COUNT(*) FROM chunks", "SELECT COUNT(*) FROM vec_chunks",
        "SELECT COUNT(*) FROM chunks_fts", "SELECT COUNT(DISTINCT path) FROM chunks"))

def cmd_index(rebuild: bool = False, dry_run: bool = False):
    t0 = time.time()
    desired, eligible, failed, seen = walk()
    failed |= _not_gone(seen, failed)
    by_hash = {c["hash"]: c for c in desired}
    if not by_hash:
        print("ABORT: zero chunks; index UNCHANGED")
        sys.exit(1)
    if failed and rebuild:  # a rebuild drops every table, so there is nowhere to keep the unreadable notes' chunks
        print(f"ABORT: {len(failed)} unreadable path(s) {sorted(failed)}; --rebuild would lose them; index UNCHANGED")
        sys.exit(1)
    if dry_run:
        existing = {}
        if not rebuild and DB_PATH.exists():
            existing = dict(connect().execute("SELECT hash, path FROM chunks"))
        kept = {h for h, p in existing.items() if h not in by_hash and _protected(p, failed)}
        nfiles = len({c["path"] for c in desired})
        print(f"dry-run: {len(by_hash)} chunks from {nfiles}/{eligible} eligible files; would embed "
              f"{len(set(by_hash) - set(existing))}, remove {len(set(existing) - set(by_hash) - kept)}, "
              f"keep {len(kept)} from {len(failed)} unreadable (rebuild={rebuild}); nothing written")
        return
    db = connect(write=True)
    old = db.execute("SELECT sql FROM sqlite_master WHERE name='vec_chunks'").fetchone()
    if old and "cosine" not in old[0] and not rebuild:
        print("ABORT: vec_chunks is the old L2 table; run `index --rebuild`")
        sys.exit(1)
    if not rebuild:
        for s in SCHEMA:
            db.execute(s)
        db.commit()
    existing = {} if rebuild else dict(db.execute("SELECT hash, path FROM chunks"))
    to_add = [c for h, c in by_hash.items() if h not in existing]
    kept = {h for h, p in existing.items() if h not in by_hash and _protected(p, failed)}
    to_del = list(set(existing) - set(by_hash) - kept)
    want = len(existing) - len(to_del) + len(to_add)  # from what is on disk, not from the (possibly partial) walk
    print(f"chunks: {len(by_hash)} | new: {len(to_add)} | removed: {len(to_del)} | kept (unreadable): {len(kept)} | rebuild={rebuild}")

    # 0) one embedding model per index (astra5_1): every vector written must come from the fingerprint the index was
    #    built with; a different one needs --rebuild, which never reuses cached vectors from another fingerprint.
    stored = None if rebuild else (db.execute("SELECT value FROM meta WHERE key='embed_fingerprint'").fetchone() or [None])[0]
    fp = None
    if rebuild or to_add or stored is None:  # a removal-only run writes no vectors: no host needed
        if not OLLAMA:
            print(f"ABORT: {NO_HOST}; index UNCHANGED")
            sys.exit(1)
        try:
            fp = fingerprint()
        except Exception as e:
            print(f"ABORT: cannot fingerprint {EMBED_MODEL} at {OLLAMA} ({e!r}); refusing to write vectors; index UNCHANGED")
            sys.exit(1)
        if not rebuild and stored is None and existing and fp != _fp(LEGACY_WEIGHTS):
            print(f"ABORT: index has no embedding fingerprint and the server's {fp} is not the legacy {LEGACY_WEIGHTS}; "
                  "run `index --rebuild`; index UNCHANGED")
            sys.exit(1)
        if stored is not None and fp != stored:
            print(f"ABORT: embedding fingerprint changed (index {stored}, server {fp}); run `index --rebuild`; index UNCHANGED")
            sys.exit(1)

    # 1) embed everything first and hold it in memory: an Ollama failure leaves the index untouched.
    #    Each batch is also appended to the cache (keyed by content hash, fingerprint on line 1), so a killed run resumes.
    #    After each batch a fresh fingerprint check appends a seal line; a resume trusts only vectors before the last
    #    seal, because a run killed after a mid-run model switch never reached the check for its tail (astra5r).
    cache_path, cache, data = DB_PATH.with_name("embed-cache.jsonl"), {}, None
    try:
        data = cache_path.read_bytes()
    except FileNotFoundError:
        pass
    seal = (json.dumps({"fingerprint": fp}) + "\n").encode()
    if data is not None and to_add:
        pending, sealed_end, off, ok = {}, 0, 0, data.startswith(seal)
        for raw in data.splitlines(keepends=True) if ok else []:
            off += len(raw)
            if raw == seal:
                cache.update(pending)
                pending, sealed_end = {}, off
                continue
            try:
                x = json.loads(raw)
            except ValueError:
                continue  # a line cut off by a kill
            if isinstance(x, dict) and x != {"fingerprint": fp}:
                ok = False  # another fingerprint's line: a concurrent run wrote here, nothing in the file is trusted
                break
            if isinstance(x, list) and len(x) == 2 and isinstance(x[0], str) and x[0] in by_hash and _valid(x[1]):
                pending[x[0]] = x[1]
        if not ok:  # empty, cut off, a pre-fingerprint cache, or another fingerprint
            nl = data.count(b"\n")
            print(f"  discarding {cache_path.name} ({nl} lines): cached vectors from another or unknown embedding fingerprint")
            cache_path.unlink()
            cache, data = {}, None
        elif sealed_end < len(data):  # appends must start at a seal, or a later seal would vouch for this tail too
            print(f"  dropping {len(data) - sealed_end} unsealed bytes ({len(pending)} vectors) from {cache_path.name}")
            os.truncate(cache_path, sealed_end)
    embedded = [(c, cache[c["hash"]]) for c in to_add if c["hash"] in cache]
    todo = [c for c in to_add if c["hash"] not in cache]
    if embedded:
        print(f"  resumed {len(embedded)} embeddings from {cache_path.name}")
    if todo:  # never create an empty, headerless cache
        with open(cache_path, "ab") as cf:
            if data is None:
                cf.write(seal)
            for i in range(0, len(todo), BATCH):
                batch = todo[i:i + BATCH]
                try:
                    vecs = embed([c["embed_text"] for c in batch])
                except Exception as e:
                    print(f"ABORT: embedding failed for batch at {i} ({e!r}); index UNCHANGED, {len(embedded)} cached for the next run")
                    sys.exit(1)
                if len(vecs) != len(batch) or not all(_valid(v) for v in vecs):
                    print(f"ABORT: bad embeddings for batch at {i}; index UNCHANGED")
                    sys.exit(1)
                cf.write("".join(json.dumps([c["hash"], v]) + "\n" for c, v in zip(batch, vecs)).encode())
                cf.flush()
                try:  # the host could switch models mid-run (a 2 h CPU reindex): seal only what the same model made
                    fp_now = fingerprint()
                except Exception as e:
                    print(f"ABORT: cannot re-confirm the embedding fingerprint ({e!r}); index UNCHANGED, sealed cache kept")
                    sys.exit(1)
                if fp_now != fp:
                    cache_path.unlink(missing_ok=True)  # its vectors may now span two models
                    print(f"ABORT: embedding fingerprint changed during the run ({fp} -> {fp_now}); cache discarded, index UNCHANGED")
                    sys.exit(1)
                cf.write(seal)
                cf.flush()
                embedded += zip(batch, vecs)
                if (i // BATCH) % 40 == 39:
                    print(f"  embedded {len(embedded)}/{len(to_add)}")

    # 2) mutate in one transaction; counts must agree before commit.
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            if not rebuild:  # astra5r: another run (a --rebuild with another model) may have committed meanwhile
                now = (db.execute("SELECT value FROM meta WHERE key='embed_fingerprint'").fetchone() or [None])[0]
                if now != stored:
                    raise RuntimeError(f"embedding fingerprint changed under this run (checked {stored}, now {now})")
            if rebuild:
                for t in ("chunks", "vec_chunks", "chunks_fts"):
                    db.execute(f"DROP TABLE IF EXISTS {t}")
                for s in SCHEMA:
                    db.execute(s)
            elif to_del:
                ph = ",".join("?" * len(to_del))
                ids = [r[0] for r in db.execute(f"SELECT id FROM chunks WHERE hash IN ({ph})", to_del)]
                for i in ids:
                    db.execute("DELETE FROM vec_chunks WHERE rowid=?", (i,))
                    db.execute("DELETE FROM chunks_fts WHERE rowid=?", (i,))
                db.execute(f"DELETE FROM chunks WHERE hash IN ({ph})", to_del)
            for c, v in embedded:
                rid = db.execute("INSERT INTO chunks(path,heading,text,hash,importance,mtime) VALUES(?,?,?,?,?,?)",
                                 (c["path"], c["heading"], c["text"], c["hash"], c["importance"], c["mtime"])).lastrowid
                db.execute("INSERT INTO vec_chunks(rowid,embedding) VALUES(?,?)", (rid, json.dumps(v)))
                db.execute("INSERT INTO chunks_fts(rowid,text,heading,path) VALUES(?,?,?,?)",
                           (rid, c["text"], c["heading"], c["path"]))
            if fp is not None:
                db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('embed_fingerprint', ?)", (fp,))
            n, nv, nf, nfiles = _counts(db)
            if not (n == nv == nf == want == len(by_hash) + len(kept)):
                raise RuntimeError(f"count mismatch chunks={n} vec={nv} fts={nf} want={want} "
                                   f"(desired={len(by_hash)} kept={len(kept)})")
    except Exception as e:
        print(f"ABORT: {e}; rolled back, index UNCHANGED")
        sys.exit(1)
    cache_path.unlink(missing_ok=True)  # committed: every cached vector is in the index now
    print(f"indexed in {time.time() - t0:.1f}s: chunks={n} files={nfiles}/{eligible} "
          f"coverage={100 * len({c['path'] for c in desired}) / eligible:.1f}% new={len(to_add)} removed={len(to_del)} kept={len(kept)} "
          f"[{'PARTIAL: ' + str(len(failed)) + ' unreadable, old chunks kept' if failed else 'OK'}]")
    if failed:
        sys.exit(1)  # committed, but the timer must surface the unreadable paths

# ---- retrieval -------------------------------------------------------------
def _tokens(q: str) -> list[str]:
    out = []
    for t in re.findall(r"[a-z0-9]+", q.lower()):
        if len(t) > 2 and t not in STOP and t not in out:
            out.append(t)
    return out

def search(q: str, k: int = 8, fts_only: bool = False) -> dict:
    t0 = time.time()
    db = connect()
    toks = _tokens(q)
    lex = []
    if toks:
        try:
            lex = [r[0] for r in db.execute(
                "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
                (" OR ".join(f'"{t}"' for t in toks), CANDIDATES))]
        except sqlite3.OperationalError:
            pass
    qvec = None
    if not fts_only and ollama_up():
        try:  # ponytail: the embed host keeps the model resident; past 3 s we answer FTS-only
            qvec = json.dumps(embed([QUERY_PREFIX + q], timeout=3)[0])
        except Exception:
            qvec = None
    vec = [r[0] for r in db.execute("SELECT rowid FROM vec_chunks WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                                    (qvec, CANDIDATES))] if qvec else []
    fused = {}
    for w, ranked in ((W_VEC, vec), (W_LEX, lex)):
        for r, rid in enumerate(ranked):
            fused[rid] = fused.get(rid, 0) + w / (RRF_K + r)
    res = {"query": q, "mode": "hybrid" if qvec else "fts", "hits": []}
    if fused:
        ids = list(fused)
        ph = ",".join("?" * len(ids))
        sim = dict(db.execute(f"SELECT rowid, 1 - vec_distance_cosine(embedding, ?) FROM vec_chunks WHERE rowid IN ({ph})",
                              [qvec, *ids])) if qvec else {}
        cover = dict.fromkeys(ids, 0)
        for t in toks:
            for (rid,) in db.execute(f"SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? AND rowid IN ({ph})",
                                     [f'"{t}"', *ids]):
                cover[rid] += 1
        now, ranked = time.time(), []
        for rid, path, heading, text, imp, mtime, h in db.execute(
                f"SELECT id,path,heading,text,importance,mtime,hash FROM chunks WHERE id IN ({ph})", ids):
            tie = 1.0 + 0.03 * min(max(imp, 0.0), 10.0) / 10.0 + 0.02 * math.exp(-max(0.0, now - mtime) / 86400 / 180)
            ranked.append((fused[rid] * tie, rid, path, heading, text, h))
        ranked.sort(key=lambda x: -x[0])
        per_path = {}
        for score, rid, path, heading, text, h in ranked:
            if per_path.get(path, 0) >= 2:  # diversify: at most 2 chunks per file
                continue
            per_path[path] = per_path.get(path, 0) + 1
            cov = cover[rid] / len(toks) if toks else 0.0
            strong = sim[rid] >= SIM_MIN if qvec else (cov >= COVER_MIN and cover[rid] >= 3)
            res["hits"].append({"path": path, "heading": heading, "score": round(score, 4),
                                "sim": round(sim[rid], 3) if qvec else None, "cover": round(cov, 2),
                                "strong": bool(strong), "id": h[:12],
                                "snippet": re.sub(r"\s+", " ", text).strip()[:240]})
            if len(res["hits"]) >= k:
                break
    res["ms"] = round((time.time() - t0) * 1000)
    return res

def cmd_query(q: str, k: int, as_json: bool, fts_only: bool):
    res = search(q, k, fts_only)
    if as_json:
        print(json.dumps(res, ensure_ascii=False))
        return
    for h in res["hits"]:
        sim = f" sim={h['sim']:.2f}" if h["sim"] is not None else ""
        print(f"[{h['score']:.4f}{sim} cov={h['cover']:.2f}{' *' if h['strong'] else ''}] {h['path']}#{h['heading']}")
        print(f"    {h['snippet']}")
    print(f"({res['mode']}, {len(res['hits'])} hits, {res['ms']} ms; * = strong){'' if OLLAMA or fts_only else '; ' + NO_HOST}")

def cmd_stats():
    db = connect()
    n, nv, nf, files = _counts(db)
    metric = "cosine" if "cosine" in db.execute("SELECT sql FROM sqlite_master WHERE name='vec_chunks'").fetchone()[0] else "L2"
    print(f"chunks={n} vec={nv} fts={nf} files={files} metric={metric} [{'OK' if n == nv == nf else 'MISMATCH'}] -> {DB_PATH}")

# ---- self-test (offline: temp vault, fake embeddings, no thor) --------------
def self_test():
    import importlib.util
    if importlib.util.find_spec("sqlite_vec") is None:
        print("sqlite-vec is not installed in this Python; install it or set VAULT_RAG_PY for vault-recall")
        raise SystemExit(77)
    import tempfile
    global VAULT, RULE_FILES, DB_PATH, OLLAMA, embed, ollama_up, fingerprint
    real_embed, real_fingerprint = embed, fingerprint
    OLLAMA = OLLAMA or "http://192.0.2.1:11434"  # fresh clone, no host configured: the fakes below never call it
    fp_a, fp_b = _fp("sha256:" + "a" * 64), _fp("sha256:" + "b" * 64)
    fingerprint = lambda timeout=10: fp_a  # noqa: E731
    tmp = Path(tempfile.mkdtemp(prefix="vault-rag-test-"))
    VAULT, DB_PATH = tmp / "vault", tmp / "state" / "idx.db"
    files = {"Projects/thor.md": "# Thor sleep\nthor suspends after 60 minutes idle; run thor-up to wake it before a backup.",
             "harness-memory/mail.md": "---\nname: mail\n---\n# Mail rules\nteam mail pipeline: Codex triages, Claude sends via send.py.",
             "_archive/old.md": "# Old\nthor archived note", ".sync/x.md": "# Hidden\nthor", "log.md": "# Log\nthor log",
             "Projects/log.md": "# Project log\nnested log notes are indexed",
             "_archive/log.md": "# Raw log\nrotated raw vault log", "_archive/projects/gone/status.md": "# Gone\nzephyr project moved to the archive",
             "Projects/p/_archive/rotated.md": "# Rotated\nolder project history", "Projects/_archive/legacy/status.md": "# Legacy\nlegacy project",
             "Actions/_archive/stale.md": "# Stale\nsuperseded rule", "harness-memory/_archive/m.md": "# Old memory\nsuperseded",
             "log-archive/x.md": "# Old log\nlog", "Blog/post.md": "# Post\nblog"}
    for rel, body in files.items():
        (VAULT / rel).parent.mkdir(parents=True, exist_ok=True)
        (VAULT / rel).write_text(body)
    RULE_FILES = [tmp / "AGENTS.md"]
    RULE_FILES[0].write_text("# Rules\nNever push a repo public without the gate.")

    def fake_embed(texts, timeout=120):  # hashed bag of words, normalized
        out = []
        for t in texts:
            v = [0.0] * DIM
            for w in re.findall(r"[a-z]+", t.lower()):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out
    embed, ollama_up = fake_embed, (lambda timeout=0.3: True)

    def expect_exit(fn):
        try:
            fn()
        except SystemExit:
            return
        raise AssertionError("expected abort")

    db = connect(write=True)  # an old-style L2 table must block incremental runs, and --rebuild must migrate it
    db.execute(f"CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[{DIM}])")
    db.commit()
    expect_exit(lambda: cmd_index(False))
    cmd_index(rebuild=True)
    ro = connect()
    assert "cosine" in ro.execute("SELECT sql FROM sqlite_master WHERE name='vec_chunks'").fetchone()[0]
    n, nv, nf, nfiles = _counts(ro)
    assert n == nv == nf and nfiles == 8, (n, nv, nf, nfiles)  # 4 live (thor, mail, Projects/log, AGENTS) + 4 archived
    paths = {r[0] for r in ro.execute("SELECT DISTINCT path FROM chunks WHERE path NOT LIKE '%AGENTS.md'")}
    assert paths == {"Projects/thor.md", "harness-memory/mail.md", "Projects/log.md", "_archive/old.md", "_archive/projects/gone/status.md",
                     "Projects/p/_archive/rotated.md", "Projects/_archive/legacy/status.md"}, paths  # schema: archive tier stays searchable
    assert search("zephyr project archive", 3, fts_only=True)["hits"][0]["path"] == "_archive/projects/gone/status.md"
    cmd_index(False)  # unchanged: nothing new
    assert _counts(connect())[0] == n
    (VAULT / "Projects/thor.md").write_text("# Thor sleep\nthor now suspends after 90 minutes idle.")
    cmd_index(False)
    assert "90 minutes" in connect().execute("SELECT text FROM chunks WHERE path='Projects/thor.md'").fetchone()[0]
    cmd_index(dry_run=True)
    # resume: a run that dies after its first batch leaves the index unchanged; the rerun embeds only the rest
    before = dict(connect().execute("SELECT hash, path FROM chunks"))
    for k in range(BATCH + 5):
        (VAULT / f"Projects/bulk{k}.md").write_text(f"# Bulk {k}\nfiller note number {k} about kumquats")
    calls = []
    def dies_after_one(texts, timeout=120):
        if calls:
            raise TimeoutError("host gone")
        calls.append(len(texts))
        return fake_embed(texts)
    embed = dies_after_one
    expect_exit(lambda: cmd_index(False))
    assert dict(connect().execute("SELECT hash, path FROM chunks")) == before and calls == [BATCH]
    def counting(texts, timeout=120):
        calls.append(len(texts))
        return fake_embed(texts)
    embed = counting
    cmd_index(False)
    assert calls[0] == BATCH and sum(calls[1:]) == 5, calls  # only the 5 not cached
    assert _counts(connect())[0] == len(before) + BATCH + 5 and not (DB_PATH.with_name("embed-cache.jsonl")).exists()
    for k in range(BATCH + 5):
        (VAULT / f"Projects/bulk{k}.md").unlink()
    embed = fake_embed
    cmd_index(False)
    assert dict(connect().execute("SELECT hash, path FROM chunks")) == before
    # astra5_1: one embedding model per index. Astra r5 repro: a killed run cached model-A vectors, the host switched to
    # model B, and `index --rebuild` committed the cached A vectors next to fresh B ones as [OK].
    import struct
    def model_b(texts, timeout=120):  # another model: same shape, different vectors
        return [v[::-1] for v in fake_embed(texts)]
    def counting_b(texts, timeout=120):
        calls.append(len(texts))
        return model_b(texts)
    stored_fp = lambda: connect().execute("SELECT value FROM meta WHERE key='embed_fingerprint'").fetchone()[0]  # noqa: E731
    rows = lambda: dict(connect().execute("SELECT hash, path FROM chunks"))  # noqa: E731
    cache = DB_PATH.with_name("embed-cache.jsonl")
    assert stored_fp() == fp_a
    for k in range(BATCH + 2):
        (VAULT / f"Projects/mix{k}.md").write_text(f"# Mix {k}\nmixed model note {k} about quinces")
    calls.clear()
    embed = dies_after_one
    expect_exit(lambda: cmd_index(False))  # leaves BATCH model-A vectors in the cache
    assert json.loads(cache.read_text().splitlines()[0]) == {"fingerprint": fp_a} and calls == [BATCH]
    fingerprint, embed = (lambda timeout=10: fp_b), counting_b
    calls.clear()
    expect_exit(lambda: cmd_index(False))  # incremental under another model: refused before embedding anything
    assert calls == [] and rows() == before and stored_fp() == fp_a and cache.exists()
    cmd_index(rebuild=True)  # discards the model-A cache, re-embeds every chunk with B
    n = _counts(connect())[0]
    assert sum(calls) == n and stored_fp() == fp_b and not cache.exists(), (calls, n)
    want = {c["hash"]: model_b([c["embed_text"]])[0] for c in walk()[0]}
    got = connect().execute("SELECT c.hash, v.embedding FROM chunks c JOIN vec_chunks v ON v.rowid = c.id").fetchall()
    assert len(got) == n and all(max(abs(a - b) for a, b in zip(struct.unpack(f"{DIM}f", e), want[h])) < 1e-6
                                 for h, e in got), "a vector from another model was committed"
    # no digest from the server: removal-only runs still commit, any vector write fails closed, retrieval still works
    def no_digest(timeout=10):
        raise RuntimeError("no weights digest")
    fingerprint = no_digest
    (VAULT / "Projects/mix1.md").unlink()
    cmd_index(False)
    snap = rows()
    (VAULT / "Projects/mix0.md").write_text("# Mix 0\nchanged quince note")
    calls.clear()
    expect_exit(lambda: cmd_index(False))
    expect_exit(lambda: cmd_index(rebuild=True))
    assert calls == [] and rows() == snap and not cache.exists() and stored_fp() == fp_b
    assert search("mixed model note about quinces", 3, fts_only=True)["hits"]
    seq = iter([fp_b, fp_a])  # the host switches models while the run embeds: abort, drop the now-mixed cache
    fingerprint = lambda timeout=10: next(seq)  # noqa: E731
    expect_exit(lambda: cmd_index(False))
    assert calls == [1] and rows() == snap and not cache.exists()
    fingerprint = lambda timeout=10: fp_b  # noqa: E731
    cmd_index(False)
    # a pre-fingerprint index adopts only the legacy weights, without re-embedding; anything else needs --rebuild
    w = connect(write=True)
    w.execute("DELETE FROM meta")
    w.commit()
    calls.clear()
    expect_exit(lambda: cmd_index(False))
    fingerprint = lambda timeout=10: _fp(LEGACY_WEIGHTS)  # noqa: E731
    cmd_index(False)
    assert calls == [] and stored_fp() == _fp(LEGACY_WEIGHTS)
    # a pre-fingerprint (headerless) cache has unknown provenance: discarded, not reused
    (VAULT / "Projects/mix9.md").write_text("# Mix 9\nlate quince note")
    h9 = chunk_file(VAULT / "Projects/mix9.md")[0]["hash"]
    cache.write_text(json.dumps([h9, fake_embed(["x"])[0]]) + "\n")
    cmd_index(False)
    assert calls == [1] and not cache.exists()
    # astra5_1r_killtail (reviewer variant of astra5_1): the host switches models mid-run and the run is SIGKILLed at
    # the 2 h cap before any check sees it; the host goes back, and the resume must not reuse the other model's tail.
    # Before the per-batch seals, the cache header vouched for every line and the resume committed both models.
    def vectors_are(model):
        want = {c["hash"]: model([c["embed_text"]])[0] for c in walk()[0]}
        got = connect().execute("SELECT c.hash, v.embedding FROM chunks c JOIN vec_chunks v ON v.rowid = c.id").fetchall()
        return len(got) == len(want) and all(
            max(abs(a - b) for a, b in zip(struct.unpack(f"{DIM}f", e), want[h])) < 1e-6 for h, e in got)
    fp_l = _fp(LEGACY_WEIGHTS)
    assert stored_fp() == fp_l and vectors_are(model_b)
    for k in range(3 * BATCH):
        (VAULT / f"Projects/mixk{k}.md").write_text(f"# Mixk {k}\nkilled run quince note {k}")
    st = {"batches": 0, "dead": False}
    def switch_then_die(texts, timeout=120):
        if st["dead"]:
            raise KeyboardInterrupt  # stands in for SIGKILL: no handler, no end check
        st["batches"] += 1
        if st["batches"] == 2:  # the host now serves another model; the process dies right after this batch
            st["dead"] = True
            return fake_embed(texts)
        return model_b(texts)
    def fp_or_die(timeout=10):
        if st["dead"]:
            raise KeyboardInterrupt
        return fp_l
    fingerprint, embed = fp_or_die, switch_then_die
    try:
        cmd_index(False)
        raise AssertionError("expected the simulated kill")
    except KeyboardInterrupt:
        pass
    assert cache.read_bytes().count(b"\n") == 1 + BATCH + 1 + BATCH  # header, sealed batch, unsealed other-model batch
    fingerprint, embed = (lambda timeout=10: fp_l), counting_b  # noqa: E731
    calls.clear()
    cmd_index(False)  # the host is back: reuse the sealed batch, re-embed the rest
    assert calls == [BATCH, BATCH] and vectors_are(model_b) and not cache.exists(), calls
    # a resume cuts the unsealed tail off the file before appending (else its next seal would vouch for those lines)
    seal_l = json.dumps({"fingerprint": fp_l}) + "\n"
    for k in (6, 7):
        (VAULT / f"Projects/mixk{k}.md").write_text(f"# Mixk {k}\ntail quince note {k}")
    c6, c7 = (chunk_file(VAULT / f"Projects/mixk{k}.md")[0] for k in (6, 7))
    sealed = seal_l + json.dumps([c6["hash"], model_b([c6["embed_text"]])[0]]) + "\n" + seal_l
    cache.write_text(sealed + json.dumps([c7["hash"], fake_embed([c7["embed_text"]])[0]]) + "\n")
    def die_at_once(texts, timeout=120):
        raise KeyboardInterrupt
    embed = die_at_once
    try:
        cmd_index(False)
        raise AssertionError("expected the simulated kill")
    except KeyboardInterrupt:
        pass
    assert cache.read_text() == sealed
    embed = counting_b
    calls.clear()
    cmd_index(False)
    assert calls == [1] and vectors_are(model_b) and not cache.exists(), calls
    # a line from another fingerprint inside the file (two runs on two hosts appended to one cache): trust none of it
    (VAULT / "Projects/mixk9.md").write_text("# Mixk 9\ninterleaved quince note")
    h = chunk_file(VAULT / "Projects/mixk9.md")[0]["hash"]
    cache.write_text(seal_l + json.dumps({"fingerprint": fp_a}) + "\n" + json.dumps([h, fake_embed(["x"])[0]]) + "\n" + seal_l)
    calls.clear()
    cmd_index(False)
    assert calls == [1] and vectors_are(model_b) and not cache.exists()
    # astra5_1r_concurrent (reviewer variant): a --rebuild with another model (another host) commits while an
    # incremental run embeds a note that rebuild never saw; the incremental commit must abort, not add its vector and
    # stamp its own fingerprint over the other model's index (the counts agree, so only the fingerprint can tell).
    x = VAULT / "Projects/mixx.md"
    x.write_text("# Mixx\nconcurrent quince note")
    def rebuild_meanwhile(texts, timeout=120):
        global fingerprint, embed
        x.rename(tmp / "mixx.hidden")  # that rebuild walked the vault before the note existed
        fingerprint, embed = (lambda timeout=10: fp_a), fake_embed  # noqa: E731
        cmd_index(rebuild=True)
        (tmp / "mixx.hidden").rename(x)
        fingerprint, embed = (lambda timeout=10: fp_l), counting_b  # noqa: E731
        return counting_b(texts)
    embed = rebuild_meanwhile
    expect_exit(lambda: cmd_index(False))
    assert stored_fp() == fp_a and "Projects/mixx.md" not in rows().values() and vectors_are(fake_embed) is False
    x.rename(tmp / "mixx.hidden")
    assert vectors_are(fake_embed)  # the rebuild's index, untouched by the aborted run
    (tmp / "mixx.hidden").rename(x)
    expect_exit(lambda: cmd_index(False))  # and the old model is refused from now on
    # the real fingerprint(): weights blob from /api/show's FROM line plus the reported dimension
    import http.server, threading
    show = {}
    class Show(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps(show).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass
    hs = http.server.HTTPServer(("127.0.0.1", 0), Show)
    threading.Thread(target=hs.serve_forever, daemon=True).start()
    OLLAMA = f"http://127.0.0.1:{hs.server_address[1]}"
    show.update(modelfile="# x\nFROM /root/.ollama/models/blobs/sha256-" + LEGACY_WEIGHTS[7:] + "\nPARAMETER num_thread 3\n",
                model_info={"nomic-bert.embedding_length": DIM})
    assert real_fingerprint() == _fp(LEGACY_WEIGHTS)
    for bad in ({"modelfile": "FROM nomic-embed-text:latest\n"}, {"model_info": {"nomic-bert.embedding_length": 1024}}):
        show.update(bad)
        try:
            real_fingerprint()
            raise AssertionError(f"fingerprint accepted {bad}")
        except RuntimeError:
            pass
    hs.shutdown()
    fingerprint, embed = (lambda timeout=10: fp_a), fake_embed  # back to model A for the retrieval checks below
    for p in VAULT.glob("Projects/mix*.md"):
        p.unlink()
    cmd_index(rebuild=True)
    assert rows() == before and stored_fp() == fp_a
    # astra5_2: an empty VAULT_RAG_OLLAMA_HOST means unset, as in reindex.sh (it used to become "http://")
    assert _ollama_host("") == _ollama_host(None) == _ollama_host(" ") == ""
    assert _ollama_host("thor:11434/") == "http://thor:11434"
    # astra5_2r_noscheme (reviewer variant): no scheme means Ollama's http on :11434, as reindex.sh assumes when it
    # wakes thor for "thor" or its address; it used to send requests to port 80 while ollama_up() probed :11434
    assert _ollama_host("thor") == "http://thor:11434" and _ollama_host("192.0.2.7") == "http://192.0.2.7:11434"
    assert _ollama_host("[::1]") == "http://[::1]:11434" and _ollama_host("http://192.0.2.8:11434/") == "http://192.0.2.8:11434"
    assert all(urllib.parse.urlsplit(_ollama_host(h)).port == 11434 for h in ("thor", "thor:11434", " 192.0.2.7 "))
    # kit T1: the host comes from VAULT_RAG_OLLAMA_HOST, else EMBED_URL in hosts.env (KIT_HOSTS_ENV), else none
    import subprocess, tempfile
    hf = Path(tempfile.mkdtemp()) / "hosts.env"
    hf.write_text("# c\nEMBED_URL=http://192.0.2.1:11434\nEMBED_URL=\"http://192.0.2.8:11434\"\n")
    def child(code, **env):
        e = {k: v for k, v in os.environ.items() if k not in ("VAULT_RAG_OLLAMA_HOST", "EMBED_URL")}
        o = subprocess.run([sys.executable, "-c", "import vault_rag as v; " + code], capture_output=True, text=True,
                           cwd=Path(__file__).resolve().parent, env={**e, **env})
        return o.stdout.strip() or o.stderr.strip()
    assert child("print(v.OLLAMA)", KIT_HOSTS_ENV=str(hf), VAULT_RAG_OLLAMA_HOST="") == "http://192.0.2.8:11434"
    assert child("print(v.OLLAMA)", KIT_HOSTS_ENV=str(hf), VAULT_RAG_OLLAMA_HOST="thor") == "http://thor:11434"
    assert child("print(v.OLLAMA)", KIT_HOSTS_ENV=str(hf), EMBED_URL="192.0.2.9") == "http://192.0.2.9:11434"  # env wins
    assert child("print(repr(v.OLLAMA), v.ollama_up())", KIT_HOSTS_ENV=str(hf.parent / "none")) == "'' False"
    hf.unlink(); hf.parent.rmdir()
    import contextlib, io
    o, up, before = OLLAMA, ollama_up, rows()
    OLLAMA, ollama_up = "", (lambda timeout=0.3: False)  # ollama_up() is False without a host (child check above)
    try:
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            cmd_index(rebuild=True)
        raise AssertionError("index without an embed host must abort")
    except SystemExit as e:
        assert e.code == 1 and "set EMBED_URL" in buf.getvalue() and rows() == before, buf.getvalue()
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        cmd_query("how do I wake thor before a backup", 3, False, False)
    assert "(fts," in buf.getvalue() and "set EMBED_URL" in buf.getvalue(), buf.getvalue()
    OLLAMA, ollama_up = o, up
    r = search("how do I wake thor before a backup", 3, fts_only=True)
    assert r["mode"] == "fts" and r["hits"][0]["path"] == "Projects/thor.md", r
    r = search("rules for the team mail pipeline", 3)
    assert r["mode"] == "hybrid" and r["hits"][0]["path"] == "harness-memory/mail.md", r
    assert all(h["strong"] == (h["sim"] >= SIM_MIN) for h in r["hits"]) and r["hits"][0]["sim"] > r["hits"][-1]["sim"], r
    assert search("thor suspends after idle minutes", 3, fts_only=True)["hits"][0]["strong"]
    r = search("banana bread recipe with walnuts please", 3, fts_only=True)
    assert not any(h["strong"] for h in r["hits"]), r
    ollama_up = lambda timeout=0.3: False  # thor asleep: must fall back to FTS without waking it
    assert search("thor idle suspend", 3)["mode"] == "fts"
    # astra2_8: an unreadable note or unlistable dir keeps its old chunks (exit 1); a real deletion still deletes
    before = {r[0]: r[1] for r in connect().execute("SELECT hash, path FROM chunks")}
    locked = [VAULT / "Projects/thor.md", VAULT / "harness-memory"]
    for x in locked:
        x.chmod(0)
    try:
        expect_exit(lambda: cmd_index(rebuild=True))  # a rebuild would drop them: abort, nothing touched
        assert dict(connect().execute("SELECT hash, path FROM chunks")) == before
        cmd_index(dry_run=True)
        (VAULT / "Projects/log.md").unlink()
        expect_exit(lambda: cmd_index(False))  # commits the rest, exits 1 so the timer surfaces it
        after = dict(connect().execute("SELECT hash, path FROM chunks"))
        assert after == {h: p for h, p in before.items() if p != "Projects/log.md"}, (before, after)
        n, nv, nf, _ = _counts(connect())
        assert n == nv == nf == len(after)
    finally:
        for x in locked:
            x.chmod(0o755 if x.is_dir() else 0o644)
    cmd_index(False)  # readable again: exits 0, nothing to add or remove
    assert dict(connect().execute("SELECT hash, path FROM chunks")) == after
    # astra2_8 (review): a loss that raises nothing (os.walk files an is_dir() error as a non-dir; ENOENT while a
    # writer recreates the file) is caught on disk: chunks kept, exit 1; an emptied note still loses its chunks
    import unittest.mock as um
    real_walk, real_read = os.walk, Path.read_text
    def blind_walk(top, onerror=None):
        for root, dirs, names in real_walk(top, onerror=onerror):
            dirs[:] = [d for d in dirs if d != "harness-memory"]
            yield root, dirs, names
    def enoent(self, *a, **k):
        if self.name == "thor.md":
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_read(self, *a, **k)
    with um.patch.object(os, "walk", blind_walk), um.patch.object(Path, "read_text", enoent):
        expect_exit(lambda: cmd_index(rebuild=True))  # nothing dropped
        expect_exit(lambda: cmd_index(False))
    assert dict(connect().execute("SELECT hash, path FROM chunks")) == after
    (VAULT / "Projects/thor.md").write_text("---\nname: thor\n---\n")  # read fine, zero chunks: a real removal
    cmd_index(False)
    assert not connect().execute("SELECT 1 FROM chunks WHERE path='Projects/thor.md'").fetchone()
    VAULT = tmp / "missing"
    expect_exit(lambda: cmd_index(False))  # missing vault never touches the index
    assert _counts(connect())[0] > 0
    srv = socket.socket()  # an Ollama that accepts but never answers: real embed() must time out once, not retry
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    OLLAMA, t1 = f"http://127.0.0.1:{srv.getsockname()[1]}", time.time()
    try:
        real_embed(["x"], timeout=0.4)
        raise AssertionError("expected a timeout")
    except (TimeoutError, OSError):
        pass
    assert time.time() - t1 < 0.7, f"embed retried after a timeout ({time.time() - t1:.2f}s)"
    srv.close()
    import shutil
    shutil.rmtree(tmp)
    print("self-test OK")

# ---- cli -------------------------------------------------------------------
if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
        sys.exit(0)
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("index")
    pi.add_argument("--rebuild", action="store_true")
    pi.add_argument("--dry-run", action="store_true")
    pq = sub.add_parser("query")
    pq.add_argument("q")
    pq.add_argument("-k", type=int, default=8)
    pq.add_argument("--json", action="store_true")
    pq.add_argument("--fts-only", action="store_true")
    sub.add_parser("stats")
    a = ap.parse_args()
    if a.cmd == "index":
        cmd_index(a.rebuild, a.dry_run)
    elif a.cmd == "query":
        cmd_query(a.q, a.k, a.json, a.fts_only)
    else:
        cmd_stats()
