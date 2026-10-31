"""The audit of the audit: after a day is processed, how far to trust it. Checks on our own numbers, no model:
  consistency   a usecase's calls = placed + not placed + other format + logged twice
  placement     the share of a usecase's chat calls placed in a call-site (flag under PLACED_MIN)
  fingerprint   call-sites with no phrases
  named         call-sites with no name (the model call that names a pre-pass call-site failed)
  mixed         call-sites whose latest version the model read as more than one template (loop.mixed)
  same_name     two or more call-sites of one usecase with one name (one template split: the over-split we saw)
  models        call-sites where no model takes ONE_MODEL of the calls (routing, fallback, or two call-sites merged):
                shown to look at, not counted against the day's trust
  volume        call-sites whose calls jumped or fell by VOLUME_X against their previous day
  unpriced      the share of a usecase's calls with no price (flag over UNPRICED_MAX)
All from our own tables: no model call, no read of the log. The grouping is also checked where it is made: every
call-site carries its phrases, the calls they placed and the model's reason (core/identity/loop.py)."""
import datetime
import json

from sqlalchemy import Boolean, Column, DateTime, String, Table, Text, delete, select

from app.services import db

PLACED_MIN = 0.9
UNPRICED_MAX = 0.05
VOLUME_X = 5
ONE_MODEL = 0.9
GROUPING = ("named", "mixed", "same_name")      # the grouping checks the trust line counts

checks = Table(
    "quality_checks", db.metadata,
    Column("day", String(10), primary_key=True),
    Column("kind", String(16), primary_key=True),
    Column("usecase", Text, primary_key=True),
    Column("site", String(32), primary_key=True),          # "" for a usecase-level check
    Column("ok", Boolean, nullable=False),
    Column("detail", Text),                                # JSON
    Column("at", DateTime, nullable=False),
)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


# ----------------------------------------------------------------------------------------------- automatic checks
def automatic(day):
    """The checks on our own numbers for the day -> rows for quality_checks."""
    from core.identity import store
    out = []
    eng = db.ready()
    with eng.connect() as c:
        cov = c.execute(select(store.usecase_days).where(store.usecase_days.c.day == day)).all()
        sites = {r.site: r for r in c.execute(select(store.callsites.c.site, store.callsites.c.usecase, store.callsites.c.markers,
                                                       store.callsites.c.name, store.callsites.c.versions))}
        rows = c.execute(select(store.site_days.c.site, store.site_days.c.day, store.site_days.c.model, store.site_days.c.calls)
                         .where(store.site_days.c.day <= day)).all()
    for r in cov:
        chat = r.calls - r.other_format - r.repeated
        out.append(("consistency", r.usecase, "", r.calls == r.placed + r.unplaced + r.other_format + r.repeated,
                    {"calls": r.calls, "placed": r.placed, "unplaced": r.unplaced, "other_format": r.other_format, "repeated": r.repeated}))
        if chat > 0:
            share = r.placed / chat
            out.append(("placement", r.usecase, "", share >= PLACED_MIN, {"placed": r.placed, "chat_calls": chat, "share": round(share, 3)}))
            out.append(("unpriced", r.usecase, "", r.unpriced <= UNPRICED_MAX * chat, {"unpriced": r.unpriced, "share": round(r.unpriced / chat, 3)}))
    today, before = {}, {}
    models = {}
    prev_day = max((d for _, d, _, _ in rows if d < day), default=None)
    for site, d, model, calls in rows:
        if d == day:
            today[site] = today.get(site, 0) + (calls or 0)
            models.setdefault(site, {})[model] = models.setdefault(site, {}).get(model, 0) + (calls or 0)
        elif d == prev_day:
            before[site] = before.get(site, 0) + (calls or 0)
    names = {}                                            # (usecase, name) -> the day's call-sites of that name
    for site, n in today.items():
        rec = sites.get(site)
        uc = rec.usecase if rec else ""
        phrases = json.loads(rec.markers or "[]") if rec else []
        if not phrases:
            out.append(("fingerprint", uc, site, False, {"phrases": 0}))
        name = (rec.name or "").strip() if rec else ""
        out.append(("named", uc, site, bool(name), {} if name else {"name": None}))
        if name:
            names.setdefault((uc, name.lower()), []).append(site)
        versions = json.loads(rec.versions or "[]") if rec else []
        last = max(versions, key=lambda v: v.get("version") or 0) if versions else {}
        out.append(("mixed", uc, site, not last.get("mixed"), {"why": last.get("mixed_why") or ""} if last.get("mixed") else {}))
        mix = {m: k for m, k in models.get(site, {}).items() if k >= 0.1 * n}
        if len(mix) > 1:
            out.append(("models", uc, site, max(mix.values()) >= ONE_MODEL * n,
                        {"calls": {m: int(k) for m, k in sorted(mix.items(), key=lambda kv: -kv[1])}}))
        b = before.get(site)
        if prev_day and b and (n > VOLUME_X * b or n * VOLUME_X < b):
            out.append(("volume", uc, site, False, {"today": int(n), "previous_day": int(b)}))
    for (uc, name), group in names.items():
        for site in group:
            out.append(("same_name", uc, site, len(group) == 1,
                        {} if len(group) == 1 else {"name": sites[site].name, "others": [x for x in group if x != site]}))
    return out


