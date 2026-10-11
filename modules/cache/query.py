"""What the cache pages show, read from the call-site records (their series and decisions). Same JSON shapes as
before: the org and usecase levels are sums over records."""
from collections import defaultdict

from core.identity import store
from modules.cache.report import SUM, total


def _series_total(series):
    """{model: {day: {...}}} -> per-day totals across models, and the window total."""
    days = defaultdict(lambda: {k: 0 for k in SUM})
    for by_day in series.values():
        for day, d in by_day.items():
            for k in SUM:
                days[day][k] += d.get(k) or 0
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


def _sum_days(blocks):
    days = defaultdict(lambda: {k: 0 for k in SUM})
    for b in blocks:
        for day, d in b["days"].items():
            for k in SUM:
                days[day][k] += d[k]
    return dict(days)


_EMPTY_STORY = {"now": "", "why": "", "action": "", "worth": 0, "share": 0, "proof": None, "cause": None}


def _site_block(rec):
    cache = rec["decisions"].get("cache") or {}
    days = _series_total(rec["series"])
    static = rec.get("static_chars") or 0
    return {"site": rec["site"], "usecase": rec["usecase"], "anchors": rec["signature"], "unsure": rec["unsure"],
            "versions": rec["versions"],
            "layout": {"static_chars": static, "dynamic_chars": rec.get("dynamic_chars"),
                       "static_after_dynamic": (rec.get("static_after") or 0) / static if static else 0.0, "tool_sets": rec.get("tool_sets")},
            "models": cache.get("models", {}), "days": days, "total": total(list(days.values())), "saving": cache.get("saving", 0),
            "profile": rec["profile"], "story": cache.get("story") or _EMPTY_STORY, "trend": trend(days),
            "built_at": cache.get("built_at"), "window": rec["window_days"]}


def usecase(uc):
    recs = store.by_usecase(uc)
    if not recs:
        return None
    sites = [_site_block(r) for r in recs]
    days = _sum_days(sites)
    return {"built_at": max((s["built_at"] or "" for s in sites), default=None) or None, "window": sites[0]["window"],
            "usecase": uc, "total": total(list(days.values())), "days": days, "trend": trend(days),
            "saving": round(sum(s["saving"] for s in sites), 2),
            "sites": sorted(sites, key=lambda s: (-s["saving"], -s["total"]["cost"]))}


def site(site_id):
    rec = store.get(site_id)
    return _site_block(rec) if rec else None


def _brief(s):
    p = s.get("profile") or {}
    return {"usecase": s["usecase"], "site": s["site"], "name": p.get("name") or (s["anchors"][0][:60] if s["anchors"] else s["site"]),
            "does": p.get("does", ""), "calls": s["total"]["calls"], "share": s["story"]["share"], "cause": s["story"]["cause"],
            "action": s["story"]["action"], "worth": s["saving"], "proof": s["story"]["proof"], "trend": s["trend"]}


def overview():
    """The org level over every analysed usecase: reuse and its trend, opportunities, drops, usecases by what's left."""
    recs = store.all_sites()
    if not recs:
        return None
    sites = [_site_block(r) for r in recs]
    by_uc = defaultdict(list)
    for s in sites:
        by_uc[s["usecase"]].append(s)
    usecases = []
    for uc, uc_sites in by_uc.items():
        days = _sum_days(uc_sites)
        usecases.append({"usecase": uc, "total": total(list(days.values())), "saving": round(sum(s["saving"] for s in uc_sites), 2),
                         "trend": trend(days), "sites": len(uc_sites), "unsure": any(s["unsure"] for s in uc_sites),
                         "causes": sorted({s["story"]["cause"] for s in uc_sites if s["story"]["cause"]})})
    days = _sum_days(sites)
    return {"built_at": max((s["built_at"] or "" for s in sites), default=None) or None, "window": sites[0]["window"], "daily": days,
            "total": total(list(days.values())), "trend": trend(days), "saving": round(sum(u["saving"] for u in usecases), 2),
            "opportunities": [_brief(s) for s in sorted(sites, key=lambda s: -s["saving"]) if s["saving"] > 0][:10],
            "dropped": sorted([_brief(s) for s in sites if s["trend"] and s["trend"]["delta"] <= -0.05], key=lambda b: b["trend"]["delta"]),
            "usecases": sorted(usecases, key=lambda u: (-u["saving"], -u["total"]["cost"]))}


def decision(site_id):
    rec = store.get(site_id)
    cache = (rec or {}).get("decisions", {}).get("cache")
    if not cache:
        return None
    st = cache["story"]
    return {"cause": st["cause"], "action": st["action"], "worth": cache["saving"], "proof": st["proof"], "share": st["share"],
            "models": sorted(cache["models"]), "built_at": cache.get("built_at")}
