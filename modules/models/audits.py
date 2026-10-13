"""Model audits.

An audit = a scope (a usecase, one call-site, or requests the caller sent) + candidate models + a budget. For a
usecase or call-site it samples requests from the log by size (smallest and largest prompts, the quartiles, the
heaviest answers, a few random) and upserts those call-sites' identity (it never drops one); then ONE validation job
runs them (faithful re-runs, the judge, evidence: validation/engine.py). When the job ends, its verdicts per
call-site x candidate are written as rows of `audit_verdicts`: findings of that audit, kept as history.

What is known about a call-site is a query over those rows (`knowledge`): per model the latest verdict, valid when not
expired and measured on the call-site's current prompt version, and from the valid ones the cheapest and fastest that
pass. Audits never write the call-site record; the CHOICE of a model is the router's or a person's
(core.identity.store.set_choice).

Candidates: the strategy's picks among the generative models the gateway serves, plus the models named.
"""
import dataclasses
import datetime
import json
import random
import secrets
from collections import defaultdict

from sqlalchemy import Column, DateTime, Float, Integer, String, Table, Text, delete, select, update

from app import catalog
from app.services import db
from core import tasks
from core.identity import store
from validation import jobs

STRATEGIES = {
    "cheaper": "per call-site, the served models in lower tiers than its current model (same provider first) and the "
               "served open-weight ones; up to 3",
    "better_value": "per call-site, the served model one tier up and the same-tier served models of other providers: "
                    "a stronger model can be cheaper per call when it answers tightly; up to 3",
    "release": "the named models, against every call-site in scope",
    "revalidate": "the models already measured on each call-site, again",
    "manual": "exactly the models you name",
}
STRATEGY_IDS = tuple(STRATEGIES) + ("open_weight", "custom", "auto", "drift_watch")      # older rows
MAX_PICKS = 3
TRIGGERS = ("user", "release", "router", "api", "schedule")
DEFAULT_SAMPLES = 12
VALID_DAYS = 30                 # a verdict is re-earned after this, or when the call-site's prompt version changes

audits = Table(
    "model_audits", db.metadata,
    Column("id", String(20), primary_key=True),
    Column("created_at", DateTime, nullable=False),
    Column("trigger", String(12), nullable=False),
    Column("usecase", Text, nullable=False, default=""),
    Column("site", String(32)),                     # one call-site, or NULL for the whole usecase
    Column("strategy", String(16), nullable=False),
    Column("candidates", Text, nullable=False),     # JSON: the models asked for ("release"/"custom"), or [] for a strategy
    Column("samples", Text, nullable=False),        # JSON: {site: [{id, why, prompt_tokens, completion_tokens}]}
    Column("job_id", String(32)),
    Column("max_budget_usd", Float),
    Column("status", String(12), nullable=False),   # sampling | queued | running | done | failed | stopped
    Column("error", Text),
    Column("verdicts", Text),                       # JSON once done: {site: {candidate: verdict}}
    Column("scope", String(10)),                    # usecase | site | requests
    Column("callback_url", Text),
    Column("finished_at", DateTime),
)

verdict_rows = Table(
    "audit_verdicts", db.metadata,
    Column("audit_id", String(20), primary_key=True),
    Column("site", String(32), primary_key=True),       # "" for requests the caller sent
    Column("model", Text, primary_key=True),             # the candidate (its label)
    Column("usecase", Text),
    Column("trigger", String(12)),
    Column("verdict", String(14), nullable=False),       # passes | drifts | not verified
    Column("requests", Integer), Column("within", Integer), Column("outside", Integer), Column("unverified", Integer),
    Column("pass_rate", Float), Column("fit_rate", Float),
    Column("cost_vs_original_pct", Integer), Column("latency_vs_original_pct", Integer),
    Column("site_version", Text),                        # the call-site's prompt version when measured
    Column("at", DateTime, nullable=False),
    Column("valid_until", DateTime, nullable=False),
)
COLS = [c.name for c in audits.columns]


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


