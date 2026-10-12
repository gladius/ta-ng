"""Model checks.

A check = scope (a usecase, or one call-site) + a sample per call-site + candidate models + a budget. It samples
requests from the log by size (smallest and largest prompts, the quartiles, the heaviest answers, a few random),
submits ONE validation job (faithful re-runs, the judge, evidence: validation/engine.py unchanged) and, when the job
ends, writes a verdict per call-site x candidate: on the check (history, provenance) and on the call-site record as
decisions["models"] (the latest per candidate, with the check it came from). The usecase record gets the roll-up.

Candidates come from a preset or a list: "cheaper" (the catalog's next lower tier of each site's own model),
"open_weight" (the catalog's in-tier open-weight swap), "release" (one named model), "custom".
"""
import dataclasses
import datetime
import json
import random
import secrets
import threading
from collections import defaultdict

from sqlalchemy import Column, DateTime, Float, String, Table, Text, select, update

from app import catalog
from app.services import db
from core.identity import store
from core.ledger import build
from validation import jobs

PRESETS = ("cheaper", "open_weight", "release", "custom", "auto")
TRIGGERS = ("user", "release", "router", "api")
DEFAULT_SAMPLES = 12

checks = Table(
    "checks", db.metadata,
    Column("id", String(20), primary_key=True),
    Column("created_at", DateTime, nullable=False),
    Column("trigger", String(12), nullable=False),
    Column("usecase", Text, nullable=False, default=""),
    Column("site", String(32)),                     # one call-site, or NULL for the whole usecase
    Column("preset", String(12), nullable=False),
    Column("candidates", Text, nullable=False),     # JSON: the models asked for ("release"/"custom"), or [] for a preset
    Column("samples", Text, nullable=False),        # JSON: {site: [{id, why, prompt_tokens, completion_tokens}]}
    Column("job_id", String(32)),
    Column("max_budget_usd", Float),
    Column("status", String(12), nullable=False),   # sampling | queued | running | done | failed | stopped
    Column("error", Text),
    Column("summary", Text),                        # JSON once done: {site: {candidate: verdict}}
    Column("finished_at", DateTime),
)
COLS = [c.name for c in checks.columns]


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


# ----------------------------------------------------------------------------------------------------- sampling
_POOL = """
SELECT CAST(site AS STRING) AS site, id, prompt_tokens, completion_tokens FROM call_rows
WHERE cost IS NOT NULL AND completion_tokens > 0{scope}
QUALIFY ROW_NUMBER() OVER (PARTITION BY site ORDER BY FARM_FINGERPRINT(id)) <= 2000
"""


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
            out.append({"id": r["id"], "why": why, "prompt_tokens": r["prompt_tokens"], "completion_tokens": r["completion_tokens"]})
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


def sample(usecase, site=None, n=DEFAULT_SAMPLES, days=30):
    """{site: [samples]} for the usecase (or one of its call-sites), from the log, in one BigQuery session."""
    b = build.Build(days, usecase or "")
    try:
        scope = " AND CAST(site AS STRING) = @site" if site else ""
        rows = b.s.query(_POOL.format(scope=scope), {"site": site} if site else None)
    finally:
        b.close()
    by = defaultdict(list)
    for r in rows:
        by[r["site"]].append(dict(r))
    return {s: pick(rs, n) for s, rs in by.items()}


# --------------------------------------------------------------------------------------------------- candidates
def auto_candidates(model):
    """What Auto tries for a call-site on `model`: the catalog's next cheaper tier and its open-weight swap. The
    measurement decides; these are only the seeds. ponytail: add one tier up and the cross-provider peer once the
    conclusion can rank them by measured cost."""
    out = []
    for c in (catalog.next_cheaper(model), catalog.open_weight_target(model)):
        if c and c != model and c not in out:
            out.append(c)
    return out


def served(models):
    """Only the models the gateway serves (a check can't call the others); unknown off-gateway -> keep as given."""
    from app import registry
    try:
        if not registry.has_gateway():
            return list(models)
        return [m for m in models if registry.is_available(m)]
    except Exception:
        return list(models)


