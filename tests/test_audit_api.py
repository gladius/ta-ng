"""The public API (model audits of sent requests), its jobs, workers and pages. Offline: the engine is replaced and workers are driven by hand
(`_work`); the tables live in the test DB (SQLite by default, Postgres when TEST_DATABASE_URL is set)."""
import pytest
from fastapi.testclient import TestClient

from app.web.server import app
from modules.models import audits
from validation import jobs

KEY = {"X-API-Key": "test-key"}


def _item(i, candidates=(("router-a", "gpt-5.4-mini"), ("router-b", "gpt-5.4-nano"))):
    return {"id": "req-%d" % i,
            "request": {"model": "gpt-5.4", "messages": [{"role": "system", "content": "Billing agent."},
                                                         {"role": "user", "content": "refund order %d" % i}],
                        "max_tokens": 256},
            "response": {"id": "c%d" % i, "model": "gpt-5.4", "choices": [{"message": {"content": "Refunded."}}],
                         "usage": {"prompt_tokens": 40, "completion_tokens": 5}},
            "candidates": [{"label": l, "model": m} for l, m in candidates]}


def _fake_engine(items, spec, run=None):
    """One engine result per item: router-a within, router-b outside; every item costs $1."""
    out = []
    for it in items:
        cands = []                                     # the original's fresh runs cost $0.01 each, at 300 ms
        for c in it.candidates:
            ok = c.label == "router-a"
            cands.append({"label": c.label, "model": c.model, "kept": 5 if ok else 1, "of": 5,
                          "verdict": "within" if ok else "outside", "reason": "r", "latency_ms": 100 if ok else 50,
                          "cost_usd": 0.002 if ok else 0.001,
                          "runs": [{"output": "o", "model": c.model, "latency_ms": 100, "cost_usd": 0.002, "kept": ok,
                                    "votes": "3/3", "reason": "r", "judges": [], "error": None, "degraded": False}]})
        out.append({"id": it.id, "model": it.model, "spend_usd": 1.0, "candidates": cands,
                    "reference": {"agree": 5, "of": 5, "runs": [
                        {"output": it.recorded, "recorded": True, "agrees": True, "reason": "", "latency_ms": None,
                         "cost_usd": 0.01},
                        {"output": "x", "recorded": False, "agrees": True, "reason": "", "latency_ms": 300,
                         "cost_usd": 0.01, "model": it.model}]}})
    return out


@pytest.fixture(autouse=True)
def _empty_queue():
    """Each test starts with no jobs. On a slow database (Postgres over a WAN) a whole run takes minutes, so an
    earlier test's unfinished group goes stale and would be claimable in a later test."""
    with jobs._engine().begin() as c:
        for t in (jobs._callbacks, jobs._cells, jobs._items, jobs._jobs, audits.audits):
            c.execute(t.delete())


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("VALIDATION_API_KEY", "test-key")
    monkeypatch.setattr(jobs, "validate_many", _fake_engine)
    return TestClient(app)                                                # no `with`: no in-process workers


def _work(worker="w1"):
    """Act as a worker until nothing is left to claim."""
    while (got := jobs.claim(worker)):
        jobs.run_group(*got)


def _audit(payload):
    """The old batch shape ({items: [...]}) as an audit of sent requests."""
    out = dict(payload)
    if "items" in out:
        out["requests"] = out.pop("items")
    return out


def _post(client, payload):
    r = client.post("/api/audit/models", headers=KEY, json=_audit(payload))
    assert r.status_code == 202, r.text
    return r.json()["audits"][0]["id"]


def test_key_is_required(client, monkeypatch):
    assert client.get("/api/audit/models").status_code == 401
    assert client.get("/api/audit/models", headers={"X-API-Key": "wrong"}).status_code == 401
    monkeypatch.setenv("VALIDATION_API_KEY", "")
    assert client.get("/api/audit/models", headers=KEY).status_code == 503          # not configured -> closed


def test_bad_batches_are_refused_whole(client):
    assert client.post("/api/audit/models", headers=KEY, json=_audit({"items": []})).status_code == 422
    assert client.post("/api/audit/models", headers=KEY, json=_audit({"items": [_item(i) for i in range(1001)]})).status_code == 422
    no_cands = dict(_item(1), candidates=[])
    assert client.post("/api/audit/models", headers=KEY, json=_audit({"items": [no_cands]})).status_code == 422
    dup = _item(1, candidates=(("a", "m1"), ("a", "m2")))
    no_user = dict(_item(2), request={"model": "gpt-5.4", "messages": [{"role": "system", "content": "s"}]})
    reserved = _item(3, candidates=(("original", "m1"),))
    r = client.post("/api/audit/models", headers=KEY, json=_audit({"items": [dup, no_user, reserved]}))
    assert r.status_code == 422
    assert r.json()["detail"] == ["req-1: candidate labels must be unique", "req-2: request.messages needs a user message",
                                  "req-3: candidate label 'original' is reserved for the original model"]


