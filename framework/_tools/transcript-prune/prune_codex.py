#!/usr/bin/env python3
"""Delete Codex session rollouts (+ their rows in the two thread DBs) untouched for N days (default 21, floor 7).
Dry run unless --apply. Claude Code expires its own transcripts via cleanupPeriodDays in ~/.claude/settings.json.
CODEX_HOME overrides ~/.codex (the self-test points it at a scratch fixture). --self-test: run against a fixture."""
import argparse, os, sqlite3, subprocess, sys, tempfile, time

MIN_DAYS = 7   # audit 2026-10-12: --days 0 (or negative) on a manual --apply removed nearly every session


def self_test():
    with tempfile.TemporaryDirectory() as h:
        old, new = time.time() - 30 * 86400, time.time()
        os.makedirs(f"{h}/sessions/2026/09/01")
        st = sqlite3.connect(f"{h}/state_5.sqlite")
        st.executescript("create table threads(id, rollout_path, updated_at); create table thread_attachments(thread_id);"
                         "create table thread_dynamic_tools(thread_id);"
                         "create table thread_spawn_edges(parent_thread_id, child_thread_id);")
        th = sqlite3.connect(f"{h}/thread_history_1.sqlite")
        th.executescript("".join(f"create table {t}(thread_id);" for t in (
            "thread_items", "thread_turns", "thread_realtime_items", "thread_history_projection_state")))
        for tid, ts in (("old", old), ("new", new), ("orphan", old)):
            f = f"{h}/sessions/2026/09/01/rollout-{tid}.jsonl"
            open(f, "w").write("{}\n")
            os.utime(f, (ts, ts))
            if tid != "orphan":
                with st:
                    st.execute("insert into threads values (?, ?, ?)", (tid, f, int(ts)))
                with th:
                    th.execute("insert into thread_items values (?)", (tid,))
        st.close(); th.close()
        run = lambda *a: subprocess.run([sys.executable, __file__, *a], capture_output=True, text=True,
                                        env={**os.environ, "CODEX_HOME": h})
        left = lambda: sorted(f for _, _, fs in os.walk(f"{h}/sessions") for f in fs)
        everything = left()
        for days in ("0", "-5", "6"):
            r = run("--days", days, "--apply")
            assert r.returncode == 2 and "--days must be >=" in r.stderr and left() == everything, (days, r.stderr)
        r = run()
        assert r.returncode == 0 and "dry-run: 1 threads, 2 rollout files" in r.stdout and "cutoff" in r.stdout
        assert left() == everything, "dry-run deleted something"
        r = run("--apply")
        assert r.returncode == 0 and left() == ["rollout-new.jsonl"], (r.stdout, r.stderr, left())
        ids = lambda db, q: [x for (x,) in sqlite3.connect(f"{h}/{db}").execute(q)]
        assert ids("state_5.sqlite", "select id from threads") == ["new"]
        assert ids("thread_history_1.sqlite", "select thread_id from thread_items") == ["new"]
    print("self-test passed")
    return 0


ap = argparse.ArgumentParser()
ap.add_argument("--days", type=int, default=21, help=f"retention in days (>= {MIN_DAYS})")
ap.add_argument("--apply", action="store_true", help="really delete (default: dry run)")
ap.add_argument("--self-test", action="store_true")
a = ap.parse_args()
if a.self_test:
    sys.exit(self_test())
if a.days < MIN_DAYS:
    ap.error(f"--days must be >= {MIN_DAYS} (got {a.days})")
H = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
cut = time.time() - a.days * 86400

def mtime(p):
    try: return os.stat(p).st_mtime
    except OSError: return 0.0

st = sqlite3.connect(f"{H}/state_5.sqlite", timeout=60)
th = sqlite3.connect(f"{H}/thread_history_1.sqlite", timeout=60)
old_ids, old_files = set(), set()
for tid, path, upd in st.execute("select id, rollout_path, updated_at from threads"):
    if max(mtime(path), upd or 0) < cut:
        old_ids.add(tid)
        if path and path.startswith(f"{H}/sessions/") and os.path.exists(path): old_files.add(path)
for dp, _, fs in os.walk(f"{H}/sessions"):          # orphan rollouts the DB never knew about
    for f in fs:
        p = os.path.join(dp, f)
        if f.startswith("rollout-") and f.endswith(".jsonl") and mtime(p) < cut: old_files.add(p)
size = sum(os.path.getsize(p) for p in old_files)
print(f"{'APPLY' if a.apply else 'dry-run'}: {len(old_ids)} threads, {len(old_files)} rollout files, {size/1e6:.0f} MB "
      f"older than {a.days}d (cutoff {time.strftime('%Y-%m-%d %H:%M', time.localtime(cut))}, {H})")
if not a.apply: sys.exit(0)

marks = lambda n: ",".join("?" * n)
ids = sorted(old_ids)
for i in range(0, len(ids), 500):                  # stay under SQLite's variable limit
    chunk = ids[i:i + 500]; m = marks(len(chunk))
    with st:
        for t in ("thread_attachments", "thread_dynamic_tools"): st.execute(f"delete from {t} where thread_id in ({m})", chunk)
        st.execute(f"delete from thread_spawn_edges where parent_thread_id in ({m}) or child_thread_id in ({m})", chunk + chunk)
        st.execute(f"delete from threads where id in ({m})", chunk)
    with th:
        for t in ("thread_items", "thread_turns", "thread_realtime_items", "thread_history_projection_state"):
            th.execute(f"delete from {t} where thread_id in ({m})", chunk)
for p in old_files:
    try: os.unlink(p)
    except OSError as e: print("skip", os.path.basename(p), e)
for dp, dn, _ in os.walk(f"{H}/sessions", topdown=False):   # drop emptied day folders
    if dp != f"{H}/sessions" and not os.listdir(dp): os.rmdir(dp)
for db in (st, th):
    try: db.execute("pragma wal_checkpoint(truncate)")
    except sqlite3.Error as e: print("checkpoint skipped:", e)
try: th.execute("vacuum")                               # frees the file; fails harmlessly if Codex holds it
except sqlite3.Error as e: print("vacuum skipped:", e)
print("done")
