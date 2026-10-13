"""The core records in the app's database: usecases, call-sites, and each call-site's history by day.

usecases        the cheap list from the log (calls in the window, days; `listed_at`) plus its call-site count and when
                its days were last processed (`analysed_at`).
callsites       one row per call-site, each part written by one owner only:
                  identity        the day processing (core/ledger/refresh.py): template (the window hashes its calls
                                  share), the start of a sample prompt, versions, prompt layout
                  profile         the profiler, once
                  cache_findings  the cache analysis (its latest diagnosis)
                  choice          the router or a person: the model this call-site uses, the audit it relied on, ...
                What the audits found is not stored here: it is a query over audit_verdicts (modules/models/audits.py).
site_days       one row per call-site, day and model: the day's numbers and its cache gaps. A day is written once
                (processed again: replaced); days older than the window are dropped. Pages sum these rows.
site_samples    a few request ids per call-site, day and model (the extremes and some at random): what a model audit
                re-runs, fetched from the log by id for just those days.
unplaced        calls the day script could not place (no template text in common with enough other calls that day),
                with their window hashes: grouped across days by the processing, capped per usecase and day.
processed_days  which days of the log have been processed, and when.

A request belongs to a call-site when its system prompt holds HITS of the call-site's marker phrases."""
import datetime
import json
from collections import defaultdict

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Table, Text, delete, func, select, update

from app.services import db

usecases = Table(
    "usecases", db.metadata,
    Column("usecase", Text, primary_key=True),                 # "" when the log has no usecase tag
    Column("calls", Integer, nullable=False),
    Column("first_day", String(10)),
    Column("last_day", String(10)),
    Column("window_days", Integer, nullable=False),
    Column("listed_at", DateTime, nullable=False),
    Column("analysed_at", DateTime),
    Column("error", Text),
    Column("sites", Integer),
    Column("profile", Text),                                   # JSON {name, does} once synthesised
)
callsites = Table(
    "callsites", db.metadata,
    Column("site", String(32), primary_key=True),
    Column("usecase", Text, nullable=False, default=""),
    # identity
    Column("template", Text, nullable=False),                  # JSON list of phrases its calls share (linking, versions)
    Column("markers", Text),                                   # JSON list of MARKERS phrases: the fingerprint (placement, router)
    Column("signature", Text, nullable=False),                 # JSON: the first lines of a sample prompt
    Column("calls", Integer, nullable=False),
    Column("first_day", String(10)),
    Column("last_day", String(10)),
    Column("versions", Text, nullable=False),                  # JSON [{version, first, last}]
    Column("static_chars", Integer),
    Column("dynamic_chars", Integer),
    Column("static_after", Integer),                           # fixed characters after the first per-call text
    Column("tool_sets", Integer),
    Column("unsure", Boolean, nullable=False),
    Column("window_days", Integer, nullable=False),
    Column("seen_at", DateTime, nullable=False),
    # profile
    Column("name", Text),
    Column("does", Text),
    Column("task", String(16)),
    Column("output", String(16)),
    Column("fixed", Text),
    Column("per_call", Text),
    Column("profile_model", Text),
    Column("profiled_at", DateTime),
    # findings and the choice
    Column("cache_findings", Text),                            # JSON, the cache analysis's latest
    Column("choice", Text),                                    # JSON {model, audit, guard, until, by, at} or null
)
NUMBERS = ("calls", "prompt_tokens", "cached_tokens", "written_tokens", "completion_tokens", "calls_cached", "cost",
           "cost_nocache", "calls_with_tools", "avg_ms", "avg_messages")
site_days = Table(
    "site_days", db.metadata,
    Column("site", String(32), primary_key=True),
    Column("day", String(10), primary_key=True),
    Column("model", Text, primary_key=True),
    *[Column(k, Float) for k in NUMBERS],
    Column("gaps", Text),                                      # JSON {bucket: {calls, prefix_tokens, prompt_tokens, cached, written}}
)
site_samples = Table(
    "site_samples", db.metadata,
    Column("site", String(32), primary_key=True),
    Column("id", Text, primary_key=True),
    Column("day", String(10), nullable=False),
    Column("model", Text),
    Column("prompt_tokens", Integer),
    Column("completion_tokens", Integer),
)
unplaced = Table(
    "unplaced", db.metadata,
    Column("id", Text, primary_key=True),
    Column("usecase", Text, nullable=False, index=True),
    Column("day", String(10), nullable=False),
    Column("model", Text),
    Column("prompt_tokens", Integer),
    Column("cached_tokens", Integer),
    Column("written_tokens", Integer),
    Column("completion_tokens", Integer),
    Column("cost", Float),
    Column("cost_nocache", Float),
    Column("template", Text, nullable=False),                  # JSON list of window hashes
)
processed_days = Table(
    "processed_days", db.metadata,
    Column("day", String(10), primary_key=True),
    Column("calls", Integer),
    Column("unplaced", Integer),
    Column("at", DateTime, nullable=False),
)
UNSURE_BELOW = 5


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _iso(v):
    return v.isoformat(timespec="seconds") if v else None