# ----------------------------------------------------------------------------------------------------- sampling
def pick(rows, n=DEFAULT_SAMPLES):
    """Representative requests of one call-site from (id, prompt_tokens, completion_tokens) rows: the smallest and
    largest prompts, the quartiles between, the two heaviest answers, then random ones up to `n`."""
    if not rows:
        return []
    by_prompt = sorted(rows, key=lambda r: (r["prompt_tokens"] or 0, r["id"]))
    by_answer = sorted(rows, key=lambda r: (-(r["completion_tokens"] or 0), r["id"]))
    chosen, out = set(), []

    def take(r, why):
        if r["id"] not in chosen and len(out) < n:
            chosen.add(r["id"])
            out.append({"id": r["id"], "why": why, "prompt_tokens": r["prompt_tokens"], "completion_tokens": r["completion_tokens"],
                        "day": r.get("day")})
    take(by_prompt[0], "smallest prompt")
    take(by_prompt[-1], "largest prompt")
    for q, why in ((0.5, "median prompt"), (0.25, "quartile prompt"), (0.75, "quartile prompt")):
        take(by_prompt[int(q * (len(by_prompt) - 1))], why)
    for r in by_answer[:2]:
        take(r, "heaviest answer")
    rest = [r for r in rows if r["id"] not in chosen]
    random.Random(42).shuffle(rest)
    for r in rest:
        take(r, "random")
    return out


def sample(usecase, site=None, n=DEFAULT_SAMPLES):
    """{site: [samples]} for the usecase (or one of its call-sites), from the request ids the day processing kept
    per call-site (the extremes and some at random, every day of the window). No BigQuery here."""
    sites = [site] if site else [r["site"] for r in store.by_usecase(usecase)]
    return {s: pick(rows, n) for s, rows in store.samples(sites).items()}


# --------------------------------------------------------------------------------------------------- candidates
def _tier_rank(m):
    return catalog._TIER_RANK.get((catalog._ORDER.get(m) or {}).get("tier"), 99)


def served_models():
    """The generative models the gateway serves (catalog ids). Off-gateway (tests): every catalog chat model."""
    from app import registry
    pool = [m for m in catalog.PRICE if (catalog._ORDER.get(m) or {}).get("mode", "chat") == "chat" and catalog.is_callable(m)]
    try:
        if registry.has_gateway():
            return [m for m in pool if registry.is_available(m)]
    except Exception:
        pass
    return pool


def _price(m):
    p = catalog.PRICE.get(m) or {}
    return (p.get("input") or 0) + (p.get("output") or 0)


def auto_candidates(model):
    """Strategy "cheaper": served models in a lower tier, same provider first then by price, and the served open-weight
    ones; up to MAX_PICKS. Seeds only: the measured cost decides."""
    m = catalog.canonical_model(model) or model
    cur, prov = _tier_rank(m), catalog.provider_of(m)
    pool = [s for s in served_models() if s != m]
    lower = sorted((s for s in pool if _tier_rank(s) > cur), key=lambda s: (catalog.provider_of(s) != prov, _tier_rank(s), _price(s)))
    ow = [s for s in pool if (catalog._ORDER.get(s) or {}).get("open_weight") and s not in lower]
    return (lower + ow)[:MAX_PICKS]


def better_value_candidates(model):
    """Strategy "better value": the served model one tier up (same provider first) and the same-tier served models of
    other providers; up to MAX_PICKS. A stronger or different model can be cheaper per call when it answers tightly."""
    m = catalog.canonical_model(model) or model
    cur, prov = _tier_rank(m), catalog.provider_of(m)
    pool = [s for s in served_models() if s != m]
    up = [s for s in pool if _tier_rank(s) < cur]
    nearest = max((_tier_rank(s) for s in up), default=None)
    up = sorted((s for s in up if _tier_rank(s) == nearest), key=lambda s: (catalog.provider_of(s) != prov, _price(s)))[:1]
    peers = sorted((s for s in pool if _tier_rank(s) == cur and catalog.provider_of(s) != prov), key=_price)
    return (up + peers)[:MAX_PICKS]


