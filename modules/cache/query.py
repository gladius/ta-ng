"""What the cache pages show, read from the app's tables (ledger_daily, cache_gaps, cache_diagnoses, callsites,
profiles). Same JSON shapes the pages had when the report was one stored blob."""
from collections import defaultdict

from sqlalchemy import func, select

from app.services import db
from core import profiler
from core.identity import store as callsites
from core.ledger import tables
from modules.cache.report import SUM, total

_U = lambda u: None if u in ("", None) else u            # usecases are stored as "" when untagged


def _days_series(rows):
    days = defaultdict(lambda: {k: 0 for k in SUM})
    for r in rows:
        for k in SUM:
            days[r["day"]][k] += r.get(k) or 0
    return dict(days)


def _share(d):
    return d["cached_tokens"] / d["prompt_tokens"] if d.get("prompt_tokens") else 0.0


def trend(days):
    keys = sorted(days)
    if len(keys) < 8:
        return None
    recent, before = total([days[k] for k in keys[-7:]]), total([days[k] for k in keys[-14:-7]])
    if not before["prompt_tokens"]:
        return None
    return {"recent": round(_share(recent), 3), "before": round(_share(before), 3), "delta": round(_share(recent) - _share(before), 3)}


def built_at():
    eng = db.ready()
    with eng.connect() as c:
        v = c.execute(select(func.max(tables.cache_diagnoses.c.built_at))).scalar()
    return v.isoformat(timespec="seconds") if v else None


def window():
    eng = db.ready()
    with eng.connect() as c:
        v = c.execute(select(func.max(callsites.callsites.c.window_days))).scalar()
    return v or 30


def _site_block(site, usecase, ledger_rows, diag_rows):
    """One site as the pages expect: models with their totals, ttl and diagnosis; days; total; story; profile."""
    cs = callsites.get(site) or {}
    prof = profiler.get(site)
    models = {}
    for d in diag_rows:
        rows = [r for r in ledger_rows if (r["model"] or "") == d["model"]]
        models[d["model"]] = {**total(rows), "model": d["model"], "ttl": d["ttl"], "diagnosis": d["diagnosis"]}
    story = diag_rows[0]["story"] if diag_rows else {"now": "", "why": "", "action": "", "worth": 0, "share": 0, "proof": None, "cause": None}
    days = _days_series(ledger_rows)
    return {"site": site, "anchors": cs.get("signature") or [], "unsure": cs.get("unsure", True), "versions": cs.get("versions") or [],
            "layout": {"static_chars": cs.get("static_chars"), "dynamic_chars": cs.get("dynamic_chars"),
                       "static_after_dynamic": (cs.get("static_after") or 0) / cs["static_chars"] if cs.get("static_chars") else 0.0,
                       "tool_sets": cs.get("tool_sets")},
            "models": models, "days": days, "total": total(ledger_rows), "saving": round(sum(d["saving"] or 0 for d in diag_rows), 2),
            "profile": prof, "story": story, "trend": trend(days), "usecase": usecase}


def _group(ledger, diags):
    by_site_l, by_site_d = defaultdict(list), defaultdict(list)
    for r in ledger:
        by_site_l[r["site"]].append(r)
    for d in diags:
        by_site_d[d["site"]].append(d)
    return by_site_l, by_site_d


def usecase(uc):
    key = uc if uc is not None else ""
    ledger = tables.rows(tables.ledger_daily, tables.ledger_daily.c.usecase == key)
    diags = tables.rows(tables.cache_diagnoses, tables.cache_diagnoses.c.usecase == key)
    if not ledger:
        return None
    by_l, by_d = _group(ledger, diags)
    sites = [_site_block(s, uc, by_l[s], by_d.get(s, [])) for s in by_l]
    days = _days_series(ledger)
    return {"built_at": built_at(), "window": window(), "usecase": uc, "total": total(ledger), "days": days, "trend": trend(days),
            "saving": round(sum(s["saving"] for s in sites), 2),
            "sites": sorted(sites, key=lambda s: (-s["saving"], -s["total"]["cost"]))}


def site(site_id):
    ledger = tables.rows(tables.ledger_daily, tables.ledger_daily.c.site == site_id)
    if not ledger:
        return None
    diags = tables.rows(tables.cache_diagnoses, tables.cache_diagnoses.c.site == site_id)
    uc = _U(ledger[0]["usecase"])
    return {"built_at": built_at(), "window": window(), **_site_block(site_id, uc, ledger, diags)}


def _brief(s):
    p = s.get("profile") or {}
    return {"usecase": s["usecase"], "site": s["site"], "name": p.get("name") or (s["anchors"][0][:60] if s["anchors"] else s["site"]),
            "does": p.get("does", ""), "calls": s["total"]["calls"], "share": s["story"]["share"], "cause": s["story"]["cause"],
            "action": s["story"]["action"], "worth": s["saving"], "proof": s["story"]["proof"], "trend": s["trend"]}


def overview():
    """The org level: reuse and its trend, opportunities, drops, and every usecase ranked by what's left."""
    ledger = tables.rows(tables.ledger_daily)
    if not ledger:
        return None
    diags = tables.rows(tables.cache_diagnoses)
    by_uc = defaultdict(list)
    for r in ledger:
        by_uc[r["usecase"]].append(r)
    by_l, by_d = _group(ledger, diags)
    sites = {s: _site_block(s, _U(by_l[s][0]["usecase"]), by_l[s], by_d.get(s, [])) for s in by_l}
    usecases = []
    for uc, rows in by_uc.items():
        uc_sites = [s for s in sites.values() if (s["usecase"] or "") == uc]
        days = _days_series(rows)
        usecases.append({"usecase": _U(uc), "total": total(rows), "saving": round(sum(s["saving"] for s in uc_sites), 2),
                         "trend": trend(days), "sites": len(uc_sites), "unsure": any(s["unsure"] for s in uc_sites),
                         "causes": sorted({s["story"]["cause"] for s in uc_sites if s["story"]["cause"]})})
    ranked = sorted(sites.values(), key=lambda s: -s["saving"])
    days = _days_series(ledger)
    return {"built_at": built_at(), "window": window(), "daily": days, "total": total(ledger), "trend": trend(days),
            "saving": round(sum(u["saving"] for u in usecases), 2),
            "opportunities": [_brief(s) for s in ranked if s["saving"] > 0][:10],
            "dropped": sorted([_brief(s) for s in sites.values() if s["trend"] and s["trend"]["delta"] <= -0.05], key=lambda b: b["trend"]["delta"]),
            "usecases": sorted(usecases, key=lambda u: (-u["saving"], -u["total"]["cost"]))}


def decision(site_id):
    """The cache decision of one site, for the call-site pages."""
    diags = tables.rows(tables.cache_diagnoses, tables.cache_diagnoses.c.site == site_id)
    if not diags:
        return None
    st = diags[0]["story"]
    return {"cause": st["cause"], "action": st["action"], "worth": round(sum(d["saving"] or 0 for d in diags), 2), "proof": st["proof"],
            "share": st["share"], "models": sorted(d["model"] for d in diags), "built_at": diags[0]["built_at"]}