def candidates_for(model, preset, asked, auto=False):
    """The candidate models for a call-site sent to `model`: the ones asked for, plus Auto's seeds."""
    if preset == "cheaper":
        return served(auto_candidates(model)[:1])
    if preset == "open_weight":
        c = catalog.open_weight_target(model)
        return served([c] if c else [])
    out = [m for m in asked if m and m != model]
    if auto or preset == "auto":
        out += [m for m in auto_candidates(model) if m not in out]
    return served(out)


# -------------------------------------------------------------------------------------------------------- checks
def _row(r):
    d = dict(zip(COLS, r))
    for k in ("candidates", "samples", "summary"):
        d[k] = json.loads(d[k]) if d[k] else ([] if k == "candidates" else {} if k == "samples" else None)
    for k in ("created_at", "finished_at"):
        d[k] = d[k].isoformat(timespec="seconds") if d[k] else None
    d["usecase"] = d["usecase"] or None
    return d


def get(check_id):
    eng = db.ready()
    with eng.connect() as c:
        r = c.execute(select(*checks.columns).where(checks.c.id == check_id)).first()
    return _row(r) if r else None


def recent(limit=50, usecase=None, site=None):
    eng = db.ready()
    stmt = select(*checks.columns)
    if usecase is not None:
        stmt = stmt.where(checks.c.usecase == (usecase or ""))
    if site:
        stmt = stmt.where(checks.c.site == site)
    with eng.connect() as c:
        rows = c.execute(stmt.order_by(checks.c.created_at.desc()).limit(limit)).all()
    return [_row(r) for r in rows]


def _set(check_id, **vals):
    eng = db.ready()
    with eng.begin() as c:
        c.execute(update(checks).where(checks.c.id == check_id).values(**vals))


def estimate(usecase, site=None, n=DEFAULT_SAMPLES, n_candidates=1):
    """A rough cost before running: per sample, the original re-run 4x and each candidate 5x at the site's average
    tokens, plus the judge; from the call-site records' series."""
    from app.services.downgrade_refset import spec as app_spec
    spec = app_spec()
    recs = [store.get(site)] if site else store.by_usecase(usecase)
    usd = 0.0
    for rec in recs:
        if not rec:
            continue
        for model, days in rec["series"].items():
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
    """create() in the background (sampling takes ~30 s); returns the check id at once."""
    check_id = "chk_" + secrets.token_hex(5)
    eng = db.ready()
    with eng.begin() as c:
        c.execute(checks.insert().values(id=check_id, created_at=_now(), trigger=kw.get("trigger", "user"),
                                         usecase=kw.get("usecase") or "", site=kw.get("site"), preset=kw.get("preset", "cheaper"),
                                         candidates=json.dumps(list(kw.get("asked") or [])), samples="{}",
                                         max_budget_usd=kw.get("max_budget_usd"), status="sampling"))

    def go():
        try:
            _fill(check_id, **kw)
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()
    return get(check_id)


def _fill(check_id, usecase, site=None, preset="cheaper", asked=(), n=DEFAULT_SAMPLES, max_budget_usd=None,
          trigger="user", judge_model=None, days=30, auto=False):
    """The body of create() for a check row that already exists."""
    from app.services.downgrade_refset import spec as app_spec
    from app.web.validations import _logged_bodies
    from connectors.bigquery.source import to_trace
    try:
        if preset not in PRESETS:
            raise ValueError("preset must be one of %s" % ", ".join(PRESETS))
        if preset in ("release", "custom") and not asked and not auto:
            raise ValueError("%s needs candidate models" % preset)
        samples = sample(usecase, site, n, days)
        if not samples:
            raise ValueError("no requests to sample for this scope")
        ids = [s["id"] for ss in samples.values() for s in ss]
        bodies = _logged_bodies(ids)
        items = []
        for st, ss in samples.items():
            for s in ss:
                if s["id"] not in bodies or not bodies[s["id"]][1]:
                    continue
                req, resp = bodies[s["id"]]
                cands = candidates_for(req.get("model"), preset, asked, auto)
                if not cands:
                    continue
                trace = to_trace(req, resp, usecase or "log")
                items.append({"id": s["id"], "request": trace, "model": req.get("model"), "recorded": trace["output"] or None,
                              "candidates": [{"label": m, "model": m} for m in cands]})
        if not items:
            raise ValueError("no candidate for these call-sites' models (nothing cheaper / no open-weight swap in the catalog)")
        spec = app_spec()
        if judge_model:
            spec = dataclasses.replace(spec, judge_model=judge_model)
        job = jobs.create(items, spec, max_budget_usd)
        _set(check_id, samples=json.dumps(samples), job_id=job["id"], status="queued")
    except Exception as e:
        _set(check_id, status="failed", error="%s: %s" % (type(e).__name__, e), finished_at=_now())
        raise


