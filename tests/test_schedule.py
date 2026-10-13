"""The scheduler (core/schedule.py): a due job is queued once however many instances check at the same moment, not
again until it is due, never while off; run now queues it at once."""
import threading

import pytest
from sqlalchemy import delete, update

from app.services import db
from core import schedule, tasks


@pytest.fixture(autouse=True)
def _clean():
    with db.ready().begin() as c:
        c.execute(delete(tasks.tasks))
        c.execute(delete(schedule.schedules))


def _queued(name):
    return [t for t in tasks.pending() if t == schedule.JOBS[name]["key"]]


def test_a_due_job_is_queued_once_across_instances():
    got, lock = [], threading.Lock()

    def instance():
        q = schedule.tick(force=True)
        with lock:
            got.extend(q)
    ts = [threading.Thread(target=instance) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert got == ["usecases_list"]                          # process_days is off by default
    assert _queued("usecases_list") and not _queued("process_days")
    assert schedule.tick(force=True) == []                    # not due again for 12 h


def test_a_job_comes_due_again_after_its_interval():
    schedule.tick(force=True)
    with db.ready().begin() as c:
        c.execute(update(tasks.tasks).values(status="done"))
        c.execute(update(schedule.schedules).where(schedule.schedules.c.name == "usecases_list").values(
            last_run=schedule._now() - schedule.datetime.timedelta(hours=13)))
    assert schedule.tick(force=True) == ["usecases_list"]


def test_turned_on_it_runs_and_run_now_queues_at_once():
    schedule.set_enabled("process_days", True)
    assert "process_days" in schedule.tick(force=True)
    with db.ready().begin() as c:
        c.execute(update(tasks.tasks).values(status="done"))
    schedule.set_enabled("process_days", False)
    task_id = schedule.run_now("process_days")
    job = {j["name"]: j for j in schedule.status()}["process_days"]
    assert job["task"]["status"] == "queued" and tasks.get(task_id)["key"] == "process_days" and job["next_run"] is None
