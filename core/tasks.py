"""Background tasks on the database queue: the steps before an audit's requests exist (sampling them from the log)
and a usecase's analysis. Any worker on any instance runs them, the way it runs request groups (validation/jobs.py):
a claim with a token, a heartbeat, and a task whose worker went quiet for STALE_S is taken over by another one. A
task with a `key` is not queued twice while one with that key is waiting or running.

Kinds map to functions by import path (no import cycles), called with the task's args. A function's own exception fails the task (its caller records why); only a dead worker's task is retried, up
to MAX_ATTEMPTS times."""
import datetime
import importlib
import json
import secrets
import threading
import uuid

from sqlalchemy import Column, DateTime, Integer, String, Table, Text, and_, exc, or_, select, update

from app.services import db

KINDS = {
    "sample_audit": "modules.models.audits:_fill",
    "process_days": "core.ledger.refresh:process_missing",
    "profile_site": "core.profiler:profile",
    "reload_usecases": "core.ledger.refresh:list_usecases",
}
HEARTBEAT_S = 20
STALE_S = 120
MAX_ATTEMPTS = 3
PENDING = ("queued", "running")

tasks = Table(
    "tasks", db.metadata,
    Column("id", String(24), primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("key", Text, index=True),                     # dedup: one pending task per key
    Column("args", Text, nullable=False),                # JSON
    Column("status", String(10), nullable=False, index=True),   # queued | running | done | failed
    Column("created_at", DateTime, nullable=False, index=True),
    Column("updated_at", DateTime, nullable=False),
    Column("claim", String(32)),
    Column("claimed_at", DateTime),
    Column("worker", String(120)),
    Column("attempts", Integer, nullable=False, default=0),
    Column("error", Text),
    Column("note", Text),                                # progress, written by the task itself (tasks.note)
)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _stale():
    return _now() - datetime.timedelta(seconds=STALE_S)


def add(kind, args, key=None):
    """Queue a task; with a `key`, the pending task of that key is returned instead of a second one. -> task id."""
    if kind not in KINDS:
        raise ValueError("unknown task kind %r" % kind)
    eng = db.ready()
    with eng.begin() as c:
        if key:
            got = c.execute(select(tasks.c.id).where(tasks.c.key == key, tasks.c.status.in_(PENDING))).scalar()
            if got:
                return got
        task_id = "tsk_" + secrets.token_hex(6)
        now = _now()
        c.execute(tasks.insert().values(id=task_id, kind=kind, key=key, args=json.dumps(args), status="queued",
                                        created_at=now, updated_at=now, attempts=0))
    return task_id


def pending(prefix=""):
    """Keys of the tasks waiting or running, optionally only those starting with `prefix`."""
    eng = db.ready()
    with eng.connect() as c:
        keys = c.execute(select(tasks.c.key).where(tasks.c.status.in_(PENDING), tasks.c.key.is_not(None))).scalars().all()
    return sorted(k for k in keys if k.startswith(prefix))


def get(task_id):
    eng = db.ready()
    with eng.connect() as c:
        r = c.execute(select(*tasks.columns).where(tasks.c.id == task_id)).first()
    return dict(zip([col.name for col in tasks.columns], r)) if r else None


def last(key):
    """The newest task of that key, or None."""
    eng = db.ready()
    with eng.connect() as c:
        got = c.execute(select(tasks.c.id).where(tasks.c.key == key).order_by(tasks.c.created_at.desc())).scalar()
    return get(got) if got else None


def _claimable():
    return or_(tasks.c.status == "queued",
               and_(tasks.c.status == "running", or_(tasks.c.claimed_at.is_(None), tasks.c.claimed_at < _stale())))


def claim(worker):
    """The oldest task nobody is running (or whose worker went quiet), claimed. -> (task, token) or None."""
    eng, token = db.ready(), uuid.uuid4().hex
    with eng.connect() as c:
        ids = c.execute(select(tasks.c.id).where(_claimable()).order_by(tasks.c.created_at).limit(5)).scalars().all()
    for task_id in ids:
        now = _now()
        with eng.begin() as c:
            row = c.execute(select(tasks.c.attempts).where(tasks.c.id == task_id, _claimable())).first()
            if row is None:
                continue                                  # another worker took it
            if row[0] >= MAX_ATTEMPTS:                    # its workers kept dying: give up, say so
                c.execute(update(tasks).where(tasks.c.id == task_id).values(
                    status="failed", updated_at=now, claim=None, error="gave up after %d attempts" % row[0]))
                continue
            got = c.execute(update(tasks).where(tasks.c.id == task_id, _claimable()).values(
                status="running", claim=token, claimed_at=now, worker=worker, attempts=tasks.c.attempts + 1,
                updated_at=now)).rowcount
        if got:
            return get(task_id), token
    return None


def _beat(task_id, token, stop):
    while not stop.wait(HEARTBEAT_S):
        try:
            with db.ready().begin() as c:
                if not c.execute(update(tasks).where(tasks.c.id == task_id, tasks.c.claim == token)
                                 .values(claimed_at=_now())).rowcount:
                    return                                # finished, or taken over
        except exc.DBAPIError:
            pass


_current = threading.local()


def note(text):
    """The running task's progress note (shown on the Schedules page); no-op outside a task."""
    task_id = getattr(_current, "task_id", None)
    if not task_id:
        return
    try:
        with db.ready().begin() as c:
            c.execute(update(tasks).where(tasks.c.id == task_id).values(note=text[:300]))
    except exc.DBAPIError:
        pass


def _finish(task_id, token, error=None):
    with db.ready().begin() as c:
        c.execute(update(tasks).where(tasks.c.id == task_id, tasks.c.claim == token).values(
            status="failed" if error else "done", error=error, claim=None, updated_at=_now()))


def release(token):
    """Give a task back (a worker shutting down): claimable again at once."""
    with db.ready().begin() as c:
        c.execute(update(tasks).where(tasks.c.claim == token, tasks.c.status == "running")
                  .values(status="queued", claim=None, attempts=tasks.c.attempts - 1, updated_at=_now()))


def run(task, token):
    """Run a claimed task with a heartbeat; its function's exception fails it."""
    stop = threading.Event()
    threading.Thread(target=_beat, args=(task["id"], token, stop), daemon=True).start()
    _current.task_id = task["id"]
    try:
        mod, fn = KINDS[task["kind"]].split(":")
        getattr(importlib.import_module(mod), fn)(**json.loads(task["args"]))
        _finish(task["id"], token)
    except Exception as e:
        _finish(task["id"], token, "%s: %s" % (type(e).__name__, str(e)[:500]))
    finally:
        _current.task_id = None
        stop.set()
