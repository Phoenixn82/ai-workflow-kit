"""Small SQLite ledger for calls, policy state, and future feed tables."""

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from .config import home

MIGRATIONS = [
    (
        1,
        "CREATE TABLE calls (id INTEGER PRIMARY KEY, class TEXT, lane TEXT, model TEXT, provider TEXT, tier TEXT, caller TEXT, result TEXT, escalated INTEGER DEFAULT 0, latency REAL, created REAL NOT NULL)",
    ),
    (
        2,
        "CREATE TABLE admission (class TEXT, chain TEXT, tier TEXT, admitted INTEGER, evidence TEXT, since TEXT, reason TEXT, source TEXT, PRIMARY KEY(class, chain, tier))",
    ),
    (
        3,
        "CREATE TABLE escalations (id INTEGER PRIMARY KEY, dedup_hash TEXT UNIQUE, class TEXT, tier TEXT, caller TEXT, reason TEXT, payload TEXT, created REAL, state TEXT DEFAULT 'queued')",
    ),
    (
        4,
        "CREATE TABLE outbox (id INTEGER PRIMARY KEY, feed TEXT, diff_key TEXT, content_hash TEXT, sink TEXT, payload TEXT, state TEXT DEFAULT 'pending', UNIQUE(feed, diff_key, content_hash, sink))",
    ),
    (
        5,
        "CREATE TABLE snapshots (id INTEGER PRIMARY KEY, name TEXT UNIQUE, payload TEXT, created REAL)",
    ),
    (
        6,
        "CREATE TABLE callers (id INTEGER PRIMARY KEY, caller TEXT, token_hash TEXT, max_tier TEXT, revoked INTEGER DEFAULT 0, created REAL)",
    ),
    (7, "CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT)"),
]


class Ledger:
    def __init__(self, path=None):
        self.path = Path(path or home() / "lanes.db")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
        )
        self._migrate()
        self.db.commit()
        os.chmod(self.path, 0o600)

    def _migrate(self):
        current = (
            self.db.execute("SELECT max(version) FROM schema_version").fetchone()[0]
            or 0
        )
        for version, sql in MIGRATIONS[current:]:
            self.db.execute("BEGIN")
            self.db.execute(sql)
            self.db.execute("INSERT INTO schema_version VALUES (?)", (version,))
            self.db.commit()

    def admission(self, cls, tier, chain):
        return self.db.execute(
            "SELECT * FROM admission WHERE class=? AND tier=? AND chain=?",
            (cls, tier, chain),
        ).fetchone()

    def set_admission(
        self, cls, chain, tier, admitted, reason, evidence="", source="manual"
    ):
        self.db.execute(
            "INSERT OR REPLACE INTO admission VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cls,
                chain,
                tier,
                int(admitted),
                evidence,
                time.strftime("%Y-%m-%d"),
                reason,
                source,
            ),
        )
        self.db.commit()

    def codex_count(self):
        return self.db.execute(
            "SELECT count(*) FROM calls WHERE escalated=1 AND created>=?",
            (time.time() - 86400,),
        ).fetchone()[0]

    def call(self, **values):
        keys = (
            "class_",
            "lane",
            "model",
            "provider",
            "tier",
            "caller",
            "result",
            "escalated",
            "latency",
        )
        self.db.execute(
            "INSERT INTO calls(class,lane,model,provider,tier,caller,result,escalated,latency,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
            tuple(values.get(k) for k in keys) + (time.time(),),
        )
        self.db.commit()

    def escalate(self, cls, tier, caller, reason, payload=None):
        private = tier in {"private", "business-pii"}
        redacted = None if private else json.dumps(redact(payload or ""))
        digest = hashlib.sha256(
            f"{cls}\0{tier}\0{caller}\0{reason}\0{redacted or ''}".encode()
        ).hexdigest()
        self.db.execute(
            "INSERT OR IGNORE INTO escalations(dedup_hash,class,tier,caller,reason,payload,created) VALUES(?,?,?,?,?,?,?)",
            (digest, cls, tier, caller, reason, redacted, time.time()),
        )
        self.db.commit()
        return digest

    def prune(self, days=30):
        self.db.execute(
            "DELETE FROM calls WHERE created<?", (time.time() - days * 86400,)
        )
        self.db.commit()

    def close(self):
        self.db.close()


def redact(value, names=()):
    text = json.dumps(value) if not isinstance(value, str) else value
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[REDACTED_EMAIL]", text)
    text = re.sub(r"(?<!\d)(?:\+?\d[\d ()-]{8,}\d)(?!\d)", "[REDACTED_PHONE]", text)
    for name in names:
        text = re.sub(re.escape(name), "[REDACTED_NAME]", text, flags=re.IGNORECASE)
    return json.loads(text) if not isinstance(value, str) else text