def run(day, **_):
    """The day's checks (the automatic ones; a model spot check is no longer needed: every call-site carries its
    phrases, placed count and reason from the loop)."""
    rows = automatic(day)
    save(day, rows)
    return len(rows)


def save(day, rows):
    eng = db.ready()
    now = _now()
    with eng.begin() as c:
        c.execute(delete(checks).where(checks.c.day == day))
        for kind, usecase, site, ok, detail in rows:
            c.execute(checks.insert().values(day=day, kind=kind, usecase=usecase or "", site=site or "", ok=bool(ok),
                                             detail=json.dumps(detail), at=now))


def report(day=None):
    """The latest (or given) day's checks: totals per kind and every failure."""
    eng = db.ready()
    with eng.connect() as c:
        day = day or c.execute(select(checks.c.day).order_by(checks.c.day.desc()).limit(1)).scalar()
        if not day:
            return {"day": None, "kinds": {}, "failures": [], "days": days(), "trust": None}
        rows = c.execute(select(checks).where(checks.c.day == day)).all()
    out = {"day": day, "kinds": {}, "failures": [], "days": days()}
    for r in rows:
        k = out["kinds"].setdefault(r.kind, {"checked": 0, "failed": 0})
        k["checked"] += 1
        k["failed"] += 0 if r.ok else 1
        item = {"kind": r.kind, "usecase": r.usecase or None, "site": r.site or None, "ok": r.ok, "detail": json.loads(r.detail or "{}")}
        if not r.ok:
            out["failures"].append(item)
    out["trust"] = trust(out["kinds"])
    return out


def trust(kinds):
    """One judgement on the day: trustworthy, mostly, or not yet, and why."""
    k = lambda name: kinds.get(name, {"checked": 0, "failed": 0})
    placed, sums, fps = k("placement"), k("consistency"), k("fingerprint")
    placed_ok = placed["checked"] - placed["failed"]
    grouping = {g: k(g)["failed"] for g in GROUPING}
    reasons = ["%d of %d usecases placed at 90%% or more" % (placed_ok, placed["checked"]),
               "numbers add up" if not sums["failed"] else "numbers don't add up in %d usecases" % sums["failed"],
               "every call-site has its phrases" if not fps["failed"] else "%d call-sites without phrases" % fps["failed"]]
    said = {"named": "without a name", "mixed": "read as more than one template", "same_name": "sharing a name with another"}
    bad = ["%d %s" % (n, said[g]) for g, n in grouping.items() if n]
    reasons.append("call-sites named, one template each, no two alike" if not bad else "call-sites " + ", ".join(bad))
    if sums["failed"] or (placed["checked"] and placed_ok < 0.75 * placed["checked"]):
        level = "not yet"
    elif placed["checked"] and placed_ok >= 0.9 * placed["checked"] and not fps["failed"] and not any(grouping.values()):
        level = "trustworthy"
    else:
        level = "mostly"
    return {"level": level, "reasons": reasons}


def days():
    """The days processed (newest first), to pick one to check."""
    from core.identity import store
    return sorted(store.days_done(), reverse=True)
