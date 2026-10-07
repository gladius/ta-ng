"""The durable KV store (app.services.store) on SQLite — values round-trip as JSON (never pickle), overwrite/clear
work, and a value SURVIVES an engine reset (i.e.
it's on disk / shareable across instances, not in a per-process dict). conftest points DATABASE_URL at a temp SQLite
file, so this runs with zero install and never touches the dev or prod store."""
from app.services import store, db


def test_roundtrip_is_plain_json():
    key = ("snapshot", "abc123")
    store.put(key, {"nodes": [{"k": "n1", "avg_in": 4000}], "t": (1, 2, 3), "s": {"y", "x"}})
    assert store.peek(key) == {"nodes": [{"k": "n1", "avg_in": 4000}], "t": [1, 2, 3], "s": ["x", "y"]}


def test_pickled_row_reads_as_missing_and_never_runs():
    import gzip, pickle
    from app.services.store import _kv, _keyid, _keytext, _upsert, _now, _ensure
    eng, key = _ensure(), ("snapshot", "pickled")
    with eng.begin() as conn:                                          # a row as the pickle-era store wrote it
        conn.execute(_upsert(eng, _keyid(key), _keytext(key), gzip.compress(pickle.dumps({"a": 1})), _now()))
    assert store.peek(key) is None


def test_overwrite_then_clear_one():
    k = ("src", "ws", "proj", "proof", "snap1")                        # prove.proof_key-shaped tuple
    store.put(k, {"total": 1})
    store.put(k, {"total": 2})                                         # upsert (ON CONFLICT), not a duplicate row
    assert store.peek(k) == {"total": 2}
    store.clear(k)
    assert store.peek(k) is None


def test_missing_key_returns_none():
    assert store.peek(("nope", "never-stored")) is None


def test_durable_across_engine_reset():
    k = ("snapshot", "persist-me")
    store.put(k, {"kept": True})
    db.reset()                                                         # simulate another instance opening the same DB
    assert store.peek(k) == {"kept": True}                            # on disk, not a per-process dict -> horizontal


def test_clear_all():
    store.put(("a", "1"), 1)
    store.put(("b", "2"), 2)
    store.clear()
    assert store.peek(("a", "1")) is None and store.peek(("b", "2")) is None