# ---------------------------------------------------------------------------------------------------- usecases
def list_usecases(rows, days):
    """Replace the list with the log's usecases of the window; what is known about them stays."""
    eng = db.ready()
    now = _now()
    with eng.begin() as c:
        have = {r[0] for r in c.execute(select(usecases.c.usecase)).all()}
        seen = set()
        for r in rows:
            uc = r["usecase"] or ""
            seen.add(uc)
            vals = {"calls": int(r["calls"] or 0), "first_day": str(r["first_day"] or ""), "last_day": str(r["last_day"] or ""),
                    "window_days": days, "listed_at": now}
            if uc in have:
                c.execute(update(usecases).where(usecases.c.usecase == uc).values(**vals))
            else:
                c.execute(usecases.insert().values(usecase=uc, **vals))
        if seen:
            c.execute(delete(usecases).where(usecases.c.usecase.notin_(list(seen))))
    return len(rows)


def listed_at():
    eng = db.ready()
    with eng.connect() as c:
        return c.execute(select(func.max(usecases.c.listed_at))).scalar()


def set_usecase_summary(uc, sites, error=None):
    """After its days were processed: its call-site count, when, and any error."""
    eng = db.ready()
    with eng.begin() as c:
        c.execute(update(usecases).where(usecases.c.usecase == (uc or "")).values(sites=sites, analysed_at=_now(), error=error))


def _urow(r):
    d = dict(zip([c.name for c in usecases.columns], r))
    d["usecase"] = d["usecase"] or None
    d["profile"] = json.loads(d["profile"]) if d["profile"] else None
    for k in ("listed_at", "analysed_at"):
        d[k] = _iso(d[k])
    return d


def get_usecase(uc):
    eng = db.ready()
    with eng.connect() as c:
        r = c.execute(select(*usecases.columns).where(usecases.c.usecase == (uc or ""))).first()
    return _urow(r) if r else None


def usecase_list(q=None, page=1, per_page=50):
    eng = db.ready()
    stmt = select(*usecases.columns)
    if q:
        stmt = stmt.where(usecases.c.usecase.ilike("%%%s%%" % q))
    with eng.connect() as c:
        total = c.execute(select(func.count()).select_from(stmt.subquery())).scalar()
        rows = c.execute(stmt.order_by(usecases.c.calls.desc()).offset((page - 1) * per_page).limit(per_page)).all()
    return {"page": page, "per_page": per_page, "total": total, "usecases": [_urow(r) for r in rows]}


# --------------------------------------------------------------------------------------------------- callsites
def templates(usecase):
    """{site: set of template phrases} of the usecase's known call-sites."""
    eng = db.ready()
    with eng.connect() as c:
        rows = c.execute(select(callsites.c.site, callsites.c.template).where(callsites.c.usecase == (usecase or ""))).all()
    return {r[0]: set(json.loads(r[1] or "[]")) for r in rows}


def fingerprints():
    """Every call-site's fingerprint, for the day script's placement: [{usecase, site, markers, template}], the
    phrases joined by '|'."""
    eng = db.ready()
    with eng.connect() as c:
        rows = c.execute(select(callsites.c.usecase, callsites.c.site, callsites.c.markers, callsites.c.template)).all()
    return [{"usecase": r[0], "site": r[1], "markers": "|".join(json.loads(r[2] or "[]")), "template": "|".join(json.loads(r[3] or "[]"))}
            for r in rows if r[2]]


