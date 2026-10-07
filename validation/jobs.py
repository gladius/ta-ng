"""Validation jobs — durable batches whose requests are shared out to every free worker.

Two tables on the app's database (SQLite in dev, Postgres in prod — app.services.db):
  validation_jobs   one row per batch: status, progress, spend
  validation_items  one row per request: what to validate, its result once done, and which worker holds it

    queued -> running -> done | stopped (budget) | failed

The unit of work is a GROUP of up to CHUNK unfinished requests. Any free worker claims the next group of the oldest
live job, so one big batch is spread across all workers. Claiming is one conditional UPDATE per group: each row's
WHERE is re-checked under its row lock (Postgres) or the single writer (SQLite), so no request is ever held by two
workers. A holder heart-beats its group; a group whose holder went quiet for STALE_S is claimable again. Results
are written only while the claim is still held, so a worker that lost its group writes nothing.

ponytail: jobs are served oldest first, so a huge batch delays later ones; add per-job fairness if teams collide.
"""
import dataclasses
import datetime
import json
import secrets
import socket
import threading
import uuid

from sqlalchemy import (Table, Column, String, Text, Integer, Float, DateTime, select, update, insert, and_, or_,
                        exists, func)

from app.services import db
from validation import Item, Candidate, Spec, validate_many

CHUNK = 5                   # requests per group: the engine pools their calls; also the redo cost of a crash
HEARTBEAT_S = 20
STALE_S = 120               # a group not heart-beaten for this long is claimable again
WORKER = "%s-%s" % (socket.gethostname(), uuid.uuid4().hex[:6])   # this process; threads add a suffix
LIVE = ("queued", "running")

_jobs = Table(
    "validation_jobs", db.metadata,
    Column("id", String(32), primary_key=True),
    Column("status", String(16), nullable=False, index=True),
    Column("created_at", DateTime, nullable=False, index=True),
    Column("updated_at", DateTime, nullable=False),
    Column("judge_model", String(120)),
    Column("max_budget_usd", Float, nullable=False),
    Column("items_total", Integer, nullable=False),
    Column("items_done", Integer, nullable=False),
    Column("spend_usd", Float, nullable=False),
    Column("unpriced_calls", Integer, nullable=False),
    Column("stop_reason", Text),
    Column("error", Text),
    Column("spec", Text, nullable=False),
)
_items = Table(
    "validation_items", db.metadata,
    Column("job_id", String(32), primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("item_id", Text, nullable=False),
    Column("payload", Text, nullable=False),       # {id, request (contract trace), model, recorded, candidates}
    Column("result", Text),                        # the engine's result once this request is done
    Column("claim", String(32), index=True),       # the group token of the worker holding it, while unfinished
    Column("claimed_at", DateTime),                # that worker's last heartbeat
    Column("worker", String(120)),                 # who holds / held it
)

_ready = None


def _engine():
    global _ready
    eng = db.engine()
    if _ready is not eng:
        db.metadata.create_all(eng)                # CREATE TABLE IF NOT EXISTS: idempotent
        _ready = eng
    return eng


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)    # naive UTC, portable


def _iso(t):
    return t.replace(tzinfo=datetime.timezone.utc).isoformat(timespec="seconds") if t else None


def _stale():
    return _now() - datetime.timedelta(seconds=STALE_S)


def _unfinished():
    """This job (correlated) still has a request without a result."""
    return and_(_items.c.job_id == _jobs.c.id, _items.c.result.is_(None))


def _free():
    """Unfinished and not held by a live worker."""
    return and_(_items.c.result.is_(None), or_(_items.c.claim.is_(None), _items.c.claimed_at < _stale()))


# ── submitting and reading ──────────────────────────────────────────────────────────────────────────────────────────
def create(items, spec, max_budget_usd):
    """items: [{id, request (contract trace), model, recorded, candidates: [{label, model}]}]. Queued for workers."""
    job_id, now = "val_" + secrets.token_hex(6), _now()
    with _engine().begin() as c:
        c.execute(insert(_jobs).values(
            id=job_id, status="queued", created_at=now, updated_at=now, judge_model=spec.judge_model,
            max_budget_usd=max_budget_usd, items_total=len(items), items_done=0, spend_usd=0.0, unpriced_calls=0,
            spec=json.dumps(dataclasses.asdict(spec))))
        c.execute(insert(_items), [{"job_id": job_id, "seq": i, "item_id": it["id"], "payload": json.dumps(it)}
                                   for i, it in enumerate(items)])
    return get(job_id)