def test_job_runs_and_reports(client):
    job_id = _post(client, {"items": [_item(1), _item(2)], "judge_model": "claude-sonnet-5-5"})
    assert client.get("/api/audit/models/" + job_id, headers=KEY).json()["status"] == "queued"
    _work()
    j = client.get("/api/audit/models/" + job_id, headers=KEY).json()
    assert (j["status"], j["items_done"], j["items_total"], j["judge_model"]) == ("done", 2, 2, "claude-sonnet-5-5")
    assert j["spend_usd"] == 2.0 and "results" not in j and "items" not in j and j["routing"] == []
    by = {s["label"]: s for s in j["summary"]}
    assert (by["router-a"]["within"], by["router-b"]["outside"]) == (2, 2)
    assert by["router-a"]["avg_latency_ms"] == 100 and by["original"]["avg_latency_ms"] == 300
    assert (by["router-a"]["avg_fit_rate"], by["router-b"]["avg_fit_rate"], by["original"]["avg_consistency"]) == (1.0, 0.2, 1.0)
    res = client.get(j["results_url"], headers=KEY).json()
    assert res["total"] == 2 and [r["id"] for r in res["results"]] == ["req-1", "req-2"]
    assert job_id in [x["id"] for x in client.get("/api/audit/models", headers=KEY).json()]
    assert by["router-a"]["within_rate"] == 1.0 and by["router-a"]["cost_vs_original_pct"] == -80
    assert client.get("/api/audit/models/%s/results?show=split" % job_id, headers=KEY).json()["total"] == 2


def test_one_request_and_the_downloads(client):
    job_id = _post(client, {"items": [_item(1), _item(2)]})
    _work()
    assert client.get("/", follow_redirects=False).headers["location"] == "/app/"   # the web UI
    assert client.get("/api/audit/models/%s/results.csv" % job_id).status_code == 401  # downloads need sign-in too
    one = client.get("/api/audit/models/%s/results/req-2" % job_id, headers=KEY).json()
    assert "refund order 2" in one["request"]["messages"][1]["content"]       # what was sent comes with the result
    assert client.get("/api/audit/models/%s/results/req-2" % job_id, headers=KEY).json()["id"] == "req-2"
    assert client.get("/api/audit/models/%s/results/nope" % job_id, headers=KEY).status_code == 404
    csv_rows = client.get("/api/audit/models/%s/results.csv" % job_id, headers=KEY).text.strip().splitlines()
    assert len(csv_rows) == 1 + 2 * 2
    assert csv_rows[1].startswith("req-1,gpt-5.4,5/5,300,0.01,router-a,gpt-5.4-mini,gpt-5.4-mini,within,5/5,1.0,100,0.002")
    assert client.get("/api/audit/models/%s/results.json" % job_id, headers=KEY).json()["id"] == job_id
    assert client.get("/api/audit/models/aud_missing/results.csv", headers=KEY).status_code == 404


def test_budget_stops_between_chunks(client):
    items = [_item(i, candidates=(("router-a", "gpt-5.4-mini"),)) for i in range(12)]
    job_id = _post(client, {"items": items, "max_budget_usd": 3})
    _work()
    j = client.get("/api/audit/models/" + job_id, headers=KEY).json()
    assert j["status"] == "stopped" and j["items_done"] == 5 and "budget of $3.00" in j["stop_reason"]


def _age(token):
    """Let a group's heartbeat go stale, as if its worker died."""
    import datetime
    from sqlalchemy import update
    with jobs._engine().begin() as c:
        c.execute(update(jobs._items).where(jobs._items.c.claim == token).values(
            claimed_at=jobs._now() - datetime.timedelta(seconds=jobs.STALE_S + 1)))


def test_free_workers_share_one_big_job(client):
    job_id = _post(client, {"items": [_item(i) for i in range(12)]})
    a, b, c = jobs.claim("a"), jobs.claim("b"), jobs.claim("c")
    assert a[0] == b[0] == c[0] == job_id                                     # the same job, three workers
    assert [s for s, _ in a[2]] == [0, 1, 2, 3, 4] and [s for s, _ in b[2]] == [5, 6, 7, 8, 9]
    assert [s for s, _ in c[2]] == [10, 11] and jobs.claim("d") is None      # nothing left unheld
    assert jobs.get(job_id)["workers"] == 3 and jobs.get(job_id)["status"] == "running"
    for got in (a, b, c):
        jobs.run_group(*got)
    assert jobs.claim("a") is None and jobs.get(job_id)["status"] == "done"