def served(models):
    """Only the models the gateway serves (a check can't call the others); unknown off-gateway -> keep as given."""
    from app import registry
    try:
        if not registry.has_gateway():
            return list(models)
        return [m for m in models if registry.is_available(m)]
    except Exception:
        return list(models)


def candidates_for(model, strategy, asked, auto=False, rec=None):
    """The candidate models for a call-site sent to `model`, by strategy, plus any models named on top; never the
    model itself under another id (a dated id is the same model)."""
    same = {model, catalog.canonical_model(model)}
    named = [m for m in asked if m and m not in same]
    if strategy in ("cheaper", "auto") or auto:
        named += [m for m in auto_candidates(model) if m not in named]
    if strategy == "better_value":
        named += [m for m in better_value_candidates(model) if m not in named]
    if strategy == "revalidate" and rec:
        named += [m for m in knowledge(rec["site"], rec)["candidates"] if m not in named and m != model]
    return served([m for m in named if m not in same])


def preview(site_models, strategy, asked):
    """What a strategy will try: {site: {current model: [candidates]}} from each call-site's known models."""
    return {st: {m: candidates_for(m, strategy, asked, rec=rec) for m in models} for st, (models, rec) in site_models.items()}


# -------------------------------------------------------------------------------------------------------- checks
def _row(r):
    d = dict(zip(COLS, r))
    for k in ("candidates", "samples", "verdicts"):
        d[k] = json.loads(d[k]) if d[k] else ([] if k == "candidates" else {} if k == "samples" else None)
    d["scope"] = d["scope"] or ("site" if d["site"] else "usecase")
    for k in ("created_at", "finished_at"):
        d[k] = d[k].isoformat(timespec="seconds") if d[k] else None
    d["usecase"] = d["usecase"] or None
    return d


def get(audit_id):
    eng = db.ready()
    with eng.connect() as c:
        r = c.execute(select(*audits.columns).where(audits.c.id == audit_id)).first()
    return _row(r) if r else None


def recent(limit=50, usecase=None, site=None, offset=0):
    eng = db.ready()
    stmt = select(*audits.columns)
    if usecase is not None:
        stmt = stmt.where(audits.c.usecase == (usecase or ""))
    if site:
        stmt = stmt.where(audits.c.site == site)
    with eng.connect() as c:
        rows = c.execute(stmt.order_by(audits.c.created_at.desc()).offset(offset).limit(limit)).all()
    return [_row(r) for r in rows]


def _set(audit_id, **vals):
    eng = db.ready()
    with eng.begin() as c:
        c.execute(update(audits).where(audits.c.id == audit_id).values(**vals))


def estimate(usecase, site=None, n=DEFAULT_SAMPLES, n_candidates=1):
    """A rough cost before running: per sample, the original re-run 4x and each candidate 5x at the site's average
    tokens, plus the judge; from the call-sites' day rows."""
    from app.services.downgrade_refset import spec as app_spec
    spec = app_spec()
    sites = [site] if site else [r["site"] for r in store.by_usecase(usecase)]
    usd = 0.0
    for by_model in store.series(sites).values():
        for model, days in by_model.items():
            calls = sum(d.get("calls") or 0 for d in days.values()) or 1
            pt = sum(d.get("prompt_tokens") or 0 for d in days.values()) / calls
            ct = sum(d.get("completion_tokens") or 0 for d in days.values()) / calls
            p = catalog.PRICE.get(catalog.canonical_model(model) or model) or {"input": 1.0, "output": 5.0}
            per_run = (pt * p["input"] + ct * p["output"]) / 1e6
            runs = spec.reference_runs + n_candidates * spec.candidate_runs
            judge = catalog.PRICE.get(spec.judge_model, {"input": 3.0, "output": 15.0})
            per_judge = ((pt + 6 * ct) * judge["input"] + 200 * judge["output"]) / 1e6
            usd += n * (runs * per_run + (1 + n_candidates * spec.candidate_runs) * per_judge)
    return round(usd, 2)


