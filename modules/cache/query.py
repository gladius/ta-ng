"""What the cache pages show, read from the call-site records and the daily ledger. Same JSON shapes as before."""
from collections import defaultdict

from sqlalchemy import func, select

from app.services import db
from core.identity import store
from core.ledger import tables
from modules.cache.report import SUM, total


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


def window():
    eng = db.ready()
    with eng.connect() as c:
        v = c.execute(select(func.max(store.callsites.c.window_days))).scalar()
    return v or 30


_EMPTY_STORY = {"now": "", "why": "", "action": "", "worth": 0, "share": 0, "proof": None, "cause": None}


def _site_block(rec, ledger_rows):
    """One call-site record + its ledger rows as the pages expect them."""
    cache = rec["decisions"].get("cache") or {}
    days = _days_series(ledger_rows)
    static = rec.get("static_chars") or 0
    return {"site": rec["site"], "usecase": rec["usecase"], "anchors": rec["signature"], "unsure": rec["unsure"],
            "versions": rec["versions"],
            "layout": {"static_chars": static, "dynamic_chars": rec.get("dynamic_chars"),
                       "static_after_dynamic": (rec.get("static_after") or 0) / static if static else 0.0, "tool_sets": rec.get("tool_sets")},
            "models": cache.get("models", {}), "days": days, "total": total(ledger_rows), "saving": cache.get("saving", 0),
            "profile": rec["profile"], "story": cache.get("story") or _EMPTY_STORY, "trend": trend(days),
            "built_at": cache.get("built_at")}


def _blocks(recs, ledger):
    by_site = defaultdict(list)
    for r in ledger:
        by_site[r["site"]].append(r)
    return [_site_block(rec, by_site.get(rec["site"], [])) for rec in recs]


def usecase(uc):
    recs = store.by_usecase(uc)
    if not recs:
        return None
    ledger = tables.ledger(tables.ledger_daily.c.usecase == (uc or ""))
    sites = _blocks(recs, ledger)
    days = _days_series(ledger)
    return {"built_at": max((s["built_at"] or "" for s in sites), default=None) or None, "window": window(), "usecase": uc,
            "total": total(ledger), "days": days, "trend": trend(days), "saving": round(sum(s["saving"] for s in sites), 2),
            "sites": sorted(sites, key=lambda s: (-s["saving"], -s["total"]["cost"]))}


def site(site_id):
    rec = store.get(site_id)
    if not rec:
        return None
    ledger = tables.ledger(tables.ledger_daily.c.site == site_id)
    return {"window": window(), **_site_block(rec, ledger)}


def _brief(s):
    p = s.get("profile") or {}
    return {"usecase": s["usecase"], "site": s["site"], "name": p.get("name") or (s["anchors"][0][:60] if s["anchors"] else s["site"]),
            "does": p.get("does", ""), "calls": s["total"]["calls"], "share": s["story"]["share"], "cause": s["story"]["cause"],
            "action": s["story"]["action"], "worth": s["saving"], "proof": s["story"]["proof"], "trend": s["trend"]}


def overview():
    """The org level: reuse and its trend, opportunities, drops, and every usecase ranked by what's left."""
    recs = store.all_sites()
    ledger = tables.ledger()
    if not recs or not ledger:
        return None
    sites = _blocks(recs, ledger)
    by_uc_l, by_uc_s = defaultdict(list), defaultdict(list)
    for r in ledger:
        by_uc_l[r["usecase"] or ""].append(r)
    for s in sites:
        by_uc_s[s["usecase"] or ""].append(s)
    usecases = []
    for uc, rows in by_uc_l.items():
        uc_sites = by_uc_s.get(uc, [])
        days = _days_series(rows)
        usecases.append({"usecase": uc or None, "total": total(rows), "saving": round(sum(s["saving"] for s in uc_sites), 2),
                         "trend": trend(days), "sites": len(uc_sites), "unsure": any(s["unsure"] for s in uc_sites),
                         "causes": sorted({s["story"]["cause"] for s in uc_sites if s["story"]["cause"]})})
    days = _days_series(ledger)
    return {"built_at": max((s["built_at"] or "" for s in sites), default=None) or None, "window": window(), "daily": days,
            "total": total(ledger), "trend": trend(days), "saving": round(sum(u["saving"] for u in usecases), 2),
            "opportunities": [_brief(s) for s in sorted(sites, key=lambda s: -s["saving"]) if s["saving"] > 0][:10],
            "dropped": sorted([_brief(s) for s in sites if s["trend"] and s["trend"]["delta"] <= -0.05], key=lambda b: b["trend"]["delta"]),
            "usecases": sorted(usecases, key=lambda u: (-u["saving"], -u["total"]["cost"]))}


def decision(site_id):
    """The cache decision of one site, for the call-site pages."""
    rec = store.get(site_id)
    cache = (rec or {}).get("decisions", {}).get("cache")
    if not cache:
        return None
    st = cache["story"]
    return {"cause": st["cause"], "action": st["action"], "worth": cache["saving"], "proof": st["proof"], "share": st["share"],
            "models": sorted(cache["models"]), "built_at": cache.get("built_at")}