def _brief(row, workers):
    j = dict(row._mapping)
    return {"id": j["id"], "status": j["status"], "created_at": _iso(j["created_at"]),
            "updated_at": _iso(j["updated_at"]), "items_total": j["items_total"], "items_done": j["items_done"],
            "spend_usd": round(j["spend_usd"], 6), "max_budget_usd": j["max_budget_usd"],
            "judge_model": j["judge_model"], "stop_reason": j["stop_reason"], "error": j["error"],
            "workers": workers, "unpriced_calls": j["unpriced_calls"], "spec": json.loads(j["spec"])}


def _workers(c, job_ids):
    """job id -> how many workers are holding a group of it right now."""
    if not job_ids:
        return {}
    rows = c.execute(select(_items.c.job_id, func.count(func.distinct(_items.c.worker)))
                     .where(_items.c.job_id.in_(job_ids), _items.c.result.is_(None), _items.c.claim.is_not(None),
                            _items.c.claimed_at >= _stale())
                     .group_by(_items.c.job_id))
    return dict(rows.all())


def get(job_id):
    """The job's status and progress (no results), or None."""
    with _engine().connect() as c:
        row = c.execute(select(_jobs).where(_jobs.c.id == job_id)).first()
        return _brief(row, _workers(c, [job_id]).get(job_id, 0)) if row else None


def recent(limit=50):
    with _engine().connect() as c:
        rows = c.execute(select(_jobs).order_by(_jobs.c.created_at.desc()).limit(limit)).all()
        active = _workers(c, [r.id for r in rows])
        return [_brief(r, active.get(r.id, 0)) for r in rows]


def results(job_id):
    """Finished requests' results, in submission order."""
    with _engine().connect() as c:
        rows = c.execute(select(_items.c.result).where(_items.c.job_id == job_id, _items.c.result.is_not(None))
                         .order_by(_items.c.seq))
        return [json.loads(r[0]) for r in rows]


def item(job_id, item_id):
    """What was submitted for one request (its contract trace is under "request"), or None."""
    with _engine().connect() as c:
        row = c.execute(select(_items.c.payload).where(_items.c.job_id == job_id, _items.c.item_id == item_id)).first()
    return json.loads(row[0]) if row else None


def view(job_id):
    job = get(job_id)
    if job is None:
        return None
    res = results(job_id)
    return {**job, "summary": summary(res), "results": res}


# ── claiming and running groups ─────────────────────────────────────────────────────────────────────────────────────
def _tidy():
    """Close out live jobs: done when nothing is unfinished; stopped once over budget (in-flight groups still land).
    Run before every claim, so a job is closed within one poll even when its last two groups finish together."""
    now = _now()
    with _engine().begin() as c:
        c.execute(update(_jobs).where(_jobs.c.status.in_(LIVE), ~exists().where(_unfinished()))
                  .values(status="done", updated_at=now))
        over = c.execute(select(_jobs.c.id, _jobs.c.max_budget_usd, _jobs.c.items_done, _jobs.c.items_total)
                         .where(_jobs.c.status.in_(LIVE), _jobs.c.spend_usd >= _jobs.c.max_budget_usd)).all()
        for job_id, budget, done, total in over:
            c.execute(update(_jobs).where(_jobs.c.id == job_id, _jobs.c.status.in_(LIVE)).values(
                status="stopped", updated_at=now,
                stop_reason="budget of $%.2f reached after %d of %d requests" % (budget, done, total)))


def claim(worker=WORKER):
    """Claim the next group of up to CHUNK unfinished requests from the oldest live job under budget.
    -> (job_id, token, [(seq, payload)]) or None when there is nothing to do."""
    _tidy()
    eng, now, token = _engine(), _now(), uuid.uuid4().hex
    with eng.connect() as c:
        job_ids = c.execute(select(_jobs.c.id).where(_jobs.c.status.in_(LIVE), exists().where(
            _items.c.job_id == _jobs.c.id, _free())).order_by(_jobs.c.created_at).limit(5)).scalars().all()
    for job_id in job_ids:
        for _ in range(10):                        # lost the race for these rows? go for the next free ones
            with eng.connect() as c:
                seqs = c.execute(select(_items.c.seq).where(_items.c.job_id == job_id, _free())
                                 .order_by(_items.c.seq).limit(CHUNK)).scalars().all()
            if not seqs:
                break
            with eng.begin() as c:                 # rows another worker took meanwhile simply don't match
                c.execute(update(_items).where(_items.c.job_id == job_id, _items.c.seq.in_(seqs), _free())
                          .values(claim=token, claimed_at=now, worker=worker))
                c.execute(update(_jobs).where(_jobs.c.id == job_id, _jobs.c.status == "queued")
                          .values(status="running", updated_at=now))
            with eng.connect() as c:
                group = [(s, json.loads(p)) for s, p in c.execute(
                    select(_items.c.seq, _items.c.payload).where(_items.c.claim == token).order_by(_items.c.seq))]
            if group:
                return job_id, token, group
    return None


