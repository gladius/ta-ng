"""Validation jobs — durable batches whose requests are shared out to every free worker.

Tables on the app's database (SQLite in dev, Postgres in prod — app.services.db):
  validation_jobs   one row per batch: status, progress, spend
  validation_items  one row per request: what to validate, its result once done, and which worker holds it
  validation_cells  one row per request x candidate (and the original): verdict, fit, served model, latency, cost —
                    what summaries, filters, pages and exports read, so they never load the full answers

    queued -> running -> done | stopped (budget) | failed

A job submitted with a callback URL is POSTed its status and per-candidate summary once it ends (validation_callbacks).

The unit of work is a GROUP of up to CHUNK unfinished requests. Any free worker claims the next group of the oldest
live job, so one big batch is spread across all workers. Claiming is one conditional UPDATE per group: each row's
WHERE is re-checked under its row lock (Postgres) or the single writer (SQLite), so no request is ever held by two
workers. A holder heart-beats its group; a group whose holder went quiet for STALE_S is claimable again. Results
are written only while the claim is still held, so a worker that lost its group writes nothing.

ponytail: jobs are served oldest first, so a huge batch delays later ones; add per-job fairness if teams collide.
"""
import dataclasses
import datetime
from collections import Counter, defaultdict
import json
import secrets
import socket
import threading
import time
import urllib.parse
import urllib.request
import uuid

from sqlalchemy import (Table, Column, String, Text, Integer, Float, DateTime, select, update, insert, and_, or_,
                        exists, func, case, exc)

from app.services import db
from app.services.llm_client import Refused, Unreachable
from validation import Item, Candidate, Spec, validate_many
from validation.engine import ANSWERS

CHUNK = 5                   # requests per group: the engine pools their calls; also the redo cost of a crash
HEARTBEAT_S = 20
STALE_S = 120               # a group not heart-beaten for this long is claimable again
WORKER = "%s-%s" % (socket.gethostname(), uuid.uuid4().hex[:6])   # this process; threads add a suffix
LIVE = ("queued", "running")
RETRY_S = (2, 5)            # waits between 3 attempts: delivering a callback, recording a group's results

