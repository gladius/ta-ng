"""The call-sites table in the app's database: one row per call-site of the window, written on every refresh from
the identity tables in BigQuery. Pages read this, never BigQuery. The site id is the hash of the call-site's
signature (its stable instruction lines), the same id every module keys on."""
import datetime
import json

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Table, Text, delete, func, select

from app.services import db

callsites = Table(
    "callsites", db.metadata,
    Column("site", String(32), primary_key=True),
    Column("usecase", Text),
    Column("signature", Text, nullable=False),       # the stable lines, one per line: the fingerprint
    Column("calls", Integer, nullable=False),
    Column("first_day", String(10)),
    Column("last_day", String(10)),
    Column("versions", Text, nullable=False),        # JSON [{version, first, last}]
    Column("static_chars", Integer),
    Column("dynamic_chars", Integer),
    Column("static_after", Integer),                 # fixed characters after the first per-call line
    Column("tool_sets", Integer),
    Column("unsure", Boolean, nullable=False),
    Column("window_days", Integer, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)
FIELDS = [c.name for c in callsites.columns]
UNSURE_BELOW = 5


def sync(sites, days):
    """Replace the table with the identity's sites of the window."""
    eng = db.ready()
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    rows = []
    for s in sites:
        sig = (s.get("signature") or "").strip()
        rows.append({"site": str(s["site"]), "usecase": s.get("usecase"), "signature": sig, "calls": int(s["n"] or 0),
                     "first_day": str(s.get("first_day") or ""), "last_day": str(s.get("last_day") or ""),
                     "versions": json.dumps([{"version": str(v["version"]), "first": str(v["first"]), "last": str(v["last"])}
                                             for v in s.get("versions") or []]),
                     "static_chars": int(s.get("static_chars") or 0), "dynamic_chars": int(s.get("dynamic_chars") or 0),
                     "static_after": int(s.get("static_after") or 0), "tool_sets": int(s.get("tool_sets") or 0),
                     "unsure": int(s["n"] or 0) < UNSURE_BELOW or not sig, "window_days": days, "updated_at": now})
    with eng.begin() as c:
        c.execute(delete(callsites))
        if rows:
            c.execute(callsites.insert(), rows)
    return len(rows)


def _row(r):
    d = dict(zip(FIELDS, r))
    d["versions"] = json.loads(d["versions"] or "[]")
    d["signature"] = [l for l in (d["signature"] or "").split("\n") if l]
    d["updated_at"] = d["updated_at"].isoformat(timespec="seconds") if d["updated_at"] else None
    return d


def get(site):
    eng = db.ready()
    with eng.connect() as c:
        r = c.execute(select(*callsites.columns).where(callsites.c.site == str(site))).first()
    return _row(r) if r else None


def by_usecase(usecase):
    eng = db.ready()
    with eng.connect() as c:
        rows = c.execute(select(*callsites.columns).where(callsites.c.usecase.is_(None) if usecase is None
                                                          else callsites.c.usecase == usecase)
                         .order_by(callsites.c.calls.desc())).all()
    return [_row(r) for r in rows]


def usecases(q=None, page=1, per_page=50):
    """Usecases with their call-site counts, biggest first."""
    eng = db.ready()
    stmt = select(callsites.c.usecase, func.count().label("sites"), func.sum(callsites.c.calls).label("calls"),
                  func.sum(func.cast(callsites.c.unsure, Integer)).label("unsure")).group_by(callsites.c.usecase)
    if q:
        stmt = stmt.where(callsites.c.usecase.ilike("%%%s%%" % q))
    with eng.connect() as c:
        total = c.execute(select(func.count()).select_from(stmt.subquery())).scalar()
        rows = c.execute(stmt.order_by(func.sum(callsites.c.calls).desc()).offset((page - 1) * per_page).limit(per_page)).all()
    return {"page": page, "per_page": per_page, "total": total,
            "usecases": [{"usecase": r[0], "sites": r[1], "calls": int(r[2] or 0), "unsure": int(r[3] or 0)} for r in rows]}
