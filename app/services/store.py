"""Durable computed-view + proof store — SQLite-backed so it is SHARED across workers and survives restarts.

Why SQLite: the snapshot and the frozen proof must be readable by ANY worker (page-load and prove can land on
different uvicorn/gunicorn workers). An in-process dict can't do that. SQLite (stdlib, no server) is a shared,
worker-safe key->JSON store; WAL mode allows concurrent readers with a single writer. No TTL — what we store is
immutable (a pinned snapshot) or frozen (a completed proof); a re-audit mints a NEW key rather than mutating one.
Bounded by evicting the oldest rows so the file stays small.

Keys may be strings or tuples (e.g. a proof_key) — tuples are serialized to a stable JSON string. Values must be
JSON-serializable (graphs of contract traces, proof dicts — both are).
"""
import json
import os
import sqlite3
import threading
import time

_DB = os.environ.get("AUDIT_STORE_DB",
                     os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                  ".audit_store.sqlite"))
_MAX_ROWS = int(os.environ.get("AUDIT_STORE_MAX", "200"))    # evict oldest beyond this to bound file size
_LOCK = threading.Lock()
_conn = None


def _db():
    global _conn
    if _conn is None:
        with _LOCK:
            if _conn is None:
                c = sqlite3.connect(_DB, check_same_thread=False)
                c.execute("PRAGMA journal_mode=WAL")            # concurrent readers + one writer across workers
                c.execute("PRAGMA busy_timeout=5000")           # wait out a concurrent writer instead of erroring
                c.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL, ts REAL)")
                c.commit()
                _conn = c
    return _conn


def _sk(key):
    return key if isinstance(key, str) else json.dumps(key)   # tuple/list keys -> stable string


def put(key, value):
    """Store `value` (JSON-serializable) under `key`. Overwrites; bounds the table to the most-recent MAX rows."""
    k, db = _sk(key), _db()
    with _LOCK:
        db.execute("INSERT OR REPLACE INTO kv (k, v, ts) VALUES (?, ?, ?)", (k, json.dumps(value), time.time()))
        db.execute("DELETE FROM kv WHERE k NOT IN (SELECT k FROM kv ORDER BY ts DESC LIMIT ?)", (_MAX_ROWS,))
        db.commit()
    return value


def peek(key):
    """The stored value for `key`, or None. Never builds — a page GET shows a paid artifact only if already run."""
    row = _db().execute("SELECT v FROM kv WHERE k=?", (_sk(key),)).fetchone()
    return json.loads(row[0]) if row else None


def clear(key=None):
    """Bust one key (re-audit) or everything."""
    k, db = (None if key is None else _sk(key)), _db()
    with _LOCK:
        db.execute("DELETE FROM kv") if key is None else db.execute("DELETE FROM kv WHERE k=?", (k,))
        db.commit()