def save_identity(rec, window):
    """Upsert a call-site's identity part (the processing's). rec: site, usecase, template, signature, calls,
    first_day, last_day, versions, static_chars, dynamic_chars, static_after, tool_sets."""
    eng = db.ready()
    vals = {"usecase": rec["usecase"] or "", "template": json.dumps(sorted(rec["template"])), "markers": json.dumps(rec.get("markers") or []),
            "signature": json.dumps(rec.get("signature") or []), "calls": int(rec["calls"]),
            "first_day": rec["first_day"], "last_day": rec["last_day"], "versions": json.dumps(rec["versions"]),
            "static_chars": rec.get("static_chars"), "dynamic_chars": rec.get("dynamic_chars"),
            "static_after": rec.get("static_after"), "tool_sets": rec.get("tool_sets"),
            "unsure": int(rec["calls"]) < UNSURE_BELOW, "window_days": window, "seen_at": _now()}
    with eng.begin() as c:
        if c.execute(update(callsites).where(callsites.c.site == rec["site"]).values(**vals)).rowcount == 0:
            c.execute(callsites.insert().values(site=rec["site"], **vals))


def drop_sites(sites):
    """Call-sites with no day left in the window, and everything of theirs."""
    if not sites:
        return
    eng = db.ready()
    with eng.begin() as c:
        for t in (site_days, site_samples, callsites):
            c.execute(delete(t).where(t.c.site.in_(list(sites))))


def set_profile(site, p):
    eng = db.ready()
    with eng.begin() as c:
        c.execute(update(callsites).where(callsites.c.site == str(site)).values(
            name=p["name"], does=p["does"], task=p["task"], output=p["output"], fixed=p.get("fixed"),
            per_call=p.get("per_call"), profile_model=p["model"], profiled_at=_now()))


def _set_json(site, column, value):
    eng = db.ready()
    with eng.begin() as c:
        c.execute(update(callsites).where(callsites.c.site == str(site)).values(**{column: json.dumps(value) if value is not None else None}))


def set_cache_findings(site, value):
    """The cache analysis's latest findings for the site (its only writer)."""
    _set_json(site, "cache_findings", value)


def set_choice(site, value):
    """The model this call-site uses: written by the router or a person only, never by an audit."""
    _set_json(site, "choice", value)


def _row(r):
    d = dict(zip([c.name for c in callsites.columns], r))
    d["template"] = json.loads(d["template"] or "[]")
    d["markers"] = json.loads(d["markers"] or "[]")
    d["signature"] = json.loads(d["signature"] or "[]")
    d["versions"] = json.loads(d["versions"] or "[]")
    d["cache"] = json.loads(d["cache_findings"]) if d["cache_findings"] else None
    d["choice"] = json.loads(d["choice"]) if d["choice"] else None
    d.pop("cache_findings")
    for k in ("seen_at", "profiled_at"):
        d[k] = _iso(d[k])
    d["profile"] = ({"name": d["name"], "does": d["does"], "task": d["task"], "output": d["output"], "fixed": d["fixed"],
                     "per_call": d["per_call"], "model": d["profile_model"], "created_at": d["profiled_at"]} if d["name"] else None)
    d["usecase"] = d["usecase"] or None
    return d


def get(site):
    eng = db.ready()
    with eng.connect() as c:
        r = c.execute(select(*callsites.columns).where(callsites.c.site == str(site))).first()
    return _row(r) if r else None


def all_sites():
    eng = db.ready()
    with eng.connect() as c:
        rows = c.execute(select(*callsites.columns).order_by(callsites.c.calls.desc())).all()
    return [_row(r) for r in rows]


def by_usecase(usecase):
    eng = db.ready()
    with eng.connect() as c:
        rows = c.execute(select(*callsites.columns).where(callsites.c.usecase == (usecase or ""))
                         .order_by(callsites.c.calls.desc())).all()
    return [_row(r) for r in rows]


# --------------------------------------------------------------------------------------------------- site_days
def replace_days(days, rows):
    """The numbers of those days, for every site: the old rows of the days go, these come in.
    rows: {site, day, model, <NUMBERS>, gaps}."""
    eng = db.ready()
    with eng.begin() as c:
        if days:
            c.execute(delete(site_days).where(site_days.c.day.in_(list(days))))
        for i in range(0, len(rows), 500):
            c.execute(site_days.insert(), [{"site": r["site"], "day": r["day"], "model": r["model"] or "",
                                            **{k: r.get(k) for k in NUMBERS}, "gaps": json.dumps(r.get("gaps") or {})}
                                           for r in rows[i:i + 500]])


