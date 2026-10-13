"""The background task queue (core/tasks.py): a task runs once however many workers claim it, a dead worker's task is
taken over (and given up after MAX_ATTEMPTS), a keyed task is not queued twice, and an audit's sampling re-run after a
crash never queues its job twice. Offline: a probe function stands in for the real work."""
import threading

import pytest
from sqlalchemy import delete, update

from app.services import db
from core import tasks

CALLS = []


def _probe(n=0, fail=False):
    CALLS.append(n)
    if fail:
        raise ValueError("bad input")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setitem(tasks.KINDS, "probe", __name__ + ":_probe")
    with db.ready().begin() as c:
        c.execute(delete(tasks.tasks))
    CALLS.clear()


def _age(task_id):
    """Make the task's worker look dead: its last heartbeat older than STALE_S."""
    with db.ready().begin() as c:
        c.execute(update(tasks.tasks).where(tasks.tasks.c.id == task_id)
                  .values(claimed_at=tasks._now() - tasks.datetime.timedelta(seconds=tasks.STALE_S + 1)))


def test_a_task_runs_once_however_many_workers_claim_it():
    task_id = tasks.add("probe", {"n": 7})
    got, lock = [], threading.Lock()

    def claim(w):
        c = tasks.claim(w)
        if c:
            with lock:
                got.append(c)
    ts = [threading.Thread(target=claim, args=("w%d" % i,)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(got) == 1
    tasks.run(*got[0])
    assert CALLS == [7] and tasks.get(task_id)["status"] == "done" and tasks.claim("w9") is None


def test_a_dead_workers_task_is_taken_over_then_given_up():
    task_id = tasks.add("probe", {})
    assert tasks.claim("w1") and tasks.claim("w2") is None       # held while its worker beats
    for n in range(2, tasks.MAX_ATTEMPTS + 1):
        _age(task_id)
        task, _ = tasks.claim("w%d" % n)
        assert task["id"] == task_id and task["attempts"] == n
    _age(task_id)
    assert tasks.claim("w9") is None
    t = tasks.get(task_id)
    assert t["status"] == "failed" and "gave up" in t["error"] and not CALLS


def test_a_released_task_is_claimable_at_once_without_using_an_attempt():
    task_id = tasks.add("probe", {})
    _, token = tasks.claim("w1")
    tasks.release(token)
    task, _ = tasks.claim("w2")
    assert task["id"] == task_id and task["attempts"] == 1


def test_the_functions_own_error_fails_the_task_without_a_retry():
    task_id = tasks.add("probe", {"fail": True})
    tasks.run(*tasks.claim("w1"))
    t = tasks.get(task_id)
    assert t["status"] == "failed" and "bad input" in t["error"] and tasks.claim("w2") is None


def test_the_missing_days_are_queued_as_one_task_once():
    from core.ledger import refresh
    assert len(refresh.missing_days()) == refresh.window()
    assert refresh.start() is True and refresh.running() == ["*"]
    assert refresh.start() is False                              # queued already


def test_sampling_rerun_after_a_crash_does_not_queue_the_job_twice(monkeypatch):
    from app.services.downgrade_refset import spec
    from modules.models import audits
    from validation import jobs
    a = audits.start(usecase="billing", strategy="manual", asked=["gpt-5.4-mini"])
    assert a["status"] == "sampling" and tasks.last("sample:" + a["id"])["kind"] == "sample_audit"
    item = {"id": "r1", "model": "gpt-5.4", "recorded": "ok", "candidates": [{"label": "m", "model": "gpt-5.4-mini"}],
            "request": {"input": [{"role": "user", "content": "hi"}], "output": "ok", "metadata": {}}}
    jobs.create([item], spec(), job_id=a["id"])                  # the first worker got this far, then died
    monkeypatch.setattr(audits, "sample", lambda *a, **k: pytest.fail("sampled again"))
    audits._fill(a["id"], usecase="billing", strategy="manual", asked=["gpt-5.4-mini"])
    after = audits.get(a["id"])
    assert after["status"] == "queued" and after["job_id"] == a["id"]


def test_an_audit_whose_sampling_was_given_up_shows_failed():
    from modules.models import audits
    a = audits.start(usecase="billing", strategy="manual", asked=["gpt-5.4-mini"])
    t = tasks.last("sample:" + a["id"])
    with db.ready().begin() as c:
        c.execute(update(tasks.tasks).where(tasks.tasks.c.id == t["id"]).values(status="failed", error="gave up after 3 attempts"))
    after = audits.refresh_status(audits.get(a["id"]))
    assert after["status"] == "failed" and "gave up" in after["error"]