def release(token):
    """Give a group back (a worker shutting down): claimable again at once instead of after STALE_S."""
    with _engine().begin() as c:
        c.execute(update(_items).where(_items.c.claim == token, _items.c.result.is_(None)).values(claim=None))


def _beat(token, stop):
    while not stop.wait(HEARTBEAT_S):
        with _engine().begin() as c:
            if not c.execute(update(_items).where(_items.c.claim == token, _items.c.result.is_(None))
                             .values(claimed_at=_now())).rowcount:
                return                             # recorded, released, or taken over


def _unpriced(result):
    runs = [r for r in result["reference"]["runs"] if not r.get("recorded")]
    runs += [r for c in result["candidates"] for r in c["runs"]]
    return sum(1 for r in runs if r.get("cost_usd") is None and not r.get("error"))


def _record(job_id, token, group, res):
    """Write the group's results — each only while this claim still holds it. -> how many were written."""
    now, written, spend, unpriced = _now(), 0, 0.0, 0
    with _engine().begin() as c:
        for (seq, _), r in zip(group, res):
            if c.execute(update(_items).where(_items.c.job_id == job_id, _items.c.seq == seq,
                                              _items.c.claim == token, _items.c.result.is_(None))
                         .values(result=json.dumps(r), claim=None, claimed_at=now)).rowcount:
                written, spend, unpriced = written + 1, spend + r["spend_usd"], unpriced + _unpriced(r)
        if written:
            c.execute(update(_jobs).where(_jobs.c.id == job_id).values(
                items_done=_jobs.c.items_done + written, spend_usd=_jobs.c.spend_usd + spend,
                unpriced_calls=_jobs.c.unpriced_calls + unpriced, updated_at=now))
    return written


def run_group(job_id, token, group):
    """Validate one claimed group and record it. A crash in the engine fails the job (and frees the group)."""
    spec = Spec(**get(job_id)["spec"])
    stop = threading.Event()
    threading.Thread(target=_beat, args=(token, stop), daemon=True).start()
    try:
        items = [Item(id=p["id"], request=p["request"], model=p["model"], recorded=p["recorded"],
                      candidates=[Candidate(**c) for c in p["candidates"]]) for _, p in group]
        return _record(job_id, token, group, validate_many(items, spec))
    except Exception as e:                         # recorded on the job, so callers see why instead of "running"
        release(token)
        with _engine().begin() as c:
            c.execute(update(_jobs).where(_jobs.c.id == job_id, _jobs.c.status.in_(LIVE)).values(
                status="failed", error=str(e)[:300], updated_at=_now()))
        return 0
    finally:
        stop.set()


# ── reporting ───────────────────────────────────────────────────────────────────────────────────────────────────────
def original_averages(result):
    """The original model's average latency and cost on one request, from its fresh re-runs."""
    fresh = [x for x in result["reference"]["runs"] if not x.get("recorded") and not x.get("error")]
    lat = [x["latency_ms"] for x in fresh if x.get("latency_ms")]
    cost = [x["cost_usd"] for x in fresh if x.get("cost_usd") is not None]
    return (round(sum(lat) / len(lat)) if lat else None), (round(sum(cost) / len(cost), 6) if cost else None)


def summary(res):
    """Per candidate label across results, plus the original as the baseline row. No winner is picked."""
    rows = {}

    def add(label, model, verdict, latency, cost):
        s = rows.setdefault(label, {"label": label, "models": [], "requests": 0, "within": 0, "outside": 0,
                                    "unverified": 0, "_lat": [], "_cost": []})
        s["requests"] += 1
        if verdict:
            s[verdict] += 1
        if model not in s["models"]:
            s["models"].append(model)
        if latency is not None:
            s["_lat"].append(latency)
        if cost is not None:
            s["_cost"].append(cost)

    for r in res:
        add("original", r["model"], None, *original_averages(r))
        for c in r["candidates"]:
            add(c["label"], c["model"], c["verdict"], c["latency_ms"], c["cost_usd"])
    for s in rows.values():
        lat, cost = s.pop("_lat"), s.pop("_cost")
        s["avg_latency_ms"] = round(sum(lat) / len(lat)) if lat else None
        s["avg_cost_usd"] = round(sum(cost) / len(cost), 6) if cost else None
    base = rows.get("original", {})
    for s in rows.values():
        if s["label"] == "original":
            continue
        s["within_rate"] = round(s["within"] / s["requests"], 3) if s["requests"] else None
        for k, out in (("avg_cost_usd", "cost_vs_original_pct"), ("avg_latency_ms", "latency_vs_original_pct")):
            s[out] = round((s[k] - base[k]) / base[k] * 100) if s.get(k) is not None and base.get(k) else None
    return list(rows.values())
