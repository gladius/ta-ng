"""The call-site record: one row per call-site in the app's database.

  identity   refreshed every run from BigQuery: usecase, signature lines (the fingerprint), calls, days, versions, layout
  profile    written once by the profiler (name, what it does, task, output, fixed / per-call), kept across refreshes
  decisions  JSON, what each module concluded: {"cache": {...}, "models": {...}, "router": {...}}

The site id is the hash of the signature; every module keys on it. A request belongs to a call-site when its
usecase matches and its system prompt (numbers masked) contains all the signature lines."""
import datetime
import json

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Table, Text, delete, func, select, update

from app.services import db

callsites = Table(
    "callsites", db.metadata,
    Column("site", String(32), primary_key=True),
    Column("usecase", Text, nullable=False, default=""),       # "" when the log has no usecase tag
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
    Column("seen_at", DateTime, nullable=False),               # last refresh that found it
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
    Column("decisions", Text, nullable=False, default="{}"),   # JSON
)
IDENTITY = ("usecase", "signature", "calls", "first_day", "last_day", "versions", "static_chars", "dynamic_chars",
            "static_after", "tool_sets", "unsure", "window_days", "seen_at")
PROFILE = ("name", "does", "task", "output", "fixed", "per_call", "profile_model", "profiled_at")
UNSURE_BELOW = 5


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _identity_row(s, days, now):
    sig = [l for l in (s.get("signature") or "").split("\n") if l]
    return {"usecase": s.get("usecase") or "", "signature": json.dumps(sig), "calls": int(s["n"] or 0),
            "first_day": str(s.get("first_day") or ""), "last_day": str(s.get("last_day") or ""),
            "versions": json.dumps([{"version": str(v["version"]), "first": str(v["first"]), "last": str(v["last"])}
                                    for v in s.get("versions") or []]),
            "static_chars": int(s.get("static_chars") or 0), "dynamic_chars": int(s.get("dynamic_chars") or 0),
            "static_after": int(s.get("static_after") or 0), "tool_sets": int(s.get("tool_sets") or 0),
            "unsure": int(s["n"] or 0) < UNSURE_BELOW or not sig, "window_days": days, "seen_at": now}


def sync(sites, days):
    """Upsert the identity of every call-site the refresh found; profiles and decisions stay. Returns the ids."""
    eng = db.ready()
    now = _now()
    ids = []
    with eng.begin() as c:
        have = {r[0] for r in c.execute(select(callsites.c.site)).all()}
        for s in sites:
            site = str(s["site"])
            row = _identity_row(s, days, now)
            if site in have:
                c.execute(update(callsites).where(callsites.c.site == site).values(**row))
            else:
                c.execute(callsites.insert().values(site=site, decisions="{}", **row))
            ids.append(site)
    return ids


def set_profile(site, p):
    eng = db.ready()
    with eng.begin() as c:
        c.execute(update(callsites).where(callsites.c.site == str(site)).values(
            name=p["name"], does=p["does"], task=p["task"], output=p["output"], fixed=p.get("fixed"),
            per_call=p.get("per_call"), profile_model=p["model"], profiled_at=_now()))


def set_decision(site, module, value):
    """Store one module's decision for the site (merged into the decisions JSON)."""
    eng = db.ready()
    with eng.begin() as c:
        cur = c.execute(select(callsites.c.decisions).where(callsites.c.site == str(site))).scalar()
        d = json.loads(cur or "{}")
        d[module] = value
        c.execute(update(callsites).where(callsites.c.site == str(site)).values(decisions=json.dumps(d)))


def clear_decisions(module):
    eng = db.ready()
    with eng.begin() as c:
        for site, cur in c.execute(select(callsites.c.site, callsites.c.decisions)).all():
            d = json.loads(cur or "{}")
            if module in d:
                del d[module]
                c.execute(update(callsites).where(callsites.c.site == site).values(decisions=json.dumps(d)))


def _row(r):
    d = dict(zip([c.name for c in callsites.columns], r))
    d["signature"] = json.loads(d["signature"] or "[]")
    d["versions"] = json.loads(d["versions"] or "[]")
    d["decisions"] = json.loads(d["decisions"] or "{}")
    for k in ("seen_at", "profiled_at"):
        d[k] = d[k].isoformat(timespec="seconds") if d[k] else None
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


def usecases(q=None, page=1, per_page=50):
    """Usecases with their call-site counts, biggest first."""
    eng = db.ready()
    stmt = select(callsites.c.usecase, func.count().label("sites"), func.sum(callsites.c.calls).label("calls"),
                  func.sum(func.cast(callsites.c.unsure, Integer)).label("unsure"),
                  func.count(callsites.c.name).label("profiled")).group_by(callsites.c.usecase)
    if q:
        stmt = stmt.where(callsites.c.usecase.ilike("%%%s%%" % q))
    with eng.connect() as c:
        total = c.execute(select(func.count()).select_from(stmt.subquery())).scalar()
        rows = c.execute(stmt.order_by(func.sum(callsites.c.calls).desc()).offset((page - 1) * per_page).limit(per_page)).all()
    return {"page": page, "per_page": per_page, "total": total,
            "usecases": [{"usecase": r[0] or None, "sites": r[1], "calls": int(r[2] or 0), "unsure": int(r[3] or 0), "profiled": r[4]}
                         for r in rows]}


def drop_missing(ids):
    """Forget call-sites the window no longer has (their identity rows); decisions and profiles go with them."""
    eng = db.ready()
    with eng.begin() as c:
        c.execute(delete(callsites).where(callsites.c.site.notin_(list(ids))))
