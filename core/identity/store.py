"""The core records in the app's database: usecases, call-sites, and each call-site's history by day.

usecases        the cheap list from the log (calls in the window, days; `listed_at`) plus its call-site count and when
                its days were last processed (`analysed_at`).
callsites       one row per call-site, each part written by one owner only:
                  identity        the loop (core/identity/loop.py) and the day processing (core/ledger/refresh.py):
                                  the fingerprint phrases (markers), its versions (each with phrases, need, days,
                                  placed count, the model's reason), the prompt's regions (signature), the layout
                  profile         the loop, from the same model call (name, what it does, task, output, fixed, per_call)
                  cache_findings  the cache analysis (its latest diagnosis)
                  choice          the router or a person: the model this call-site uses, the audit it relied on, ...
                What the audits found is not stored here: it is a query over audit_verdicts (modules/models/audits.py).
site_days       one row per call-site, day and model: the day's numbers and its cache gaps. A day is written once
                (processed again: replaced); days older than the window are dropped. Pages sum these rows.
site_samples    a few request ids per call-site, day and model (the extremes and some at random): what a model audit
                re-runs, fetched from the log by id for just those days.
unplaced        calls no fingerprint placed that day (shown as such), capped per usecase and day.
processed_days  which days of the log have been processed, and when.

A request belongs to a call-site when its head holds `need` of one of its versions' phrases."""
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
    Column("template", Text, nullable=False),                  # unused ("[]")
    Column("markers", Text),                                   # JSON list of phrases: the current version's fingerprint (placement, router)
    Column("ambiguous", Boolean),                              # unused (False)
    Column("signature", Text, nullable=False),                 # JSON [{kind: fixed|per_call, what}]: the prompt's regions in order
    Column("calls", Integer, nullable=False),
    Column("first_day", String(10)),
    Column("last_day", String(10)),
    Column("versions", Text, nullable=False),                  # JSON [{version, phrases, need, first, last, placed, reason, model}]
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
           "cost_nocache", "calls_unpriced", "calls_with_tools", "avg_ms", "avg_messages")
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
    Column("duration_ms", Float),
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
    Column("template", Text, nullable=False),                  # unused ("[]")
)
COVERAGE = ("calls", "other_format", "no_usage", "unpriced", "repeated", "placed", "unplaced")
usecase_days = Table(                                      # what each usecase's day was made of, and how much was analysed
    "usecase_days", db.metadata,
    Column("usecase", Text, primary_key=True),
    Column("day", String(10), primary_key=True),
    *[Column(k, Integer) for k in COVERAGE],
)
day_sessions = Table(                                      # the BigQuery session of a day's last script run: a failure
    "day_sessions", db.metadata,                           # after the script resumes from its temp tables (24 h)
    Column("day", String(10), primary_key=True),
    Column("session_id", Text, nullable=False),
    Column("at", DateTime, nullable=False),
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
def fingerprints():
    """Every call-site's fingerprints, for the day script's placement: one entry per version with phrases,
    [{usecase, site, markers, need}], the phrases joined by '|'. A few hundred bytes per call-site, so thousands fit
    in one BigQuery request."""
    eng = db.ready()
    with eng.connect() as c:
        rows = c.execute(select(callsites.c.usecase, callsites.c.site, callsites.c.markers, callsites.c.versions)).all()
    out = []
    for uc, site, markers, versions in rows:
        vs = [v for v in json.loads(versions or "[]") if v.get("phrases")] or [{"phrases": json.loads(markers or "[]")}]
        for v in vs:
            if v["phrases"]:
                out.append({"usecase": uc, "site": site, "markers": "|".join(v["phrases"]), "need": int(v.get("need") or _need(v["phrases"]))})
    return out


def _need(phrases):
    return len(phrases) - 1 if len(phrases) >= 4 else len(phrases)


def brief(usecase):
    """The usecase's call-sites as the loop shows them to the model: [{site, name, phrases, calls}]."""
    eng = db.ready()
    with eng.connect() as c:
        rows = c.execute(select(callsites.c.site, callsites.c.name, callsites.c.markers, callsites.c.calls)
                         .where(callsites.c.usecase == (usecase or "")).order_by(callsites.c.calls.desc())).all()
    return [{"site": r.site, "name": r.name, "phrases": json.loads(r.markers or "[]"), "calls": r.calls or 0} for r in rows]


def save_site(rec):
    """A call-site the loop made or gave a new version (rec: core.identity.loop._record). New: the row with its first
    version and profile. A version of a known one (version_of): the version appended, the fingerprint and profile
    renewed. The counts over the window are set by save_identity after the day's numbers are in."""
    eng = db.ready()
    version = {"version": _version_id(rec["phrases"]), "phrases": rec["phrases"], "need": rec["need"], "first": rec["day"], "last": rec["day"],
               "placed": rec.get("placed") or 0, "reason": rec.get("reason") or "", "model": rec["profile"]["model"],
               "mixed": bool(rec.get("mixed")), "mixed_why": rec.get("mixed_why") or ""}
    lay = {k: rec[k] for k in ("static_chars", "dynamic_chars", "static_after") if rec.get(k) is not None}
    with eng.begin() as c:
        row = c.execute(select(callsites.c.versions, callsites.c.calls).where(callsites.c.site == rec["site"])).first()
        if row is None:
            c.execute(callsites.insert().values(site=rec["site"], usecase=rec["usecase"] or "", template="[]", markers=json.dumps(rec["phrases"]),
                                                ambiguous=False, signature=json.dumps(rec.get("regions") or []), calls=rec.get("placed") or 0,
                                                first_day=rec["day"], last_day=rec["day"], versions=json.dumps([version]),
                                                unsure=(rec.get("placed") or 0) < UNSURE_BELOW, window_days=7, seen_at=_now(), **lay))
        else:
            versions = [v for v in json.loads(row.versions or "[]") if v.get("version") != version["version"]] + [version]
            c.execute(update(callsites).where(callsites.c.site == rec["site"]).values(
                markers=json.dumps(rec["phrases"]), signature=json.dumps(rec.get("regions") or []), versions=json.dumps(versions),
                seen_at=_now(), **lay))
    p = rec["profile"]
    set_profile(rec["site"], p)


def _version_id(phrases):
    import hashlib
    return hashlib.sha1("|".join(sorted(phrases)).encode()).hexdigest()[:12]


def save_identity(rec, window):
    """Upsert a call-site's summary over the window (the processing's). rec: site, usecase, calls, first_day, last_day,
    versions (their `last` days kept up), and the layout when known."""
    eng = db.ready()
    vals = {"usecase": rec["usecase"] or "", "calls": int(rec["calls"]), "first_day": rec["first_day"], "last_day": rec["last_day"],
            "versions": json.dumps(rec["versions"]), "unsure": int(rec["calls"]) < UNSURE_BELOW, "window_days": window, "seen_at": _now(),
            **{k: rec[k] for k in ("static_chars", "dynamic_chars", "static_after", "tool_sets") if rec.get(k) is not None}}
    with eng.begin() as c:
        if c.execute(update(callsites).where(callsites.c.site == rec["site"]).values(**vals)).rowcount == 0:
            c.execute(callsites.insert().values(site=rec["site"], template="[]", markers=json.dumps(rec.get("markers") or []),
                                                ambiguous=False, signature="[]", **vals))


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


def add_to_days(rows):
    """Add numbers onto existing day rows (calls of an earlier day placed only later), inserting the missing ones."""
    if not rows:
        return
    eng = db.ready()
    with eng.begin() as c:
        for r in rows:
            key = (site_days.c.site == r["site"]) & (site_days.c.day == r["day"]) & (site_days.c.model == (r["model"] or ""))
            have = c.execute(select(site_days).where(key)).first()
            if have is None:
                c.execute(site_days.insert().values(site=r["site"], day=r["day"], model=r["model"] or "",
                                                    **{k: r.get(k) for k in NUMBERS}, gaps=json.dumps(r.get("gaps") or {})))
            else:
                c.execute(update(site_days).where(key).values(**{k: (getattr(have, k) or 0) + (r.get(k) or 0)
                                                                 for k in NUMBERS if k not in ("avg_ms", "avg_messages")}))


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
        for t in (site_days, site_samples, unplaced, processed_days, usecase_days):
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
                                               "prompt_tokens": r["prompt_tokens"], "completion_tokens": r["completion_tokens"],
                                               "duration_ms": r.get("duration_ms")}
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
                                           "usecase": r["usecase"] or "", "template": "[]"}
                                          for r in rows[i:i + 500]])


