"""The validation API, its jobs, workers and pages. Offline: the engine is replaced and workers are driven by hand
(`_work`); the tables live in the test DB (SQLite by default, Postgres when TEST_DATABASE_URL is set)."""
import pytest
from fastapi.testclient import TestClient

from app.web.server import app
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


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("VALIDATION_API_KEY", "test-key")
    monkeypatch.setattr(jobs, "validate_many", _fake_engine)
    return TestClient(app)                                                # no `with`: no in-process workers


def _work(worker="w1"):
    """Act as a worker until nothing is left to claim."""
    while (got := jobs.claim(worker)):
        jobs.run_group(*got)


def _post(client, payload):
    r = client.post("/api/validations", headers=KEY, json=payload)
    assert r.status_code == 202, r.text
    return r.json()["id"]


def test_key_is_required(client, monkeypatch):
    assert client.get("/api/validations").status_code == 401
    assert client.get("/api/validations", headers={"X-API-Key": "wrong"}).status_code == 401
    monkeypatch.setenv("VALIDATION_API_KEY", "")
    assert client.get("/api/validations", headers=KEY).status_code == 503          # not configured -> closed


def test_bad_batches_are_refused_whole(client):
    assert client.post("/api/validations", headers=KEY, json={"items": []}).status_code == 422
    assert client.post("/api/validations", headers=KEY, json={"items": [_item(i) for i in range(1001)]}).status_code == 422
    no_cands = dict(_item(1), candidates=[])
    assert client.post("/api/validations", headers=KEY, json={"items": [no_cands]}).status_code == 422
    dup = _item(1, candidates=(("a", "m1"), ("a", "m2")))
    no_user = dict(_item(2), request={"model": "gpt-5.4", "messages": [{"role": "system", "content": "s"}]})
    r = client.post("/api/validations", headers=KEY, json={"items": [dup, no_user]})
    assert r.status_code == 422
    assert r.json()["detail"] == ["req-1: candidate labels must be unique", "req-2: request.messages needs a user message"]


def test_job_runs_and_reports(client):
    job_id = _post(client, {"items": [_item(1), _item(2)], "judge_model": "claude-sonnet-5-5"})
    assert client.get("/api/validations/" + job_id, headers=KEY).json()["status"] == "queued"
    _work()
    j = client.get("/api/validations/" + job_id, headers=KEY).json()
    assert (j["status"], j["items_done"], j["items_total"], j["judge_model"]) == ("done", 2, 2, "claude-sonnet-5-5")
    assert j["spend_usd"] == 2.0 and len(j["results"]) == 2 and "items" not in j
    by = {s["label"]: s for s in j["summary"]}
    assert (by["router-a"]["within"], by["router-b"]["outside"]) == (2, 2)
    assert by["router-a"]["avg_latency_ms"] == 100 and by["original"]["avg_latency_ms"] == 300
    assert job_id in [x["id"] for x in client.get("/api/validations", headers=KEY).json()]
    assert by["router-a"]["within_rate"] == 1.0 and by["router-a"]["cost_vs_original_pct"] == -80
    page = client.get("/validations/" + job_id).text
    assert "verdict-within" in page and "verdict-outside" in page and "refreshing" not in page
    assert "Candidates disagree (2)" in page and 'id="evidence"' not in page
    assert job_id in client.get("/validations").text


def test_grid_cell_opens_the_evidence_and_downloads(client):
    job_id = _post(client, {"items": [_item(1), _item(2)]})
    _work()
    page = client.get("/validations/%s?item=req-2&cand=router-b" % job_id).text
    assert 'id="evidence"' in page and "refund order 2" in page            # the request itself is shown
    assert "router-b (gpt-5.4-nano)" in page and "router-b run 1" in page   # the candidate's runs, labelled
    assert "?show=split" in page and client.get("/validations/%s?show=nonsense" % job_id).status_code == 200
    csv_rows = client.get("/validations/%s/results.csv" % job_id).text.strip().splitlines()
    assert len(csv_rows) == 1 + 2 * 2 and csv_rows[1].startswith("req-1,gpt-5.4,5/5,300,0.01,router-a,gpt-5.4-mini,within,5/5")
    assert client.get("/validations/%s/results.json" % job_id).json()["id"] == job_id
    assert client.get("/validations/val_missing/results.csv").status_code == 404


def test_budget_stops_between_chunks(client):
    items = [_item(i, candidates=(("router-a", "gpt-5.4-mini"),)) for i in range(12)]
    job_id = _post(client, {"items": items, "max_budget_usd": 3})
    _work()
    j = client.get("/api/validations/" + job_id, headers=KEY).json()
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
    j = client.get("/api/validations/" + job_id, headers=KEY).json()
    assert jobs.claim("a") is None
    j = client.get("/api/validations/" + job_id, headers=KEY).json()
    assert (j["status"], j["items_done"], len(j["results"])) == ("done", 7, 7)
    assert [r["id"] for r in j["results"]] == ["req-%d" % i for i in range(7)]


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
    assert client.get("/api/validations/nope", headers=KEY).status_code == 404
    assert client.get("/validations/nope").status_code == 404


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