def start(**kw):
    """Start an audit: the row now, its sampling as a task any worker runs (~30 s); returns the audit at once."""
    audit_id = "aud_" + secrets.token_hex(5)
    eng = db.ready()
    with eng.begin() as c:
        c.execute(audits.insert().values(id=audit_id, created_at=_now(), trigger=kw.get("trigger", "user"),
                                         usecase=kw.get("usecase") or "", site=kw.get("site"), strategy=kw.get("strategy", "cheaper"),
                                         candidates=json.dumps(list(kw.get("asked") or [])), samples="{}",
                                         max_budget_usd=kw.get("max_budget_usd"), status="sampling",
                                         scope="site" if kw.get("site") else "usecase", callback_url=kw.get("callback_url")))

    tasks.add("sample_audit", {"audit_id": audit_id, **{k: list(v) if isinstance(v, tuple) else v for k, v in kw.items()}},
              key="sample:" + audit_id)
    return get(audit_id)


def start_requests(items, strategy="manual", asked=(), max_budget_usd=None, callback_url=None, trigger="api",
                   judge_model=None):
    """An audit of requests the caller sent (already parsed into engine items): queued at once, no sampling."""
    from app.services.downgrade_refset import spec as app_spec
    audit_id = "aud_" + secrets.token_hex(5)
    spec = app_spec()
    if judge_model:
        spec = dataclasses.replace(spec, judge_model=judge_model)
    samples = {"": [{"id": it["id"], "why": "sent"} for it in items]}
    eng = db.ready()
    with eng.begin() as c:
        c.execute(audits.insert().values(id=audit_id, created_at=_now(), trigger=trigger, usecase="", site=None,
                                         strategy=strategy, candidates=json.dumps(list(asked)), samples=json.dumps(samples),
                                         max_budget_usd=max_budget_usd, status="queued", scope="requests",
                                         callback_url=callback_url, job_id=audit_id))
    jobs.create(items, spec, max_budget_usd, callback_url, job_id=audit_id)
    return get(audit_id)


def _fill(audit_id, usecase, site=None, strategy="cheaper", asked=(), n=DEFAULT_SAMPLES, max_budget_usd=None,
          trigger="user", judge_model=None, auto=False, callback_url=None):
    """Sample, build the items and queue the validation, for an audit row that already exists. Run again (its worker
    died), it finishes what is missing and never queues the job twice."""
    from app.services.downgrade_refset import spec as app_spec
    from app.web.public import _logged_bodies
    from connectors.bigquery.source import to_trace
    a = get(audit_id)
    if not a or a["status"] != "sampling":
        return
    if jobs.get(audit_id):                                 # the job was queued; only the status write was lost
        _set(audit_id, job_id=audit_id, status="queued")
        return
    try:
        if strategy not in STRATEGY_IDS:
            raise ValueError("strategy must be one of %s" % ", ".join(STRATEGIES))
        if strategy in ("release", "manual", "custom") and not asked and not auto:
            raise ValueError("%s needs model names" % strategy)
        samples = sample(usecase, site, n)
        if not samples:
            raise ValueError("no requests to sample for this scope: the log's days are not processed yet")
        ids = [s["id"] for ss in samples.values() for s in ss]
        oldest = min(s["day"] for ss in samples.values() for s in ss if s.get("day")) if ids else None
        bodies = _logged_bodies(ids, oldest)
        items = []
        for st, ss in samples.items():
            for s in ss:
                if s["id"] not in bodies or not bodies[s["id"]][1]:
                    continue
                req, resp = bodies[s["id"]]
                cands = candidates_for(req.get("model"), strategy, asked, auto, store.get(st))
                if not cands:
                    continue
                trace = to_trace(req, resp, usecase or "log")
                items.append({"id": s["id"], "request": trace, "model": req.get("model"), "recorded": trace["output"] or None,
                              "candidates": [{"label": (m + " now") if strategy == "drift_watch" else m, "model": m} for m in cands]})
        if not items:
            raise ValueError("no candidate for these call-sites' models (nothing cheaper / no open-weight swap in the catalog)")
        spec = app_spec()
        if judge_model:
            spec = dataclasses.replace(spec, judge_model=judge_model)
        _set(audit_id, samples=json.dumps(samples))          # before the job: a retry after a crash needs them
        job = jobs.create(items, spec, max_budget_usd, callback_url, job_id=audit_id)
        _set(audit_id, job_id=job["id"], status="queued")
    except Exception as e:
        if jobs.get(audit_id):                             # another worker queued it first (a handed-back task)
            _set(audit_id, job_id=audit_id, status="queued")
            return
        _set(audit_id, status="failed", error="%s: %s" % (type(e).__name__, e), finished_at=_now())
        raise


