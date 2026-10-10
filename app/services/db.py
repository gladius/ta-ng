"""The app's one database engine: Postgres when DATABASE_URL is set (production, shared by every instance), else a
local SQLite file in WAL mode (dev). Same code either way; the engine is built lazily on first use."""
import os
import threading

from sqlalchemy import create_engine, event, exc, inspect, MetaData, text

import credentials

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DATA_DIR = os.path.join(_REPO_ROOT, ".data")
_DEFAULT_SQLITE = "sqlite:///" + os.path.join(_DATA_DIR, "audit_store.db")

# Every table registers here; ready() creates missing ones on first use.
metadata = MetaData()

_engine = None
_tables_for = None          # (engine, tables registered) last created: a module imported later adds tables
_tables_lock = threading.Lock()


def url():
    """The active DATABASE_URL (or the dev SQLite default). It holds credentials: don't log it."""
    u = credentials.get_config("DATABASE_URL")
    if not u and os.environ.get("K_SERVICE"):                    # on Cloud Run a local SQLite file is lost per instance
        raise RuntimeError("DATABASE_URL is not set: Cloud Run needs a shared Postgres")
    return u or _DEFAULT_SQLITE


def engine():
    global _engine
    if _engine is None:
        _engine = _build(url())
    return _engine


def ready():
    """The engine, with every table on `metadata` created if missing, by one thread per process. If another instance
    creates the same tables at that moment, Postgres rejects the second CREATE and the retry finds them made."""
    global _tables_for
    eng = engine()
    key = (eng, len(metadata.tables))
    if _tables_for != key:
        with _tables_lock:
            if _tables_for != key:
                try:
                    _create(eng)
                except exc.DBAPIError:
                    _create(eng)
                _tables_for = key
    return eng


def _create(eng):
    """Missing tables, then missing nullable columns. Never alters or drops: see tools/migrate_validation_schema.py."""
    metadata.create_all(eng)
    insp = inspect(eng)
    with eng.begin() as c:
        for t in metadata.sorted_tables:
            have = {col["name"] for col in insp.get_columns(t.name)}
            for col in t.columns:
                if col.name not in have and col.nullable and not col.primary_key:
                    c.execute(text("ALTER TABLE %s ADD COLUMN %s %s" % (t.name, col.name,
                                                                       col.type.compile(eng.dialect))))


def _ensure_sqlite_dir(u):
    path = u.split("sqlite:///", 1)[-1]
    if path and path != ":memory:":
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)


def _build(u):
    sqlite = u.startswith("sqlite")
    kw = {"pool_pre_ping": True, "future": True, "hide_parameters": True}   # no prompt text in error logs
    if sqlite:
        _ensure_sqlite_dir(u)
        kw["connect_args"] = {"check_same_thread": False}        # one file, shared across the prove thread-pool
    else:                                                        # small pools: many instances x big pools exhaust PG
        kw["pool_size"] = int(credentials.get_config("DB_POOL_SIZE", "5"))
        kw["max_overflow"] = int(credentials.get_config("DB_MAX_OVERFLOW", "5"))
    eng = create_engine(u, **kw)
    if sqlite:
        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_conn, _rec):                          # WAL = concurrent readers + one writer, no hard lock
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")              # wait, don't error, if another thread holds the write
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()
    return eng


def reset():
    """Drop the cached engine (tests: point DATABASE_URL at a temp DB, then call this)."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def health():
    """Live DB health for /healthz/db: {ok, backend, target, entries, last_write}. `target` never holds credentials."""
    from sqlalchemy import text
    eng = engine()
    u = eng.url
    if eng.dialect.name == "sqlite":
        target = u.database or "(memory)"
    else:
        target = "%s%s/%s" % (u.host or "?", (":%s" % u.port) if u.port else "", u.database or "?")
    out = {"ok": False, "backend": eng.dialect.name, "target": target, "entries": None, "last_write": None}
    try:
        from app.services import store
        store._ensure()
        with eng.connect() as c:
            row = c.execute(text("SELECT count(*), max(created_at) FROM kv")).first()
        out["ok"] = True
        out["entries"] = int(row[0]) if row and row[0] is not None else 0
        lw = row[1] if row else None                            # raw SQL: SQLite returns a str, Postgres a datetime
        out["last_write"] = lw.isoformat() if hasattr(lw, "isoformat") else (str(lw) if lw is not None else None)
    except Exception as e:                                       # report the error type only, never its details
        out["error"] = type(e).__name__
    return out
