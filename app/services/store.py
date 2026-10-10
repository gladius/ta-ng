"""Durable key->value store on the shared database (app.services.db), so any instance serves any step of a flow.
Keys are tuples, stored as the SHA-256 of their JSON. Values are plain data as gzipped JSON (tuples come back as lists),
never pickle: a pickled row runs code on load. No TTL: snapshots are pinned and paid proofs are never evicted."""
import gzip
import hashlib
import json
import datetime

from sqlalchemy import Table, Column, String, Text, LargeBinary, DateTime, select, delete

from app.services import db

_kv = Table(
    "kv", db.metadata,
    Column("k", String(64), primary_key=True),          # sha256 hex of the JSON key
    Column("keytext", Text, nullable=False),
    Column("v", LargeBinary, nullable=False),
    Column("created_at", DateTime, nullable=False),
)


def _ensure():
    """The engine, with the tables created on first use (again after a test's db.reset())."""
    return db.ready()


def _keytext(key):
    return json.dumps(key, default=str, sort_keys=True)

def _keyid(key):
    return hashlib.sha256(_keytext(key).encode("utf-8")).hexdigest()

def _jsonable(v):
    if isinstance(v, (set, frozenset)):
        return sorted(v, key=str)
    raise TypeError("store values must be plain data, got %s" % type(v).__name__)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)   # naive UTC — portable across backends


def _upsert(eng, k, keytext, blob, now):
    """Portable insert-or-replace: the one dialect-specific spot (both dialects speak ON CONFLICT)."""
    if eng.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    ins = insert(_kv).values(k=k, keytext=keytext, v=blob, created_at=now)
    return ins.on_conflict_do_update(index_elements=[_kv.c.k],
                                     set_={"v": ins.excluded.v, "created_at": ins.excluded.created_at})


def put(key, value):
    """Store `value` under `key` (overwrites); durable and visible to every instance."""
    eng = _ensure()
    blob = gzip.compress(json.dumps(value, default=_jsonable).encode("utf-8"))
    with eng.begin() as conn:
        conn.execute(_upsert(eng, _keyid(key), _keytext(key), blob, _now()))
    return value


def peek(key):
    """The stored value for `key`, or None. Never builds, so a page load can't start a paid run."""
    eng = _ensure()
    with eng.connect() as conn:
        row = conn.execute(select(_kv.c.v).where(_kv.c.k == _keyid(key))).first()
    if not row:
        return None
    try:
        return json.loads(gzip.decompress(row[0]))
    except ValueError:                                  # not JSON: reads as missing
        return None


def clear(key=None):
    """Bust one key (re-audit) or everything."""
    eng = _ensure()
    with eng.begin() as conn:
        conn.execute(delete(_kv) if key is None else delete(_kv).where(_kv.c.k == _keyid(key)))
