"""Test isolation for the durable store: point DATABASE_URL at a THROWAWAY temp SQLite file for the whole session, so
the suite never touches the dev store (.data/audit_store.db) or prod. Zero install — stdlib sqlite. Autouse, so
any test that reaches the store (now or later) is isolated automatically."""
import os
import tempfile

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolate_store():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="audit_test_")
    os.close(fd)
    os.environ["DATABASE_URL"] = "sqlite:///" + path.replace(os.sep, "/")
    from app.services import db
    db.reset()                                       # rebuild the engine against the temp DB (store create_all's lazily)
    yield
    db.reset()                                       # release the file handle before deleting
    for p in (path, path + "-wal", path + "-shm"):   # SQLite WAL sidecars
        try:
            os.remove(p)
        except OSError:
            pass
