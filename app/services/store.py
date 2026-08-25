"""Computed-view + proof store — a durable key->value store, backed by SQLAlchemy (SQLite in dev, Postgres in prod;
chosen by DATABASE_URL — see app.services.db).

Same put/peek/clear interface as the old in-memory dict, so every caller (snapshot, prove, comprehend, the routes)
is UNCHANGED — but the state now lives in a SHARED database, so the app scales HORIZONTALLY: any Cloud Run instance
serves any step of a user's flow (page-load -> prove -> the after-prove re-fetch) instead of the whole flow being
pinned to one process. That is the one change that removes the `--max-instances=1` constraint.

  key   -> a tuple like ("snapshot", id) or prove.proof_key(...). Encoded to a stable JSON string, then SHA-256'd to
           a bounded 64-char primary key (the readable form is kept in `keytext` for debugging).
  value -> ANY Python object the caller stored (a graph, a proof dict). pickled + gzipped into a BLOB, so it round-
           trips IDENTICALLY — a true drop-in for the in-memory dict (no JSON coercion of tuples/sets/etc.).

No TTL: a snapshot is pinned per report and a proof is FROZEN — a re-audit mints a NEW key. Retention/pruning of
un-audited scratch snapshots is a separate concern (a cleanup job), so paid proofs are never silently evicted. The
ONE backend-specific line is the upsert (ON CONFLICT), isolated in _upsert().
"""
import gzip
import hashlib
import json
import pickle
import datetime

from sqlalchemy import Table, Column, String, Text, LargeBinary, DateTime, select, delete

from app.services import db

# Registered on the shared db.metadata; the store create_all's it on first use (see _ensure). Phase B's entity
# tables (snapshot / call_site / proof) will register here too and be created the same way.
_kv = Table(
    "kv", db.metadata,
    Column("k", String(64), primary_key=True),          # sha256 hex of the encoded key — bounded, index-safe
    Column("keytext", Text, nullable=False),            # the readable key, for debugging / inspection
    Column("v", LargeBinary, nullable=False),           # pickled + gzipped value
    Column("created_at", DateTime, nullable=False),
)


_ready_engine = None                                    # the engine we've create_all'd against (re-runs after db.reset)


def _ensure():
    """Return the engine, creating the store's tables on first use if they're missing — create_all is CREATE TABLE IF
    NOT EXISTS for everything on db.metadata: idempotent, and a no-op once the tables exist. Seamless: the first store
    call in a process initializes the schema, so there's no migration step and no init endpoint. Re-runs when the
    engine changes (a test called db.reset())."""
    global _ready_engine
    eng = db.engine()
    if _ready_engine is not eng:
        db.metadata.create_all(eng)
        _ready_engine = eng
    return eng


def _keytext(key):
    return json.dumps(key, default=str, sort_keys=True)     # ("snapshot","id") -> '["snapshot", "id"]', stable

def _keyid(key):
    return hashlib.sha256(_keytext(key).encode("utf-8")).hexdigest()

def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)   # naive UTC — portable across backends


def _upsert(eng, k, keytext, blob, now):
    """Portable insert-or-replace. The ONE dialect-specific import, isolated here (both dialects speak ON CONFLICT)."""
    if eng.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    ins = insert(_kv).values(k=k, keytext=keytext, v=blob, created_at=now)
    return ins.on_conflict_do_update(index_elements=[_kv.c.k],
                                     set_={"v": ins.excluded.v, "created_at": ins.excluded.created_at})


def put(key, value):
    """Store `value` under `key` (overwrites). Durable — survives process restart and is visible to every instance."""
    eng = _ensure()
    blob = gzip.compress(pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL))
    with eng.begin() as conn:
        conn.execute(_upsert(eng, _keyid(key), _keytext(key), blob, _now()))
    return value


def peek(key):
    """The stored value for `key`, or None — never builds (a page GET shows a paid artifact only if already run)."""
    eng = _ensure()
    with eng.connect() as conn:
        row = conn.execute(select(_kv.c.v).where(_kv.c.k == _keyid(key))).first()
    return pickle.loads(gzip.decompress(row[0])) if row else None


def clear(key=None):
    """Bust one key (re-audit) or everything."""
    eng = _ensure()
    with eng.begin() as conn:
        conn.execute(delete(_kv) if key is None else delete(_kv).where(_kv.c.k == _keyid(key)))
