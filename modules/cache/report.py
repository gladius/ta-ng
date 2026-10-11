"""The cache diagnoses: for every call-site and model of the window, the cause, the lifetime what-if and the story,
from the ledger and gap rows the refresh just fetched. Stored in cache_diagnoses; modules/cache/query.py serves them."""
from collections import defaultdict

from core import profiler
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
    return {"site": str(s["site"]), "usecase": s.get("usecase"), "static_chars": static,
            "dynamic_chars": int(s.get("dynamic_chars") or 0),
            "static_after_dynamic": (int(s.get("static_after") or 0) / static) if static else 0.0,
            "tool_sets": int(s.get("tool_sets") or 0),
            "versions": [{"version": str(v["version"]), "first": str(v["first"]), "last": str(v["last"])} for v in s.get("versions") or []],
            "profile": profiler.get(str(s["site"]))}


def diagnoses(sites, ledger, gaps, days):
    """-> rows for cache_diagnoses: one per site and model, each carrying the site's story."""
    by_sm, by_gap = defaultdict(list), defaultdict(list)
    for r in ledger:
        by_sm[(str(r["site"]), r["model"] or "")].append(r)
    for g in gaps:
        by_gap[(str(g["site"]), g["model"] or "")].append(g)
    out = []
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
        st = story.tell(entry, days)
        for model, m in models.items():
            out.append({"site": site, "model": model, "usecase": s.get("usecase"), "cause": m["diagnosis"]["cause"],
                        "saving": m["diagnosis"]["saving"], "proof": m["diagnosis"]["proof"],
                        "diagnosis": m["diagnosis"], "ttl": m["ttl"], "story": st})
    return out
