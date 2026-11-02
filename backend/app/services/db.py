"""The app's one database engine: Postgres from DATABASE_URL (production, shared by every instance); with APP_ENV=dev
and no DATABASE_URL, a local SQLite file in WAL mode. Same code either way; the engine is built lazily on first use."""
import os
import threading

from sqlalchemy import create_engine, event, inspect, MetaData, text

import credentials

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DATA_DIR = os.path.join(_REPO_ROOT, ".data")
_DEFAULT_SQLITE = "sqlite:///" + os.path.join(_DATA_DIR, "audit_store.db")

# Every table registers here. The schema itself comes from the migrations in alembic/versions, which ready() applies.
metadata = MetaData()
_MIGRATIONS = os.path.join(_REPO_ROOT, "alembic")
_LOCK = 74120501            # Postgres advisory lock id: one instance migrates, the others wait for it

_engine = None
_ready_for = None           # the engine whose database this process has migrated
_ready_lock = threading.Lock()


def url():
    """The active DATABASE_URL (or the dev SQLite default). It holds credentials: don't log it."""
    u = credentials.get_config("DATABASE_URL")
    if not u and credentials.get_config("APP_ENV") != "dev":     # a local SQLite file is lost with its container
        raise RuntimeError("DATABASE_URL is not set (only APP_ENV=dev may use SQLite)")
    return u or _DEFAULT_SQLITE


def engine():
    global _engine
    if _engine is None:
        _engine = _build(url())
    return _engine


def load_tables():
    """Import every module that defines a table, so `metadata` holds the whole schema."""
    import core.identity.store, core.schedule, core.tasks, modules.models.audits, modules.quality, validation.jobs  # noqa: F401
    return metadata


def ready():
    """The engine, its database migrated to the latest version, once per process."""
    global _ready_for
    eng = engine()
    if _ready_for is not eng:
        with _ready_lock:
            if _ready_for is not eng:
                migrate(eng)
                _ready_for = eng
    return eng


def migrate(eng):
    """Apply the migrations not yet applied, in one transaction. On Postgres an advisory lock makes every other instance
    wait, then find nothing left to do. A database with our tables but no migration record is refused: it predates
    migrations, and its schema is not known."""
    from alembic import command
    from alembic.config import Config
    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS)
    with eng.begin() as c:
        if c.dialect.name == "postgresql":
            c.execute(text("SELECT pg_advisory_xact_lock(%d)" % _LOCK))
        have = set(inspect(c).get_table_names())
        old = sorted(have & set(load_tables().tables))
        if "alembic_version" not in have and old:
            raise RuntimeError("the database has tables from before migrations (%s); start from an empty database"
                               % ", ".join(old))
        cfg.attributes["connection"] = c
        command.upgrade(cfg, "head")


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
        with eng.connect() as c:
            c.execute(text("SELECT 1")).first()
        out["ok"] = True
    except Exception as e:                                       # report the error type only, never its details
        out["error"] = type(e).__name__
    return out