def unplaced_rows(usecase):
    eng = db.ready()
    with eng.connect() as c:
        rows = c.execute(select(unplaced).where(unplaced.c.usecase == (usecase or ""))).all()
    return [dict(r._mapping) for r in rows]


def drop_unplaced(ids):
    if not ids:
        return
    eng = db.ready()
    with eng.begin() as c:
        for i in range(0, len(ids), 500):
            c.execute(delete(unplaced).where(unplaced.c.id.in_(ids[i:i + 500])))


def day_session(day, session_id=None):
    """Remember the session of the day's script run, or read it back (None when older than 20 h)."""
    eng = db.ready()
    with eng.begin() as c:
        if session_id:
            c.execute(delete(day_sessions).where(day_sessions.c.day == day))
            c.execute(day_sessions.insert().values(day=day, session_id=session_id, at=_now()))
            return session_id
        r = c.execute(select(day_sessions.c.session_id, day_sessions.c.at).where(day_sessions.c.day == day)).first()
    return r[0] if r and (_now() - r[1]).total_seconds() < 20 * 3600 else None


def replace_coverage(days, rows):
    eng = db.ready()
    with eng.begin() as c:
        if days:
            c.execute(delete(usecase_days).where(usecase_days.c.day.in_(list(days))))
        if rows:
            c.execute(usecase_days.insert(), [{"usecase": r["usecase"] or "", "day": str(r["day"]), **{k: int(r.get(k) or 0) for k in COVERAGE}}
                                              for r in rows])


def coverage(usecase=None):
    """{coverage counts summed over the window}, for one usecase or all."""
    eng = db.ready()
    stmt = select(*[func.sum(getattr(usecase_days.c, k)) for k in COVERAGE])
    if usecase is not None:
        stmt = stmt.where(usecase_days.c.usecase == (usecase or ""))
    with eng.connect() as c:
        r = c.execute(stmt).first()
    return {k: int(v or 0) for k, v in zip(COVERAGE, r)}


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