def test_dead_workers_group_is_taken_over_and_it_cannot_write_late(client):
    job_id = _post(client, {"items": [_item(i) for i in range(7)]})
    dead = jobs.claim("a")                                                    # a takes requests 0-4 ... and dies
    other = jobs.claim("b")
    assert [s for s, _ in other[2]] == [5, 6]                                 # b only gets what a doesn't hold
    jobs.run_group(*other)
    assert jobs.claim("c") is None                                            # a's group is not stale yet
    _age(dead[1])
    again = jobs.claim("c")
    assert [s for s, _ in again[2]] == [0, 1, 2, 3, 4]                        # a's group, now c's
    jobs.run_group(*again)
    assert jobs.run_group(*dead) == 0                                         # a comes back: writes nothing
    assert jobs.claim("a") is None
    j = client.get("/api/audit/models/" + job_id, headers=KEY).json()
    res = client.get("/api/audit/models/%s/results" % job_id, headers=KEY).json()
    assert (j["status"], j["items_done"], res["total"]) == ("done", 7, 7)
    assert [r["id"] for r in res["results"]] == ["req-%d" % i for i in range(7)]
    assert sum(s["requests"] for s in j["summary"] if s["label"] == "original") == 7      # no request counted twice


def test_released_group_is_claimable_at_once(client):
    job_id = _post(client, {"items": [_item(1), _item(2)]})
    got = jobs.claim("a")
    assert jobs.claim("b") is None
    jobs.release(got[1])                                                      # a shuts down cleanly
    assert jobs.claim("b")[0] == job_id


def test_engine_crash_fails_the_job_and_frees_the_group(client, monkeypatch):
    job_id = _post(client, {"items": [_item(1)]})
    monkeypatch.setattr(jobs, "validate_many", lambda *a, **k: 1 / 0)
    _work()
    j = jobs.get(job_id)
    assert j["status"] == "failed" and "division by zero" in j["error"] and j["workers"] == 0


def test_unknown_job_is_404(client):
    assert client.get("/api/audit/models/nope", headers=KEY).status_code == 404
    assert client.get("/api/audit/models/nope/results", headers=KEY).status_code == 404


def test_parallel_workers_run_each_request_exactly_once(client, monkeypatch):
    import threading
    from collections import Counter
    job_ids = [_post(client, {"items": [_item(i), _item(i + 100)]}) for i in range(10)]
    calls, lock = Counter(), threading.Lock()

    def counted(items, spec, run=None):
        with lock:
            calls.update((it.id, it.request["input_messages"][-1]["content"]) for it in items)
        return _fake_engine(items, spec)
    monkeypatch.setattr(jobs, "validate_many", counted)
    workers = [threading.Thread(target=_work, args=("w%d" % n,)) for n in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=60)
    assert all(jobs.get(j)["status"] == "done" for j in job_ids)
    assert sum(calls.values()) == 20 and set(calls.values()) == {1}      # 20 requests, none run twice


def test_parallel_workers_split_one_big_job(client, monkeypatch):
    import threading
    from collections import Counter
    from sqlalchemy import select
    job_id = _post(client, {"items": [_item(i, candidates=(("router-a", "gpt-5.4-mini"),)) for i in range(40)],
                            "max_budget_usd": 100})                          # $1 per request in the fake engine
    calls, lock = Counter(), threading.Lock()

    def counted(items, spec, run=None):
        with lock:
            calls.update(it.id for it in items)
        return _fake_engine(items, spec)
    monkeypatch.setattr(jobs, "validate_many", counted)
    workers = [threading.Thread(target=_work, args=("w%d" % n,)) for n in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=60)
    assert jobs.get(job_id)["status"] == "done" and sum(calls.values()) == 40 and set(calls.values()) == {1}
    with jobs._engine().connect() as c:
        who = set(c.execute(select(jobs._items.c.worker).where(jobs._items.c.job_id == job_id)).scalars())
    assert len(who) > 1                                                       # one job, several workers


