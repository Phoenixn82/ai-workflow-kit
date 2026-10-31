"""hook_event.py -- IMPORTED helper, never registered as a hook itself.

Python equivalent of hook-event.sh, for python-based hooks. A hook imports
this, does its own complete stdin/stdout/exit work first, and only then
calls log_hook_event(...) -- so this never sits in the stdin/stdout path and
a secret-firewall deny JSON is byte-for-byte untouched (the log write goes
only to the log file, never to stdout/stderr). Logs metadata only: never
hook payloads (tool inputs, file contents, transcript text).

Rotation (audit 2026-10-12, reports/automations-gc.md: rotate at 5 MB): once the log
reaches ROTATE_BYTES, the writer that notices renames it to events-<UTC>-<pid>-<ns>.jsonl and
gzips it in place. Nothing is deleted (automations never delete); old .gz files stay in
logs/ for the user or retain.py. hook-event.sh (bash hooks) shares the file and relies on
this rotation, since the python secret-firewall hooks write on every tool call.
  python3 hook_event.py --self-test
"""
import gzip
import json
import os
import shutil
import sys
import time

HOOK_EVENT_LOG = os.environ.get(
    "HOOK_EVENT_LOG",
    os.path.join(os.path.expanduser("~"), ".claude", "hooks", "logs", "events.jsonl"),
)


ROTATE_BYTES = 5 * 1024 * 1024


def _rotate():
    try:
        st = os.stat(HOOK_EVENT_LOG)
        if st.st_size < ROTATE_BYTES:
            return
        # unique per rotation (UTC second, pid, nanoseconds): the gz rename below must never replace an older archive
        base = os.path.join(os.path.dirname(HOOK_EVENT_LOG), "events-%s-%d-%09d.jsonl"
                            % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), os.getpid(), time.time_ns() % 10**9))
        if os.stat(HOOK_EVENT_LOG).st_ino != st.st_ino:
            return  # another hook rotated it a moment ago
        os.rename(HOOK_EVENT_LOG, base)  # atomic; writers that still hold the old fd finish into `base`
        with open(base, "rb") as src, gzip.open(base + ".gz.tmp", "wb") as dst:
            shutil.copyfileobj(src, dst)
            time.sleep(0.05)  # a writer that opened the log just before the rename appends here: take its line too
            shutil.copyfileobj(src, dst)
        os.rename(base + ".gz.tmp", base + ".gz")
        os.unlink(base)  # its bytes now live in base.gz
    except Exception:
        pass  # a failed rotation leaves the log (or the renamed plain file) in place


def log_hook_event(hook, event, exit_code, start_ms, outcome, session_id="unknown"):
    now_ms = int(time.time() * 1000)
    ts_utc = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + (
        ".%03dZ" % (now_ms % 1000)
    )
    rec = {
        "ts_utc": ts_utc,
        "session_id": session_id or "unknown",
        "hook": hook,
        "event": event,
        "exit": exit_code,
        "ms": now_ms - start_ms,
        "outcome": outcome,
    }
    try:
        os.makedirs(os.path.dirname(HOOK_EVENT_LOG), exist_ok=True)
        with open(HOOK_EVENT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        _rotate()
    except Exception:
        pass  # evidence logging must never break the hook it's attached to


def _self_test():
    import tempfile
    global HOOK_EVENT_LOG, ROTATE_BYTES
    saved = HOOK_EVENT_LOG, ROTATE_BYTES
    with tempfile.TemporaryDirectory() as d:
        try:
            HOOK_EVENT_LOG = os.path.join(d, "events.jsonl")
            log_hook_event("t", "PreToolUse", 0, int(time.time() * 1000), "allow", "s0")
            ROTATE_BYTES = os.path.getsize(HOOK_EVENT_LOG) * 2 + 1  # rotates on the 3rd row
            log_hook_event("t", "PreToolUse", 0, int(time.time() * 1000), "allow", "s1")
            assert not [f for f in os.listdir(d) if f.endswith(".gz")], os.listdir(d)
            log_hook_event("t", "PreToolUse", 0, int(time.time() * 1000), "deny", "s2")
            gz = [f for f in os.listdir(d) if f.endswith(".jsonl.gz")]
            assert len(gz) == 1 and os.listdir(d) == gz, os.listdir(d)  # no plain copy or .tmp left behind
            rows = [json.loads(x) for x in gzip.open(os.path.join(d, gz[0]), "rt")]
            assert [r["session_id"] for r in rows] == ["s0", "s1", "s2"], rows  # nothing lost
            log_hook_event("t", "PreToolUse", 0, int(time.time() * 1000), "allow", "s9")
            assert sum(1 for _ in open(HOOK_EVENT_LOG)) == 1
            ROTATE_BYTES = 1  # three rotations by one process inside one second: no archive may overwrite another
            for sid in ("s10", "s11", "s12"):
                log_hook_event("t", "PreToolUse", 0, int(time.time() * 1000), "allow", sid)
            gz = [f for f in os.listdir(d) if f.endswith(".jsonl.gz")]
            rows = sorted(json.loads(x)["session_id"] for f in gz for x in gzip.open(os.path.join(d, f), "rt"))
            assert len(gz) == 4 and rows == sorted(["s0", "s1", "s2", "s9", "s10", "s11", "s12"]), (gz, rows)
        finally:
            HOOK_EVENT_LOG, ROTATE_BYTES = saved
    print("hook_event self-test: ok")


if __name__ == "__main__" and sys.argv[1:] == ["--self-test"]:
    _self_test()
