"""The cache decision per call-site: for each model it is sent to, the window totals, the lifetime what-if and the
diagnosis; plus the story (now / why / what to do) and the saving. Stored on the call-site record as
its cache findings; modules/cache/query.py serves it."""
from collections import defaultdict

from core.identity import store
from modules.cache import diagnosis, story, ttl

SUM = ("calls", "prompt_tokens", "cached_tokens", "written_tokens", "completion_tokens", "calls_cached", "cost",
       "cost_nocache", "calls_with_tools")


def total(rows):
    out = {k: 0 for k in SUM}
    for r in rows:
        for k in SUM:
            out[k] += r.get(k) or 0
    return out


def site_entry(s):
    """A sites row (from the build) as the diagnosis and story code expect it."""
    static = int(s.get("static_chars") or 0)
    rec = store.get(str(s["site"])) or {}
    return {"site": str(s["site"]), "usecase": s.get("usecase"), "static_chars": static,
            "dynamic_chars": int(s.get("dynamic_chars") or 0),
            "static_after_dynamic": (int(s.get("static_after") or 0) / static) if static else 0.0,
            "tool_sets": int(s.get("tool_sets") or 0),
            "versions": [{"version": str(v["version"]), "first": str(v["first"]), "last": str(v["last"])} for v in s.get("versions") or []],
            "profile": rec.get("profile")}


def cache_decisions(sites, ledger, gaps, days):
    """-> {site: {"models": {model: {totals, ttl, diagnosis}}, "story": {...}, "saving": $}}"""
    by_sm, by_gap = defaultdict(list), defaultdict(list)
    for r in ledger:
        by_sm[(str(r["site"]), r["model"] or "")].append(r)
    for g in gaps:
        by_gap[(str(g["site"]), g["model"] or "")].append(g)
    out = {}
    for s in sites:
        site = str(s["site"])
        entry = site_entry(s)
        models = {}
        for (st, model), rows in by_sm.items():
            if st != site:
                continue
            tot = total(rows)
            tot["model"] = model
            t = ttl.what_if(by_gap.get((site, model), []), model)
            models[model] = {**tot, "ttl": t, "diagnosis": diagnosis.diagnose(entry, tot, t)}
        if not models:
            continue
        entry["models"] = models
        entry["total"] = total(list(models.values()))
        entry["saving"] = round(sum((m["diagnosis"] or {}).get("saving", 0) for m in models.values()), 2)
        out[site] = {"models": models, "story": story.tell(entry, days), "saving": entry["saving"]}
    return out