def test_callback_is_posted_once_when_the_job_ends(client, monkeypatch):
    from sqlalchemy import select
    sent = []
    monkeypatch.setattr(jobs, "_deliver", lambda url, body: sent.append((url, body)))
    assert client.post("/api/audit/models", headers=KEY, json=_audit({"items": [_item(1)], "callback_url": "ftp://x"})).status_code == 422
    job_id = _post(client, {"items": [_item(1)], "callback_url": "https://hooks.example/ta"})
    _work()                                                                 # the last poll sees it done and posts
    jobs.claim("w2")                                                        # later polls never post it again
    mine = [b for u, b in sent if b["id"] == job_id]
    assert len(mine) == 1 and sent[0][0] == "https://hooks.example/ta"
    assert mine[0]["status"] == "done" and mine[0]["results_url"] == "/api/audit/models/%s/results" % job_id
    assert {s["label"] for s in mine[0]["summary"]} == {"original", "router-a", "router-b"}

    monkeypatch.setattr(jobs, "RETRY_S", (0, 0))
    monkeypatch.setattr(jobs, "_deliver", lambda url, body: (_ for _ in ()).throw(OSError("connection refused")))
    job_id = _post(client, {"items": [_item(2)], "callback_url": "https://hooks.example/ta"})
    _work()
    with jobs._engine().connect() as c:                                     # receiver down: the error is kept
        assert "refused" in c.execute(select(jobs._callbacks.c.error).where(jobs._callbacks.c.job_id == job_id)).scalar()


def test_unreachable_gateway_leaves_the_requests_queued(client, monkeypatch):
    from app.services.llm_client import Unreachable
    job_id = _post(client, {"items": [_item(1)]})
    monkeypatch.setattr(jobs, "validate_many", lambda *a, **k: (_ for _ in ()).throw(Unreachable("Connection error.")))
    got = jobs.claim("w1")
    assert got[0] == job_id
    with pytest.raises(Unreachable):
        jobs.run_group(*got)
    j = jobs.get(job_id)
    assert (j["status"], j["items_done"], j["workers"]) == ("running", 0, 0)                     # waiting, not failed
    assert j["error"].startswith("Waiting: the model endpoint has been unreachable since")
    assert client.get("/api/audit/models/" + job_id, headers=KEY).json()["error"].startswith("Waiting:")
    monkeypatch.setattr(jobs, "validate_many", _fake_engine)                                    # the gateway is back
    _work()
    j = jobs.get(job_id)
    assert (j["status"], j["error"]) == ("done", None)


def test_results_are_paged_filtered_and_searchable(client, monkeypatch):
    def half(items, spec, run=None):                      # router-b is within on req-0..5, outside on req-6..11
        out = _fake_engine(items, spec)
        for r in out:
            if int(r["id"].split("-")[1]) < 6:
                r["candidates"][1]["verdict"] = "within"
        return out
    monkeypatch.setattr(jobs, "validate_many", half)
    job_id = _post(client, {"items": [_item(i) for i in range(12)]})
    _work()
    get = lambda q: client.get("/api/audit/models/%s/results?%s" % (job_id, q), headers=KEY).json()
    p3 = get("page=3&per_page=5")
    assert (p3["total"], [r["id"] for r in p3["results"]]) == (12, ["req-10", "req-11"])
    outside = get("label=router-b&verdict=outside")
    assert [r["id"] for r in outside["results"]] == ["req-%d" % i for i in range(6, 12)]   # per request, not per job
    assert get("label=router-a&verdict=outside")["total"] == 0 and get("verdict=within")["total"] == 12
    assert get("show=problems")["total"] == get("show=split")["total"] == 6
    assert get("q=REQ-1")["total"] == 3                                     # req-1, req-10, req-11; any case
    assert client.get("/api/audit/models/%s/results?verdict=bad" % job_id, headers=KEY).status_code == 422
    assert (get("q=req-1&show=problems")["total"], get("q=req-1&show=all")["total"]) == (2, 3)    # counts per filter
    assert len(client.get("/api/audit/models?per_page=1", headers=KEY).json()) == 1


def test_status_polling_says_unchanged_and_shows_router_choices(client, monkeypatch):
    def routed(items, spec, run=None):                    # router-a answers with nano or mini, request by request
        out = _fake_engine(items, spec)
        for n, r in enumerate(out):
            for x in r["candidates"][0]["runs"]:
                x["model"] = "gpt-5.4-nano" if n % 2 else "gpt-5.4-mini"
        return out
    monkeypatch.setattr(jobs, "validate_many", routed)
    job_id = _post(client, {"items": [_item(i) for i in range(4)]})
    first = client.get("/api/audit/models/" + job_id, headers=KEY)
    tag = first.headers["etag"]
    assert client.get("/api/audit/models/" + job_id, headers={**KEY, "If-None-Match": tag}).status_code == 304
    _work()
    done = client.get("/api/audit/models/" + job_id, headers={**KEY, "If-None-Match": tag})
    assert done.status_code == 200 and done.json()["status"] == "done"
    routing = {(x["label"], x["served_model"]): x["requests"] for x in done.json()["routing"]}
    assert routing == {("router-a", "gpt-5.4-mini"): 2, ("router-a", "gpt-5.4-nano"): 2}   # router-b used one model