# -------------------------------------------------------------------------------------------------------- results
def _verdicts(audit):
    """{site: {candidate: {requests, within, outside, unverified, pass_rate, cost_vs_original_pct,
    latency_vs_original_pct, consistency}}} from the job's results, by the samples' ids."""
    site_of = {s["id"]: st for st, ss in audit["samples"].items() for s in ss}
    agg = defaultdict(lambda: defaultdict(lambda: {"requests": 0, "within": 0, "outside": 0, "unverified": 0,
                                                   "cost": [], "lat": [], "base_cost": [], "base_lat": [], "fit": []}))
    for r in jobs.results(audit["job_id"]):
        st = site_of.get(r["id"])
        if st is None:                                     # "" is the key of your requests
            continue
        base_lat, base_cost = jobs.original_averages(r)
        for cand in r["candidates"]:
            a = agg[st][cand["label"]]
            a["requests"] += 1
            a[cand["verdict"]] += 1
            if cand.get("cost_usd") is not None and base_cost:
                a["cost"].append(cand["cost_usd"]); a["base_cost"].append(base_cost)
            if cand.get("latency_ms") is not None and base_lat:
                a["lat"].append(cand["latency_ms"]); a["base_lat"].append(base_lat)
            if cand.get("fit_rate") is not None:
                a["fit"].append(cand["fit_rate"])
    out = {}
    for st, by in agg.items():
        out[st] = {}
        for cand, a in by.items():
            n = a["requests"] or 1
            pct = lambda xs, bs: round((sum(xs) / len(xs) - sum(bs) / len(bs)) / (sum(bs) / len(bs)) * 100) if xs and bs and sum(bs) else None
            out[st][cand] = {"requests": a["requests"], "within": a["within"], "outside": a["outside"], "unverified": a["unverified"],
                             "pass_rate": round(a["within"] / n, 3), "cost_vs_original_pct": pct(a["cost"], a["base_cost"]),
                             "latency_vs_original_pct": pct(a["lat"], a["base_lat"]),
                             "fit_rate": round(sum(a["fit"]) / len(a["fit"]), 3) if a["fit"] else None,
                             "verdict": "passes" if a["within"] == a["requests"] else "drifts" if a["outside"] else "not verified"}
    return out


def refresh_status(audit):
    """Follow the job; when it ends, write the verdicts (once). Returns the current audit."""
    if not audit["job_id"]:
        t = tasks.last("sample:" + audit["id"]) if audit["status"] == "sampling" else None
        if t and t["status"] == "failed":                  # its workers kept dying before it could say why
            _set(audit["id"], status="failed", error=t["error"], finished_at=_now())
            return get(audit["id"])
        return audit
    job = jobs.get(audit["job_id"])
    if not job:
        return audit
    if job["status"] in ("queued", "running"):            # running, or resumed after it stopped
        if audit["status"] != job["status"]:
            _set(audit["id"], status=job["status"], finished_at=None)
        return get(audit["id"])
    if audit["status"] == job["status"] and audit["finished_at"]:
        return audit                                       # its verdicts are written
    verdicts = _verdicts(audit)
    at = _now()
    rows = []
    for st, by in verdicts.items():
        rec = store.get(st) if st else None
        version = rec["versions"][-1]["version"] if rec and rec["versions"] else None
        for cand, v in by.items():
            rows.append({"audit_id": audit["id"], "site": st, "model": cand, "usecase": audit["usecase"] or "",
                         "trigger": audit["trigger"], "verdict": v["verdict"], "requests": v["requests"],
                         "within": v["within"], "outside": v["outside"], "unverified": v["unverified"],
                         "pass_rate": v["pass_rate"], "fit_rate": v["fit_rate"],
                         "cost_vs_original_pct": v["cost_vs_original_pct"], "latency_vs_original_pct": v["latency_vs_original_pct"],
                         "site_version": version, "at": at, "valid_until": at + datetime.timedelta(days=VALID_DAYS)})
    eng = db.ready()
    with eng.begin() as c:
        c.execute(delete(verdict_rows).where(verdict_rows.c.audit_id == audit["id"]))
        if rows:
            c.execute(verdict_rows.insert(), rows)
    _set(audit["id"], status="done" if job["status"] == "done" else job["status"], verdicts=json.dumps(verdicts),
         finished_at=at, error=job.get("error"))
    return get(audit["id"])


