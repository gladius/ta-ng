"""The ONE database engine for the whole app — backend chosen by DATABASE_URL, same code either way.

  dev (restricted / no Postgres): defaults to a local SQLite FILE — stdlib sqlite3, ZERO install. WAL mode so the
      prove thread-pool's concurrent writes don't lock each other.
  prod: set DATABASE_URL=postgresql+psycopg://user:pass@host/db and the SAME code runs on Postgres — which is what
      lets the app scale horizontally (many Cloud Run instances sharing one store) instead of pinning to one.

DATABASE_URL resolves through credentials.get_config (process env first, .env fallback) so it lives with the rest of
the app's config. The engine is built once, lazily, on first use. `reset()` exists for tests (point DATABASE_URL at a
temp DB, reset, run). Nothing backend-specific lives here except the SQLite pragmas; the one upsert dialect branch is
isolated in store.py.
"""
import os

from sqlalchemy import create_engine, event, MetaData

import credentials

# Dev store lives under repo-root/.data/ (gitignored, dev convenience only — prod is Postgres via DATABASE_URL).
# db.py is app/services/db.py -> up 3 = repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DATA_DIR = os.path.join(_REPO_ROOT, ".data")
_DEFAULT_SQLITE = "sqlite:///" + os.path.join(_DATA_DIR, "audit_store.db")

# The ONE metadata every table registers on (store's kv today; Phase B's snapshot/proof tables next). The store
# create_all's it on first use (CREATE TABLE IF NOT EXISTS, idempotent) — see app/services/store.py.
metadata = MetaData()

_engine = None


def url():
    """The active DATABASE_URL (or the dev SQLite default). Never logged with credentials intact by callers."""
    return credentials.get_config("DATABASE_URL", _DEFAULT_SQLITE)


def engine():
    """The process-wide SQLAlchemy engine, built once on first use."""
    global _engine
    if _engine is None:
        _engine = _build(url())
    return _engine


def is_sqlite():
    return engine().dialect.name == "sqlite"


def _ensure_sqlite_dir(u):
    """SQLite won't create a missing parent directory — make it (handles the .data/ default and any custom path)."""
    path = u.split("sqlite:///", 1)[-1]
    if path and path != ":memory:":
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)


def _build(u):
    sqlite = u.startswith("sqlite")
    kw = {"pool_pre_ping": True, "future": True}
    if sqlite:
        _ensure_sqlite_dir(u)                                    # create .data/ (or any custom dir) before opening
        kw["connect_args"] = {"check_same_thread": False}        # one file, shared across the prove thread-pool
    else:                                                        # Postgres on Cloud Run: keep pools SMALL — many
        kw["pool_size"] = int(credentials.get_config("DB_POOL_SIZE", "5"))       # instances x big pools exhausts PG
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
    """Drop the cached engine (tests: set DATABASE_URL to a temp DB, call reset(), then use the store)."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None