def test_a_worker_thread_survives_an_unexpected_error(monkeypatch):
    import threading
    from validation import worker
    stop, calls = threading.Event(), []

    def claim(w):
        calls.append(w)
        if len(calls) == 1:
            raise RuntimeError("server closed the connection unexpectedly")
        stop.set()
    monkeypatch.setattr(jobs, "claim", claim)
    monkeypatch.setattr(worker, "ERROR_WAIT_S", 0)
    worker.loop("w1", stop)                                      # returns only because it carried on and claimed again
    assert len(calls) == 2


def test_tables_made_by_another_instance_at_the_same_moment_are_fine(monkeypatch):
    from sqlalchemy import exc
    from app.services import db
    real, calls = db.metadata.create_all, []

    def racing(eng):
        calls.append(1)
        if len(calls) == 1:                                      # Postgres rejects the second of two concurrent CREATEs
            raise exc.IntegrityError("CREATE TABLE validation_jobs", {}, Exception("pg_type_typname_nsp_index"))
        real(eng)
    monkeypatch.setattr(db.metadata, "create_all", racing)
    monkeypatch.setattr(db, "_tables_for", None)
    assert db.ready() is db.engine() and len(calls) == 2


def test_a_table_registered_after_the_first_create_is_still_made(monkeypatch):
    from sqlalchemy import Column, Integer, Table, inspect
    from app.services import db
    db.ready()
    late = Table("late_import_probe", db.metadata, Column("id", Integer, primary_key=True))
    try:
        db.ready()                                               # e.g. store imported after the workers started
        assert inspect(db.engine()).has_table("late_import_probe")
    finally:
        late.drop(db.engine(), checkfirst=True)
        db.metadata.remove(late)


def test_a_failed_job_resumes_where_it_stopped(client, monkeypatch):
    job_id = _post(client, {"items": [_item(i) for i in range(7)], "callback_url": "https://hooks.example/ta",
                            "max_budget_usd": 1.5})
    sent = []
    monkeypatch.setattr(jobs, "_deliver", lambda url, body: sent.append(body["status"]))
    _work()                                                      # $1 per item: stops after the first group of 5
    assert jobs.get(job_id)["status"] == "stopped" and sent == ["stopped"]
    assert client.post("/api/audit/models/%s/resume" % job_id, headers=KEY).status_code == 409   # budget already spent
    assert client.post("/api/audit/models/%s/resume" % job_id, headers=KEY, json={"max_budget_usd": 50}).json()["status"] == "queued"
    _work()
    j = jobs.get(job_id)
    assert (j["status"], j["items_done"], j["stop_reason"], j["max_budget_usd"]) == ("done", 7, None, 50)
    assert sent == ["stopped", "done"]                           # told again when it ends
    assert client.post("/api/audit/models/%s/resume" % job_id, headers=KEY).status_code == 409   # only failed / stopped


def test_a_dropped_connection_while_recording_does_not_lose_the_group(client, monkeypatch):
    from sqlalchemy import exc
    job_id = _post(client, {"items": [_item(1)]})
    real, calls = jobs._record, []

    def flaky(*a):
        calls.append(1)
        if len(calls) == 1:
            raise exc.OperationalError("UPDATE validation_items", {}, Exception("server closed the connection"))
        return real(*a)
    monkeypatch.setattr(jobs, "_record", flaky)
    monkeypatch.setattr(jobs, "RETRY_S", (0, 0))
    _work()
    assert jobs.get(job_id)["status"] == "done" and len(calls) == 2


def _bigquery(monkeypatch, rows):
    from connectors.bigquery import source
    asked = []
    monkeypatch.setattr(source, "_cfg", lambda: ("p.litellm.v_requests", "messages", "response"))
    monkeypatch.setattr(source, "lookup", lambda ids: asked.append(sorted(ids)) or
                        {i: rows[i] for i in ids if i in rows})
    return asked