# -------------------------------------------------------------------------------------------------------- results
def _verdicts(check):
    """{site: {candidate: {requests, within, outside, unverified, pass_rate, cost_vs_original_pct,
    latency_vs_original_pct, consistency}}} from the job's results, by the samples' ids."""
    site_of = {s["id"]: st for st, ss in check["samples"].items() for s in ss}
    agg = defaultdict(lambda: defaultdict(lambda: {"requests": 0, "within": 0, "outside": 0, "unverified": 0,
                                                   "cost": [], "lat": [], "base_cost": [], "base_lat": [], "fit": []}))
    for r in jobs.results(check["job_id"]):
        st = site_of.get(r["id"])
        if not st:
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


def refresh_status(check):
    """Follow the job; when it ends, write the verdicts (once). Returns the current check."""
    if check["status"] in ("done", "failed", "stopped") or not check["job_id"]:
        return check
    job = jobs.get(check["job_id"])
    if not job:
        return check
    if job["status"] in ("queued", "running"):
        if check["status"] != job["status"]:
            _set(check["id"], status=job["status"])
        return get(check["id"])
    verdicts = _verdicts(check)
    at = _now()
    touched = set()
    for st, by in verdicts.items():
        rec = store.get(st)
        if not rec:
            continue
        cur = (rec["decisions"].get("models") or {}).get("candidates", {})
        for cand, v in by.items():
            cur[cand] = {**v, "check": check["id"], "at": at.isoformat(timespec="seconds"), "trigger": check["trigger"]}
        store.set_decision(st, "models", {"candidates": cur, **conclusion(cur)})
        touched.add(rec["usecase"] or "")
    for uc in touched:
        store.set_usecase_decision(uc, "models", usecase_rollup(uc))
    _set(check["id"], status="done" if job["status"] == "done" else job["status"], summary=json.dumps(verdicts),
         finished_at=at, error=job.get("error"))
    return get(check["id"])


def conclusion(cands):
    """Among the candidates that pass on every sampled request, the cheapest and the fastest by MEASURED cost and
    latency against the original (a stronger model that answers tightly can be both)."""
    ok = {m: v for m, v in cands.items() if v.get("verdict") == "passes"}
    cheapest = min((m for m in ok if ok[m].get("cost_vs_original_pct") is not None), key=lambda m: ok[m]["cost_vs_original_pct"], default=None)
    fastest = min((m for m in ok if ok[m].get("latency_vs_original_pct") is not None), key=lambda m: ok[m]["latency_vs_original_pct"], default=None)
    pick = lambda m, k: None if m is None or ok[m][k] >= 0 else {"model": m, k: ok[m][k], "check": ok[m]["check"]}
    return {"cheapest": pick(cheapest, "cost_vs_original_pct"), "fastest": pick(fastest, "latency_vs_original_pct")}


def usecase_rollup(usecase):
    """Per usecase: on how many call-sites a cheaper model passed, and the saving at the window's volume."""
    recs = store.by_usecase(usecase or None)
    sites, with_pick, saving, base = 0, 0, 0.0, 0.0
    for r in recs:
        sites += 1
        m = r["decisions"].get("models") or {}
        cost = sum(d.get("cost") or 0 for days in r["series"].values() for d in days.values())
        base += cost
        if m.get("cheapest"):
            with_pick += 1
            saving += cost * (-m["cheapest"]["cost_vs_original_pct"]) / 100
    return {"sites": sites, "with_cheaper": with_pick, "saving": round(saving, 2), "cost": round(base, 2),
            "picks": {r["site"]: r["decisions"]["models"]["cheapest"] for r in recs if (r["decisions"].get("models") or {}).get("cheapest")}}