def series(sites):
    """{site: {model: {day: {<NUMBERS>, gaps}}}} for those sites, from site_days."""
    out = defaultdict(lambda: defaultdict(dict))
    if not sites:
        return out
    eng = db.ready()
    with eng.connect() as c:
        rows = c.execute(select(site_days).where(site_days.c.site.in_(list(sites)))).all()
    for r in rows:
        out[r.site][r.model][r.day] = {**{k: getattr(r, k) for k in NUMBERS}, "gaps": json.loads(r.gaps or "{}")}
    return out


def site_summary():
    """{site: (calls, first day, last day)} over site_days, for the identity rows."""
    eng = db.ready()
    with eng.connect() as c:
        rows = c.execute(select(site_days.c.site, func.sum(site_days.c.calls), func.min(site_days.c.day), func.max(site_days.c.day))
                         .group_by(site_days.c.site)).all()
    return {r[0]: (int(r[1] or 0), r[2], r[3]) for r in rows}


def trim(cutoff):
    """Drop the days before `cutoff` (ISO date) everywhere. -> the sites left with no day."""
    eng = db.ready()
    with eng.begin() as c:
        for t in (site_days, site_samples, unplaced, processed_days):
            c.execute(delete(t).where(t.c.day < cutoff))
        gone = c.execute(select(callsites.c.site).where(callsites.c.site.notin_(select(site_days.c.site).distinct()))).scalars().all()
    return gone


# ------------------------------------------------------------------------------------------------ site_samples
def replace_samples(days, rows):
    eng = db.ready()
    with eng.begin() as c:
        if days:
            c.execute(delete(site_samples).where(site_samples.c.day.in_(list(days))))
        for i in range(0, len(rows), 500):
            c.execute(site_samples.insert(), [{"site": r["site"], "id": r["id"], "day": r["day"], "model": r["model"] or "",
                                               "prompt_tokens": r["prompt_tokens"], "completion_tokens": r["completion_tokens"]}
                                              for r in rows[i:i + 500]])


def samples(sites):
    """{site: [{id, day, model, prompt_tokens, completion_tokens}]}"""
    out = defaultdict(list)
    if not sites:
        return out
    eng = db.ready()
    with eng.connect() as c:
        for r in c.execute(select(site_samples).where(site_samples.c.site.in_(list(sites)))).all():
            out[r.site].append(dict(r._mapping))
    return out


# ---------------------------------------------------------------------------------------------------- unplaced
def replace_unplaced(days, rows):
    eng = db.ready()
    with eng.begin() as c:
        if days:
            c.execute(delete(unplaced).where(unplaced.c.day.in_(list(days))))
        for i in range(0, len(rows), 500):
            c.execute(unplaced.insert(), [{**{k: r.get(k) for k in ("id", "usecase", "day", "model", "prompt_tokens", "cached_tokens",
                                                                   "written_tokens", "completion_tokens", "cost", "cost_nocache")},
                                           "usecase": r["usecase"] or "", "template": json.dumps(list(r["template"]))}
                                          for r in rows[i:i + 500]])


def unplaced_rows(usecase):
    eng = db.ready()
    with eng.connect() as c:
        rows = c.execute(select(unplaced).where(unplaced.c.usecase == (usecase or ""))).all()
    return [{**r._mapping, "template": json.loads(r.template)} for r in rows]


def drop_unplaced(ids):
    if not ids:
        return
    eng = db.ready()
    with eng.begin() as c:
        for i in range(0, len(ids), 500):
            c.execute(delete(unplaced).where(unplaced.c.id.in_(ids[i:i + 500])))


# ------------------------------------------------------------------------------------------- processed_days
def mark_day(day, calls, n_unplaced):
    eng = db.ready()
    with eng.begin() as c:
        c.execute(delete(processed_days).where(processed_days.c.day == day))
        c.execute(processed_days.insert().values(day=day, calls=calls, unplaced=n_unplaced, at=_now()))


def days_done():
    """{day: {calls, unplaced, at}}"""
    eng = db.ready()
    with eng.connect() as c:
        rows = c.execute(select(processed_days)).all()
    return {r.day: {"calls": r.calls, "unplaced": r.unplaced, "at": _iso(r.at)} for r in rows}