def test_items_can_name_logged_calls_by_request_id(client, monkeypatch):
    tools = [{"type": "function", "function": {"name": "refund", "parameters": {"type": "object"}}}]
    asked = _bigquery(monkeypatch, {
        "chatcmpl-1": ({"model": "gpt-5.4-2026-03-05", "max_tokens": 300, "tools": tools, "tool_choice": "required",
                        "messages": [{"role": "system", "content": "Billing agent."},
                                     {"role": "user", "content": "refund order 1"}]},
                       {"id": "chatcmpl-1", "model": "gpt-5.4-2026-03-05",
                        "choices": [{"message": {"content": "Refunded."}}], "usage": {"prompt_tokens": 40}}),
        "chatcmpl-failed": ({"model": "gpt-5.4", "messages": [{"role": "user", "content": "x"}]}, None)})
    cands = [{"label": "router-a", "model": "gpt-5.4-mini"}]
    r = client.post("/api/audit/models", headers=KEY, json={"requests": [
        {"request_id": "chatcmpl-1", "candidates": cands}, {"request_id": "chatcmpl-nope", "candidates": cands},
        {"request_id": "chatcmpl-failed", "candidates": cands}, {"candidates": cands}]})
    assert r.status_code == 422 and r.json()["detail"] == [
        "chatcmpl-nope: request_id not found in BigQuery", "chatcmpl-failed: the logged call has no response (it failed)",
        "item 4: send request (and response) or request_id"]
    job_id = _post(client, {"items": [{"request_id": "chatcmpl-1", "candidates": cands},     # mixed with a full item
                                      dict(_item(2), candidates=cands)]})
    assert asked[-1] == ["chatcmpl-1"]                           # one lookup per batch, only for the id-only items
    p, _ = jobs.request(job_id, "chatcmpl-1")                    # the id defaults to the request_id
    assert p["model"] == "gpt-5.4-2026-03-05" and p["recorded"] == "Refunded."
    assert (p["request"]["max_output_tokens"], p["request"]["tool_choice"]) == (300, "required")
    assert [t[0] for t in p["request"]["tools_defined"]] == ["refund"]
    _work()
    assert jobs.get(job_id)["status"] == "done"


def test_request_ids_need_bigquery(client, monkeypatch):
    from connectors.bigquery import source
    monkeypatch.setattr(source, "_cfg", lambda: (None, None, None))
    r = client.post("/api/audit/models", headers=KEY, json={"requests": [
        {"request_id": "chatcmpl-1", "candidates": [{"label": "a", "model": "m"}]}]})
    assert r.status_code == 422 and "need BigQuery" in r.json()["detail"][0]


def test_without_a_budget_a_job_runs_to_the_end(client):
    job_id = _post(client, {"items": [_item(i) for i in range(12)]})     # $1 per item, no budget given
    _work()
    j = jobs.get(job_id)
    assert (j["status"], j["items_done"], j["max_budget_usd"], j["stop_reason"]) == ("done", 12, None, None)
    assert client.get("/api/audit/models/" + job_id, headers=KEY).json()["max_budget_usd"] is None


def test_a_refused_account_stops_the_job_until_resumed(client, monkeypatch):
    from app.services.llm_client import Refused
    job_id = _post(client, {"items": [_item(i) for i in range(7)]})
    monkeypatch.setattr(jobs, "validate_many", lambda *a, **k: (_ for _ in ()).throw(Refused("insufficient_quota")))
    _work()
    j = jobs.get(job_id)
    assert (j["status"], j["items_done"], j["workers"]) == ("stopped", 0, 0)
    assert "refused the account (insufficient_quota)" in j["stop_reason"]
    monkeypatch.setattr(jobs, "validate_many", _fake_engine)                      # credit added
    client.post("/api/audit/models/%s/resume" % job_id, headers=KEY)
    _work()
    assert jobs.get(job_id)["status"] == "done"


def test_a_model_that_is_not_a_name_is_refused_at_submit(client):
    bad = dict(_item(1), request={"model": ["gpt-5.4"], "messages": [{"role": "user", "content": "x"}]})
    long = dict(_item(2), request={"model": "m" * 121, "messages": [{"role": "user", "content": "x"}]})
    r = client.post("/api/audit/models", headers=KEY, json=_audit({"items": [bad, long]}))
    assert r.status_code == 422 and r.json()["detail"] == [
        "req-1: request.model must be a model name of at most 120 characters",
        "req-2: request.model must be a model name of at most 120 characters"]


def test_the_callback_waits_for_groups_still_running(client, monkeypatch):
    job_id = _post(client, {"items": [_item(i) for i in range(10)], "max_budget_usd": 1.5,
                            "callback_url": "https://hooks.example/ta"})
    sent = []
    monkeypatch.setattr(jobs, "_deliver", lambda url, body: sent.append((body["status"], body["items_done"])))
    first = jobs.claim("w1")                                     # w1 holds requests 0-4 for now
    jobs.run_group(*jobs.claim("w2"))                            # w2 spends $5: over the $1.50 budget
    assert jobs.claim("w2") is None and jobs.get(job_id)["status"] == "stopped" and sent == []   # w1 still running
    jobs.run_group(*first)
    jobs.claim("w1")
    j = jobs.get(job_id)
    assert (j["status"], j["items_done"], j["stop_reason"]) == ("done", 10, None) and sent == [("done", 10)]