def knowledge(site, rec=None):
    """What the audits found on a call-site: per model its latest verdict (with `valid`: not expired and measured on
    the current prompt version), and among the valid ones the cheapest and fastest that pass."""
    rec = rec or store.get(site)
    version = rec["versions"][-1]["version"] if rec and rec.get("versions") else None
    eng = db.ready()
    with eng.connect() as c:
        rows = c.execute(select(*verdict_rows.columns).where(verdict_rows.c.site == str(site))
                         .order_by(verdict_rows.c.at.desc())).all()
    now = _now()
    cands = {}
    for r in rows:
        d = dict(zip([col.name for col in verdict_rows.columns], r))
        if d["model"] in cands:
            continue                                        # an older verdict of the same model
        d["valid"] = d["valid_until"] > now and d["site_version"] in (None, version)
        d["audit"] = d.pop("audit_id")
        for k in ("at", "valid_until"):
            d[k] = d[k].isoformat(timespec="seconds")
        cands[d["model"]] = d
    return {"candidates": cands, **conclusion({m: v for m, v in cands.items() if v["valid"]})}


def conclusion(cands):
    """Among the candidates that pass on every sampled request, the cheapest and the fastest by MEASURED cost and
    latency against the original (a stronger model that answers tightly can be both)."""
    ok = {m: v for m, v in cands.items() if v.get("verdict") == "passes"}
    cheapest = min((m for m in ok if ok[m].get("cost_vs_original_pct") is not None), key=lambda m: ok[m]["cost_vs_original_pct"], default=None)
    fastest = min((m for m in ok if ok[m].get("latency_vs_original_pct") is not None), key=lambda m: ok[m]["latency_vs_original_pct"], default=None)
    pick = lambda m, k: None if m is None or ok[m][k] >= 0 else {"model": m, k: ok[m][k], "audit": ok[m]["audit"]}
    return {"cheapest": pick(cheapest, "cost_vs_original_pct"), "fastest": pick(fastest, "latency_vs_original_pct")}


def usecase_rollup(usecase):
    """Per usecase, from what the audits found: on how many call-sites a cheaper model passes (valid verdicts), and
    the saving at the window's volume."""
    recs = store.by_usecase(usecase or None)
    series = store.series([r["site"] for r in recs])
    sites, with_pick, saving, base, picks = 0, 0, 0.0, 0.0, {}
    for r in recs:
        sites += 1
        k = knowledge(r["site"], r)
        cost = sum(d.get("cost") or 0 for days in series[r["site"]].values() for d in days.values())
        base += cost
        if k["cheapest"]:
            with_pick += 1
            saving += cost * (-k["cheapest"]["cost_vs_original_pct"]) / 100
            picks[r["site"]] = k["cheapest"]
    return {"sites": sites, "with_cheaper": with_pick, "saving": round(saving, 2), "cost": round(base, 2), "picks": picks}


def for_job(job_id):
    """The audit running under this job id, or None."""
    return get(job_id)