_jobs = Table(
    "validation_jobs", db.metadata,
    Column("id", String(32), primary_key=True),
    Column("status", String(16), nullable=False, index=True),
    Column("created_at", DateTime, nullable=False, index=True),
    Column("updated_at", DateTime, nullable=False),
    Column("judge_model", Text),
    Column("max_budget_usd", Float),               # None: no budget, the job runs to the end
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
_cells = Table(
    "validation_cells", db.metadata,
    Column("job_id", String(32), primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("label", Text, primary_key=True),  # "original" is the original model's row
    Column("pos", Integer, nullable=False),          # 0 for the original, then candidates in submission order
    Column("model", Text),
    Column("served_model", Text),             # the model that answered most runs: a router's actual pick
    Column("verdict", String(16)),
    Column("kept", Integer),                         # runs that fit; for the original, how many of its answers agree
    Column("runs", Integer),
    Column("latency_ms", Float),
    Column("cost_usd", Float),
    Column("reason", Text),
    Column("judge_reason", Text),
    Column("match", Integer),                        # the candidate's answers by category (None before they existed)
    Column("acceptable", Integer),
    Column("drift", Integer),
    Column("unclear", Integer),
)
_callbacks = Table(
    "validation_callbacks", db.metadata,
    Column("job_id", String(32), primary_key=True),
    Column("url", Text, nullable=False),
    Column("sent_at", DateTime),                   # set by the one worker that delivers it
    Column("error", Text),                         # why delivery failed after every attempt
)

def _engine():
    return db.ready()


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
def create(items, spec, max_budget_usd=None, callback_url=None):
    """items: [{id, request (contract trace), model, recorded, candidates: [{label, model}]}]. Queued for workers."""
    job_id, now = "val_" + secrets.token_hex(6), _now()
    with _engine().begin() as c:
        c.execute(insert(_jobs).values(
            id=job_id, status="queued", created_at=now, updated_at=now, judge_model=spec.judge_model,
            max_budget_usd=max_budget_usd, items_total=len(items), items_done=0, spend_usd=0.0, unpriced_calls=0,
            spec=json.dumps(dataclasses.asdict(spec))))
        c.execute(insert(_items), [{"job_id": job_id, "seq": i, "item_id": it["id"], "payload": json.dumps(it)}
                                   for i, it in enumerate(items)])
        if callback_url:
            c.execute(insert(_callbacks).values(job_id=job_id, url=callback_url))
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


def recent(limit=50, offset=0):
    with _engine().connect() as c:
        rows = c.execute(select(_jobs).order_by(_jobs.c.created_at.desc()).limit(limit).offset(offset)).all()
        active = _workers(c, [r.id for r in rows])
        return [_brief(r, active.get(r.id, 0)) for r in rows]


def results(job_id):
    """Every finished request's full result, in submission order (the JSON download; pages use page())."""
    with _engine().connect() as c:
        rows = c.execute(select(_items.c.result).where(_items.c.job_id == job_id, _items.c.result.is_not(None))
                         .order_by(_items.c.seq))
        return [json.loads(r[0]) for r in rows]


def request(job_id, item_id):
    """(what was submitted — its contract trace is under "request" —, its result or None), or None if unknown."""
    with _engine().connect() as c:
        row = c.execute(select(_items.c.payload, _items.c.result)
                        .where(_items.c.job_id == job_id, _items.c.item_id == item_id)).first()
    return (json.loads(row[0]), json.loads(row[1]) if row[1] else None) if row else None


def _where(job_id, show="all", label=None, verdict=None, q=""):
    it, ce = _items.c, _cells.c

    def candidate(*w):
        return exists().where(ce.job_id == it.job_id, ce.seq == it.seq, ce.label != "original", *w)
    w = [it.job_id == job_id, it.result.is_not(None)]
    if q:
        w.append(func.lower(it.item_id).contains(q.lower(), autoescape=True))
    if label or verdict:
        w.append(candidate(*([ce.label == label] if label else []), *([ce.verdict == verdict] if verdict else [])))
    if show == "problems":
        w.append(candidate(ce.verdict != "within"))
    elif show == "split":
        w.append(select(func.count(func.distinct(ce.verdict))).where(
            ce.job_id == it.job_id, ce.seq == it.seq, ce.label != "original").scalar_subquery() > 1)
    return w


def count(job_id, **filters):
    """How many finished requests match: show (all | problems: a candidate not within | split: candidates disagree),
    label + verdict (a candidate's), q (in the request id)."""
    with _engine().connect() as c:
        return c.execute(select(func.count()).select_from(_items).where(*_where(job_id, **filters))).scalar()


def page(job_id, page=1, per_page=50, **filters):
    """-> (total, [full result]): one page of the finished requests matching the filters, in submission order."""
    with _engine().connect() as c:
        rows = c.execute(select(_items.c.result).where(*_where(job_id, **filters)).order_by(_items.c.seq)
                         .limit(per_page).offset((page - 1) * per_page))
        res = [json.loads(r[0]) for r in rows]
    return count(job_id, **filters), res


def table(job_id):
    """Every row of the job's cells with its request id, request by request, the original's row first (CSV)."""
    with _engine().connect() as c:
        rows = c.execute(select(_items.c.item_id, _cells).where(
            _cells.c.job_id == job_id, _items.c.job_id == _cells.c.job_id, _items.c.seq == _cells.c.seq)
            .order_by(_cells.c.seq, _cells.c.pos))
        return [dict(r._mapping) for r in rows]


def view(job_id):
    """Everything about a job, every result included (the JSON download)."""
    job = get(job_id)
    if job is None:
        return None
    return {**job, "summary": summary(job_id), "routing": routing(job_id), "results": results(job_id)}


# ── claiming and running groups ─────────────────────────────────────────────────────────────────────────────────────
def _tidy():
    """Close out live jobs: done when nothing is unfinished; stopped once over budget (in-flight groups still land).
    Run before every claim, so a job is closed within one poll even when its last two groups finish together."""
    now = _now()
    with _engine().begin() as c:
        c.execute(update(_jobs).where(_jobs.c.status.in_(LIVE + ("stopped",)), ~exists().where(_unfinished()))
                  .values(status="done", error=None, stop_reason=None, updated_at=now))
        over = c.execute(select(_jobs.c.id, _jobs.c.max_budget_usd, _jobs.c.items_done, _jobs.c.items_total)
                         .where(_jobs.c.status.in_(LIVE), _jobs.c.max_budget_usd.is_not(None),
                                _jobs.c.spend_usd >= _jobs.c.max_budget_usd)).all()
        for job_id, budget, done, total in over:
            c.execute(update(_jobs).where(_jobs.c.id == job_id, _jobs.c.status.in_(LIVE)).values(
                status="stopped", error=None, updated_at=now,
                stop_reason="budget of $%.2f reached after %d of %d requests" % (budget, done, total)))


def callback(job_id):
    """How the job's callback went: {sent_at, error} (both None until the job ends), or None without a callback."""
    with _engine().connect() as c:
        row = c.execute(select(_callbacks.c.sent_at, _callbacks.c.error).where(_callbacks.c.job_id == job_id)).first()
    return {"sent_at": _iso(row[0]), "error": row[1]} if row else None


def resume(job_id, max_budget_usd=None):
    """Queue a failed or stopped job again, optionally with a new budget: its unfinished requests run, the finished
    ones are kept, and its callback is sent again when it ends. -> the job, or None if it was not failed or stopped,
    or its budget (the new one if given) is already spent."""
    budget = _jobs.c.max_budget_usd if max_budget_usd is None else max_budget_usd
    with _engine().begin() as c:
        if not c.execute(update(_jobs).where(
                _jobs.c.id == job_id, _jobs.c.status.in_(("failed", "stopped")),
                or_(_jobs.c.spend_usd < budget, _jobs.c.max_budget_usd.is_(None) if max_budget_usd is None else False))
                .values(
                status="queued", error=None, stop_reason=None, updated_at=_now(),
                max_budget_usd=_jobs.c.max_budget_usd if max_budget_usd is None else max_budget_usd)).rowcount:
            return None
        c.execute(update(_callbacks).where(_callbacks.c.job_id == job_id).values(sent_at=None, error=None))
    return get(job_id)


def _deliver(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).close()     # a non-2xx reply raises, so it counts as a failed attempt


def _notify():
    """POST each ended job's status and summary to its callback URL, once: the worker whose UPDATE marks it sent
    delivers it. ponytail: 3 attempts inline; a receiver down for longer misses it (the error is kept on the row)."""
    with _engine().connect() as c:
        due = c.execute(select(_callbacks.c.job_id, _callbacks.c.url).where(   # ended, and no group still landing
            _callbacks.c.sent_at.is_(None), _callbacks.c.job_id == _jobs.c.id, _jobs.c.status.not_in(LIVE),
            ~exists().where(_items.c.job_id == _jobs.c.id, _items.c.result.is_(None), _items.c.claim.is_not(None),
                            _items.c.claimed_at >= _stale()))).all()
    for job_id, url in due:
        with _engine().begin() as c:
            if not c.execute(update(_callbacks).where(_callbacks.c.job_id == job_id, _callbacks.c.sent_at.is_(None))
                             .values(sent_at=_now())).rowcount:
                continue                               # another worker is delivering it
        body = {**get(job_id), "summary": summary(job_id), "routing": routing(job_id),
                "results_url": "/api/validations/%s/results" % job_id}
        error = None
        for wait in (0,) + RETRY_S:
            time.sleep(wait)
            try:
                _deliver(url, body)
                error = None
                break
            except Exception as e:
                error = str(e)[:300]
        if error:
            print("callback for %s to %s failed: %s" % (job_id, urllib.parse.urlsplit(url).hostname, error), flush=True)
            with _engine().begin() as c:
                c.execute(update(_callbacks).where(_callbacks.c.job_id == job_id).values(error=error))


def claim(worker=WORKER):
    """Claim the next group of up to CHUNK unfinished requests from the oldest live job.
    -> (job_id, token, [(seq, payload)]) or None when there is nothing to do."""
    _tidy()
    _notify()
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
        try:
            with _engine().begin() as c:
                if not c.execute(update(_items).where(_items.c.claim == token, _items.c.result.is_(None))
                                 .values(claimed_at=_now())).rowcount:
                    return                         # recorded, released, or taken over
        except exc.DBAPIError:                     # e.g. a dropped connection: beat again next time
            pass


def _unpriced(result):
    runs = [r for r in result["reference"]["runs"] if not r.get("recorded")]
    runs += [r for c in result["candidates"] for r in c["runs"]]
    return sum(1 for r in runs if r.get("cost_usd") is None and not r.get("error"))


def _cell_rows(job_id, seq, r):
    lat, cost = original_averages(r)
    ref = r["reference"]
    rows = [{"job_id": job_id, "seq": seq, "label": "original", "pos": 0, "model": r["model"], "served_model": None,
             "verdict": None, "kept": ref["agree"], "runs": ref["of"], "latency_ms": lat, "cost_usd": cost,
             "reason": None, "judge_reason": None, **{k: None for k in ANSWERS}}]
    for n, c in enumerate(r["candidates"], 1):
        served = Counter(x["model"] for x in c["runs"] if x.get("model") and not x.get("error")).most_common(1)
        rows.append({"job_id": job_id, "seq": seq, "label": c["label"], "pos": n, "model": c["model"],
                     "served_model": served[0][0] if served else None, "verdict": c["verdict"], "kept": c["kept"],
                     "runs": c["of"], "latency_ms": c["latency_ms"], "cost_usd": c["cost_usd"], "reason": c["reason"],
                     "judge_reason": c.get("judge_reason") or None,
                     **{k: (c.get("answers") or {}).get(k) for k in ANSWERS}})
    return rows


def _record(job_id, token, group, res):
    """Write the group's results — each only while this claim still holds it. -> how many were written."""
    now, written, spend, unpriced, cells = _now(), 0, 0.0, 0, []
    with _engine().begin() as c:
        for (seq, _), r in zip(group, res):
            if c.execute(update(_items).where(_items.c.job_id == job_id, _items.c.seq == seq,
                                              _items.c.claim == token, _items.c.result.is_(None))
                         .values(result=json.dumps(r), claim=None, claimed_at=now)).rowcount:
                written, spend, unpriced = written + 1, spend + r["spend_usd"], unpriced + _unpriced(r)
                cells += _cell_rows(job_id, seq, r)
        if cells:
            c.execute(insert(_cells), cells)
        if written:
            c.execute(update(_jobs).where(_jobs.c.id == job_id).values(
                items_done=_jobs.c.items_done + written, spend_usd=_jobs.c.spend_usd + spend,
                unpriced_calls=_jobs.c.unpriced_calls + unpriced, updated_at=now,
                error=case((_jobs.c.status.in_(LIVE), None), else_=_jobs.c.error)))
    return written


def run_group(job_id, token, group):
    """Validate one claimed group and record it. -> how many requests were recorded.
      model endpoint unreachable   the group goes back to the queue; the job says it is waiting, and since when
      account refused              out of credit / quota / budget or no access: the job stops with why (resume later)
      any other error validating   the job fails with it (frees the group; resume() runs the rest later)
      database error recording     retried; if it lasts it reaches the worker and the group is redone after STALE_S"""
    stop = threading.Event()
    threading.Thread(target=_beat, args=(token, stop), daemon=True).start()
    try:
        try:
            spec = Spec(**get(job_id)["spec"])
            items = [Item(id=p["id"], request=p["request"], model=p["model"], recorded=p["recorded"],
                          candidates=[Candidate(**c) for c in p["candidates"]]) for _, p in group]
            res = validate_many(items, spec)
        except Unreachable as e:
            release(token)
            with _engine().begin() as c:               # the first worker to hit it sets "since"
                c.execute(update(_jobs).where(_jobs.c.id == job_id, _jobs.c.status.in_(LIVE),
                                              _jobs.c.error.is_(None)).values(
                    error="Waiting: the model endpoint has been unreachable since %s UTC (%s)"
                          % (_now().strftime("%H:%M"), str(e)[:200]), updated_at=_now()))
            raise
        except Refused as e:                           # every other group would be refused too: stop the job
            release(token)
            with _engine().begin() as c:
                c.execute(update(_jobs).where(_jobs.c.id == job_id, _jobs.c.status.in_(LIVE)).values(
                    status="stopped", error=None, updated_at=_now(), stop_reason="Stopped: the model provider or gateway refused "
                    "the account (%s). Add credit or fix access, then resume." % str(e)[:240]))
            return 0
        except Exception as e:                         # recorded on the job, so callers see why instead of "running"
            release(token)
            with _engine().begin() as c:
                c.execute(update(_jobs).where(_jobs.c.id == job_id, _jobs.c.status.in_(LIVE)).values(
                    status="failed", error=str(e)[:300], updated_at=_now()))
            return 0
        for wait in RETRY_S + (None,):                 # the calls are paid for: don't lose them to a dropped connection
            try:
                return _record(job_id, token, group, res)
            except exc.OperationalError:
                if wait is None:
                    raise
                time.sleep(wait)
            except exc.DBAPIError as e:                # data that can never be stored: fail, don't redo it forever
                release(token)
                with _engine().begin() as c:
                    c.execute(update(_jobs).where(_jobs.c.id == job_id, _jobs.c.status.in_(LIVE)).values(
                        status="failed", error="could not store results: %s" % str(e)[:250], updated_at=_now()))
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


def _num(v, digits):
    return None if v is None else round(float(v), digits)       # Postgres averages come back as Decimal


def _verdicts(ce):
    return [func.sum(case((ce.verdict == v, 1), else_=0)) for v in ("within", "outside", "unverified")]


def summary(job_id):
    """Per label across the job's finished requests, the original first as the baseline. No winner is picked.
    avg_fit_rate: the share of a candidate's runs the judge placed among the original's own answers, averaged over
    requests; avg_consistency (the original's row): how often the original's own answers agree."""
    return summaries([job_id]).get(job_id, [])


def summaries(job_ids):
    """{job_id: summary()} for several jobs in two queries, whatever their number (a page of the job list)."""
    if not job_ids:
        return {}
    ce = _cells.c
    with _engine().connect() as c:
        agg = c.execute(select(ce.job_id, ce.label, func.count(), *_verdicts(ce),
                               func.avg(case((ce.runs > 0, ce.kept * 1.0 / ce.runs))),
                               func.avg(ce.latency_ms), func.avg(ce.cost_usd),
                               *[func.sum(getattr(ce, k)) for k in ANSWERS])
                        .where(ce.job_id.in_(job_ids)).group_by(ce.job_id, ce.label)
                        .order_by(ce.job_id, func.min(ce.pos), ce.label)).all()
        models = defaultdict(list)
        for job_id, label, model in c.execute(select(ce.job_id, ce.label, ce.model).where(ce.job_id.in_(job_ids))
                                              .distinct().order_by(ce.job_id, ce.label, ce.model)):
            models[(job_id, label)].append(model)
    out = defaultdict(list)
    for job_id, label, n, within, outside, unverified, fit, lat, cost, *answers in agg:
        out[job_id].append({"label": label, "models": models[(job_id, label)], "requests": n,
                            "answers": None if label == "original" or all(a is None for a in answers) else
                            dict(zip(ANSWERS, (int(a or 0) for a in answers))),
                            "within": int(within or 0), "outside": int(outside or 0), "unverified": int(unverified or 0),
                            "avg_consistency" if label == "original" else "avg_fit_rate": _num(fit, 3),
                            "avg_latency_ms": None if lat is None else round(float(lat)), "avg_cost_usd": _num(cost, 6)})
    for rows in out.values():
        base = next((s for s in rows if s["label"] == "original"), {})
        for s in rows:
            if s["label"] == "original":
                continue
            s["within_rate"] = round(s["within"] / s["requests"], 3) if s["requests"] else None
            for k, pct in (("avg_cost_usd", "cost_vs_original_pct"), ("avg_latency_ms", "latency_vs_original_pct")):
                s[pct] = round((s[k] - base[k]) / base[k] * 100) if s.get(k) is not None and base.get(k) else None
    return dict(out)


def routing(job_id):
    """For every candidate answered by more than one model (a router): per model it sent requests to, how they did."""
    ce = _cells.c
    with _engine().connect() as c:
        rows = c.execute(select(ce.label, ce.served_model, func.count(), *_verdicts(ce))
                         .where(ce.job_id == job_id, ce.label != "original").group_by(ce.label, ce.served_model)
                         .order_by(func.min(ce.pos), ce.label, func.count().desc())).all()
    by = defaultdict(list)
    for label, served, n, within, outside, unverified in rows:
        by[label].append({"label": label, "served_model": served, "requests": n, "within": int(within or 0),
                          "outside": int(outside or 0), "unverified": int(unverified or 0)})
    return [r for label, rs in by.items() if len(rs) > 1 for r in rs]