def test_job_list_shows_each_candidates_result(client):
    job_id = _post(client, {"items": [_item(i) for i in range(2)]})
    _work()
    assert "summary" not in client.get("/api/audit/models", headers=KEY).json()[0]
    got = client.get("/api/audit/models?with_summary=1", headers=KEY).json()[0]
    assert got["id"] == job_id
    assert [(s["label"], s.get("within_rate"), s.get("cost_vs_original_pct")) for s in got["summary"]] == [
        ("original", None, None), ("router-a", 1.0, -80), ("router-b", 0.0, -90)]


def test_a_batch_counts_each_labels_answers(client, monkeypatch):
    def counted(items, spec, run=None):
        out = _fake_engine(items, spec)
        for r in out:
            r["candidates"][0]["answers"] = {"match": 3, "acceptable": 1, "drift": 1, "unclear": 0}
        return out
    monkeypatch.setattr(jobs, "validate_many", counted)
    job_id = _post(client, {"items": [_item(i) for i in range(3)]})
    _work()
    by = {s["label"]: s.get("answers") for s in jobs.summary(job_id)}
    assert by == {"original": None, "router-a": {"match": 9, "acceptable": 3, "drift": 3, "unclear": 0},
                  "router-b": None}                              # router-b's results carry no counts (older results)


def test_a_new_optional_column_is_added_to_an_existing_table():
    from sqlalchemy import Column, Integer, Table, inspect, text
    from app.services import db
    eng = db.ready()
    with eng.begin() as c:
        c.execute(text("CREATE TABLE late_column_probe (id INTEGER PRIMARY KEY)"))
    t = Table("late_column_probe", db.metadata, Column("id", Integer, primary_key=True), Column("extra", Integer))
    try:
        db.ready()
        assert "extra" in {c["name"] for c in inspect(db.engine()).get_columns("late_column_probe")}
    finally:
        t.drop(db.engine(), checkfirst=True)
        db.metadata.remove(t)


def test_the_api_reference_matches_what_the_api_returns(client, monkeypatch):
    """/openapi.json documents the public /api calls only (model audits), with the key as their auth, and every documented response model
    takes a real response (the real engine, fake model calls) with no field missing or undocumented."""
    from app.web import public as v
    from validation import engine

    def run(trace, model):                                       # router-a answers req-2 with another model
        served = "gpt-5.4-nano" if model == "gpt-5.4-mini" and "2" in trace["input_messages"][-1]["content"] else model
        return {"output": "%s ok" % served, "usage": {"input": 10, "output": 2, "cache_read": 0, "cache_write": 0},
                "latency_ms": 5, "model": served, "stop_reason": "stop", "degraded": False}
    monkeypatch.setattr(engine.judges, "fit", lambda ref, cand, req, spec: (True, "same", 0.001))
    monkeypatch.setattr(engine.judges, "coherence", lambda req, outs, spec: {
        "agree": len(outs), "of": len(outs), "divergent": set(), "reasons": {}, "cost_usd": 0.001})
    monkeypatch.setattr(jobs, "validate_many", lambda items, spec: engine.validate_many(items, spec, run))
    monkeypatch.setattr(jobs, "_deliver", lambda url, body: None)
    r = client.post("/api/audit/models", headers=KEY, json=_audit({"items": [_item(1), _item(2)], "callback_url": "http://cb/x"}))
    v.Submitted.model_validate(r.json())
    assert v.Submitted.model_validate(r.json())
    job_id = r.json()["audits"][0]["id"]
    _work()
    got = v.Status.model_validate(client.get("/api/audit/models/" + job_id, headers=KEY).json())
    assert got.status == "done" and got.routing and got.callback.sent_at and got.summary[1].answers.match == 6   # 2 requests x 3 clear passes
    page = v.ResultsPage.model_validate(client.get("/api/audit/models/%s/results" % job_id, headers=KEY).json())
    assert page.total == 2 and page.results[0].candidates[0].runs[0].judges[0].kept is True
    one = v.Result.model_validate(client.get("/api/audit/models/%s/results/req-1" % job_id, headers=KEY).json())
    assert [m.role for m in one.request.messages] == ["system", "user"] and one.request.tools == []
    for q in ("", "?with_summary=1"):
        [v.Listed.model_validate(x) for x in client.get("/api/audit/models" + q, headers=KEY).json()]

    spec = client.get("/openapi.json").json()
    assert set(spec["paths"]) == {"/api/audit/models", "/api/audit/models/{audit_id}", "/api/audit/models/{audit_id}/resume",
                                  "/api/audit/models/{audit_id}/results", "/api/audit/models/{audit_id}/results/{request_id}",
                                  "/api/audit/models/strategies", "/api/audit/models/estimate", "/api/audit/models/preview"}
    assert spec["components"]["securitySchemes"]["APIKeyHeader"] == {
        "type": "apiKey", "in": "header", "name": "X-API-Key", "description": "The server's VALIDATION_API_KEY"}
    assert spec["paths"]["/api/audit/models/{audit_id}"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"] == {"$ref": "#/components/schemas/Status"}
    assert client.get("/docs").status_code == 200


def test_a_signed_in_browser_may_call_the_api(client, monkeypatch):
    from app.web import public as v
    assert client.get("/api/audit/models").status_code == 401
    monkeypatch.setenv("VALIDATION_UI_OPEN", "1")                       # a developer's machine, for the web UI
    assert client.get("/api/audit/models").status_code == 200
    monkeypatch.setenv("K_SERVICE", "token-auditor")                    # never on Cloud Run
    assert client.get("/api/audit/models").status_code == 401
    monkeypatch.setattr(v, "_iap_user", lambda a: "someone@example.com" if a == "signed" else None)
    assert client.get("/api/audit/models", headers={"x-goog-iap-jwt-assertion": "signed"}).status_code == 200
    assert client.get("/api/audit/models", headers={"x-goog-iap-jwt-assertion": "forged"}).status_code == 401


def test_iap_sign_in_is_checked_against_its_audience_and_issuer(monkeypatch):
    import time
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from google.auth import crypt, jwt
    from app.web import public as v
    key = ec.generate_private_key(ec.SECP256R1())
    signer = crypt.ES256Signer.from_string(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), key_id="k1")
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    now, audience = int(time.time()), "/projects/1/global/backendServices/2"

    def token(**claims):
        return jwt.encode(signer, {"iss": "https://cloud.google.com/iap", "aud": audience, "email": "someone@example.com",
                                   "iat": now, "exp": now + 600, **claims}).decode()
    monkeypatch.setattr(v, "_iap", {"at": time.time(), "certs": {"k1": public.decode()}})   # IAP's keys, as fetched
    monkeypatch.setenv("IAP_AUDIENCE", "")
    assert v._iap_user(token()) is None                                  # no audience configured: never trusted
    monkeypatch.setenv("IAP_AUDIENCE", audience)
    assert v._iap_user(token()) == "someone@example.com"
    assert v._iap_user(token(aud="/projects/1/global/backendServices/9")) is None    # another service's sign-in
    assert v._iap_user(token(iss="https://accounts.google.com")) is None             # not from IAP
    assert v._iap_user(token(exp=now - 60, iat=now - 700)) is None                    # expired
    assert v._iap_user("not-a-token") is None


