"""Test isolation for the durable store.

By DEFAULT the suite runs against a THROWAWAY temp SQLite file (zero install, fast) — it never touches the dev store
(.data/audit_store.db) or prod. To exercise the POSTGRES path (the dialect-specific upsert, bytea blob, create_all),
point TEST_DATABASE_URL at a DEDICATED throwaway Postgres database and run the same suite:

    pip install "psycopg[binary]"
    TEST_DATABASE_URL="postgresql+psycopg://user:pass@host:5432/ta_test" pytest -q

WARNING: use a DEDICATED test database — teardown drops the store's tables. Never point this at prod.
Autouse + session-scoped, so any test that reaches the store is isolated automatically on either backend."""
import os
import tempfile

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolate_store():
    from app.services import db
    from app.services import store  # noqa: F401  ensure the kv table is registered on db.metadata before drop_all

    test_url = os.environ.get("TEST_DATABASE_URL")
    tmp_path = None
    if test_url:                                         # run the suite against a real (throwaway) DB, e.g. Postgres
        os.environ["DATABASE_URL"] = test_url
    else:                                                # default: an isolated temp SQLite file
        fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="audit_test_")
        os.close(fd)
        os.environ["DATABASE_URL"] = "sqlite:///" + tmp_path.replace(os.sep, "/")
    db.reset()                                           # rebuild the engine against the chosen DB
    yield
    try:
        db.metadata.drop_all(db.engine())                # leave the DB clean on ANY backend (drops our tables only)
    except Exception:
        pass
    db.reset()                                           # release handles before deleting the temp file
    if tmp_path:
        for p in (tmp_path, tmp_path + "-wal", tmp_path + "-shm"):   # SQLite WAL sidecars
            try:
                os.remove(p)
            except OSError:
                pass
