"""Jobs that run every few hours, once across every instance: the workers check JOBS as they poll (at most every
TICK_S per process); the one whose UPDATE moves a due job's last_run wins and queues its task (core/tasks.py), the
others find nothing due. Same on SQLite and Postgres; no outside scheduler."""
import datetime
import time

from sqlalchemy import Boolean, Column, DateTime, String, Table, exc, or_, select, update

from app.services import db
from core import tasks

JOBS = {
    "usecases_list": {"every_h": 12, "kind": "reload_usecases", "args": {}, "key": "reload_usecases",
                      "on": True, "does": "Reload the usecases list from the log (calls per usecase in the window; cheap)"},
    "process_days": {"every_h": 24, "kind": "process_days", "args": {}, "key": "process_days",
                     "on": False, "does": "Process the log's new days: call-sites, versions, cache findings, audit samples "
                                          "(one read of each new day, all usecases; off until its cost on production is known)"},
}
TICK_S = 30
_next_tick = [0.0]

schedules = Table(
    "schedules", db.metadata,
    Column("name", String(40), primary_key=True),
    Column("enabled", Boolean, nullable=False),
    Column("last_run", DateTime),
    Column("task_id", String(24)),
)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _rows():
    """The schedule rows, created on first use with each job's default."""
    eng = db.ready()
    with eng.connect() as c:
        have = {r.name: r for r in c.execute(select(schedules)).all()}
    for name in JOBS.keys() - have.keys():
        try:
            with eng.begin() as c:
                c.execute(schedules.insert().values(name=name, enabled=JOBS[name]["on"]))
        except exc.IntegrityError:
            pass                                        # another instance made it
    if JOBS.keys() - have.keys():
        with eng.connect() as c:
            have = {r.name: r for r in c.execute(select(schedules)).all()}
    return have


def _queue(name):
    job = JOBS[name]
    task_id = tasks.add(job["kind"], job["args"], key=job["key"])
    with db.ready().begin() as c:
        c.execute(update(schedules).where(schedules.c.name == name).values(task_id=task_id))
    return task_id


def tick(force=False):
    """Queue every due job, once across all instances. -> names queued by this call."""
    if not force and time.time() < _next_tick[0]:
        return []
    _next_tick[0] = time.time() + TICK_S
    _rows()
    now, queued = _now(), []
    for name, job in JOBS.items():
        due = or_(schedules.c.last_run.is_(None), schedules.c.last_run < now - datetime.timedelta(hours=job["every_h"]))
        with db.ready().begin() as c:
            won = c.execute(update(schedules).where(schedules.c.name == name, schedules.c.enabled, due)
                            .values(last_run=now)).rowcount
        if won:
            _queue(name)
            queued.append(name)
    return queued


def run_now(name):
    with db.ready().begin() as c:
        c.execute(update(schedules).where(schedules.c.name == name).values(last_run=_now()))
    return _queue(name)


def set_enabled(name, on):
    _rows()
    with db.ready().begin() as c:
        c.execute(update(schedules).where(schedules.c.name == name).values(enabled=bool(on)))


def status():
    """Each job: what it does, how often, on or off, its last run and that task's outcome, the next run."""
    out = []
    for name, row in sorted(_rows().items()):
        if name not in JOBS:
            continue
        job, t = JOBS[name], tasks.get(row.task_id) if row.task_id else None
        nxt = (row.last_run + datetime.timedelta(hours=job["every_h"])) if row.last_run else None
        out.append({"name": name, "does": job["does"], "every_h": job["every_h"], "enabled": row.enabled,
                    "last_run": row.last_run.isoformat(timespec="seconds") if row.last_run else None,
                    "next_run": (nxt.isoformat(timespec="seconds") if nxt else "at the next check") if row.enabled else None,
                    "task": {"status": t["status"], "error": t["error"], "attempts": t["attempts"],
                             "finished_at": t["updated_at"].isoformat(timespec="seconds") if t["status"] in ("done", "failed") else None}
                    if t else None})
    return out