def test_the_web_ui_is_served_under_app(client, monkeypatch, tmp_path):
    from app.web import server
    assert client.get("/app/validations").status_code in (200, 503)      # 503 until `npm run build` has run
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<div id=root></div>")
    (tmp_path / "assets" / "index-abc.js").write_text("console.log(1)")
    monkeypatch.setattr(server, "_DIST", str(tmp_path))
    r = client.get("/app/validations/val_1/requests/a/b")                  # any page route -> the app's index.html
    assert r.status_code == 200 and "root" in r.text and r.headers["cache-control"] == "no-cache"
    r = client.get("/app/assets/index-abc.js")
    assert r.text == "console.log(1)" and "immutable" in r.headers["cache-control"]
    assert "root" in client.get("/app/..%2f..%2fcredentials.py").text      # never a file outside the build
    assert client.get("/app", follow_redirects=False).headers["location"] == "/app/"


def test_one_scope_and_the_audits_models_stand_in_for_missing_candidates(client):
    req = {k: v for k, v in _item(1).items() if k != "candidates"}
    assert client.post("/api/audit/models", headers=KEY, json={}).status_code == 422                     # no scope
    two = {"requests": [req], "usecases": ["uc-x"], "models": ["gpt-5.4-mini"]}
    assert client.post("/api/audit/models", headers=KEY, json=two).status_code == 422                    # two scopes
    assert client.post("/api/audit/models", headers=KEY, json={"requests": [req]}).status_code == 422   # no models
    aid = _post(client, {"requests": [req], "models": ["gpt-5.4-mini"]})
    p, _ = jobs.request(aid, "req-1")
    assert p["candidates"] == [{"label": "gpt-5.4-mini", "model": "gpt-5.4-mini"}]                       # labels are models
    a = client.get("/api/audit/models/" + aid, headers=KEY).json()
    assert (a["scope"], a["strategy"], a["models"], list(a["samples"])) == ("requests", "manual", ["gpt-5.4-mini"], [""])
