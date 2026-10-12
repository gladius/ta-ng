"""The core records in the app's database: one row per usecase, one row per call-site.

usecases   the cheap list from the log (calls, days; `listed_at`) plus, once a usecase was analysed, its roll-up and
           profile and when (`analysed_at`); decisions JSON for module-level conclusions.
callsites  identity refreshed by each analysis (usecase, signature lines = the fingerprint, calls, days, versions,
           layout, series per model per day), profile written once by the profiler, decisions JSON per module.

The site id is the hash of the signature; every module keys on it. A request belongs to a call-site when its
usecase matches and its system prompt (numbers masked) contains all the signature lines."""
import datetime
import json

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Table, Text, delete, func, select, update

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
    Column("analysing", Boolean, nullable=False, default=False),
    Column("error", Text),
    Column("sites", Integer),
    Column("profile", Text),                                   # JSON {name, does} once synthesised
    Column("decisions", Text, nullable=False, default="{}"),
)
callsites = Table(
    "callsites", db.metadata,
    Column("site", String(32), primary_key=True),
    Column("usecase", Text, nullable=False, default=""),
    # identity
    Column("signature", Text, nullable=False),                 # JSON list of lines
    Column("calls", Integer, nullable=False),
    Column("first_day", String(10)),
    Column("last_day", String(10)),
    Column("versions", Text, nullable=False),                  # JSON [{version, first, last}]
    Column("static_chars", Integer),
    Column("dynamic_chars", Integer),
    Column("static_after", Integer),                           # fixed characters after the first per-call line
    Column("tool_sets", Integer),
    Column("unsure", Boolean, nullable=False),
    Column("window_days", Integer, nullable=False),
    Column("seen_at", DateTime, nullable=False),
    Column("series", Text),                                    # JSON {model: {day: {calls, prompt_tokens, ...}}}
    # profile
    Column("name", Text),
    Column("does", Text),
    Column("task", String(16)),
    Column("output", String(16)),
    Column("fixed", Text),
    Column("per_call", Text),
    Column("profile_model", Text),
    Column("profiled_at", DateTime),
    # decisions
    Column("decisions", Text, nullable=False, default="{}"),
)
UNSURE_BELOW = 5
SERIES_KEYS = ("calls", "prompt_tokens", "cached_tokens", "written_tokens", "completion_tokens", "calls_cached", "cost",
               "cost_nocache", "calls_with_tools", "avg_ms", "avg_messages")


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _iso(v):
    return v.isoformat(timespec="seconds") if v else None


# ---------------------------------------------------------------------------------------------------- usecases
def list_usecases(rows, days):
    """Replace the list with the log's usecases of the window; analysis results of known usecases stay."""
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
                c.execute(usecases.insert().values(usecase=uc, decisions="{}", analysing=False, **vals))
        if seen:
            c.execute(delete(usecases).where(usecases.c.usecase.notin_(list(seen))))
    return len(rows)


def listed_at():
    eng = db.ready()
    with eng.connect() as c:
        return c.execute(select(func.max(usecases.c.listed_at))).scalar()


def mark_analysing(uc, on, error=None):
    eng = db.ready()
    vals = {"analysing": on, "error": error}
    if not on and not error:
        vals["analysed_at"] = _now()
    with eng.begin() as c:
        c.execute(update(usecases).where(usecases.c.usecase == (uc or "")).values(**vals))


def set_usecase_summary(uc, sites, profile=None):
    eng = db.ready()
    vals = {"sites": sites}
    if profile is not None:
        vals["profile"] = json.dumps(profile)
    with eng.begin() as c:
        c.execute(update(usecases).where(usecases.c.usecase == (uc or "")).values(**vals))


def set_usecase_decision(uc, module, value):
    eng = db.ready()
    with eng.begin() as c:
        cur = c.execute(select(usecases.c.decisions).where(usecases.c.usecase == (uc or ""))).scalar()
        d = json.loads(cur or "{}")
        d[module] = value
        c.execute(update(usecases).where(usecases.c.usecase == (uc or "")).values(decisions=json.dumps(d)))


def _urow(r):
    d = dict(zip([c.name for c in usecases.columns], r))
    d["usecase"] = d["usecase"] or None
    d["profile"] = json.loads(d["profile"]) if d["profile"] else None
    d["decisions"] = json.loads(d["decisions"] or "{}")
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
def _identity_row(s, days, now, series):
    sig = [l for l in (s.get("signature") or "").split("\n") if l]
    return {"usecase": s.get("usecase") or "", "signature": json.dumps(sig), "calls": int(s["n"] or 0),
            "first_day": str(s.get("first_day") or ""), "last_day": str(s.get("last_day") or ""),
            "versions": json.dumps([{"version": str(v["version"]), "first": str(v["first"]), "last": str(v["last"])}
                                    for v in s.get("versions") or []]),
            "static_chars": int(s.get("static_chars") or 0), "dynamic_chars": int(s.get("dynamic_chars") or 0),
            "static_after": int(s.get("static_after") or 0), "tool_sets": int(s.get("tool_sets") or 0),
            "unsure": int(s["n"] or 0) < UNSURE_BELOW or not sig, "window_days": days, "seen_at": now,
            "series": json.dumps(series or {})}


def series_by_site(ledger_rows):
    """ledger rows -> {site: {model: {day: {...}}}}"""
    out = {}
    for r in ledger_rows:
        day = str(r["day"])[:10]
        out.setdefault(str(r["site"]), {}).setdefault(r["model"] or "", {})[day] = {k: r.get(k) for k in SERIES_KEYS}
    return out


def sync(sites, ledger_rows, days, usecase=None):
    """Upsert the identity of every call-site the analysis found (the whole window, or one usecase); profiles and
    decisions stay; sites of the scope no longer found are dropped. Returns the ids."""
    eng = db.ready()
    now = _now()
    series = series_by_site(ledger_rows)
    ids = []
    with eng.begin() as c:
        have = {r[0] for r in c.execute(select(callsites.c.site)).all()}
        for s in sites:
            site = str(s["site"])
            row = _identity_row(s, days, now, series.get(site))
            if site in have:
                c.execute(update(callsites).where(callsites.c.site == site).values(**row))
            else:
                c.execute(callsites.insert().values(site=site, decisions="{}", **row))
            ids.append(site)
        gone = callsites.c.site.notin_(ids) if ids else True
        if usecase is None:
            c.execute(delete(callsites).where(gone))
        else:
            c.execute(delete(callsites).where((callsites.c.usecase == (usecase or "")) & gone))
    return ids


def set_profile(site, p):
    eng = db.ready()
    with eng.begin() as c:
        c.execute(update(callsites).where(callsites.c.site == str(site)).values(
            name=p["name"], does=p["does"], task=p["task"], output=p["output"], fixed=p.get("fixed"),
            per_call=p.get("per_call"), profile_model=p["model"], profiled_at=_now()))


def set_decision(site, module, value):
    eng = db.ready()
    with eng.begin() as c:
        cur = c.execute(select(callsites.c.decisions).where(callsites.c.site == str(site))).scalar()
        d = json.loads(cur or "{}")
        d[module] = value
        c.execute(update(callsites).where(callsites.c.site == str(site)).values(decisions=json.dumps(d)))


def _row(r):
    d = dict(zip([c.name for c in callsites.columns], r))
    d["signature"] = json.loads(d["signature"] or "[]")
    d["versions"] = json.loads(d["versions"] or "[]")
    d["decisions"] = json.loads(d["decisions"] or "{}")
    d["series"] = json.loads(d["series"] or "{}")
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
