"""The cache report: the ledger, gaps and sites joined per usecase and call-site, with each site's diagnosis and TTL
what-if. Built by core.ledger.refresh and stored; the API serves it as it is."""
import json
from collections import defaultdict

from core.ledger import bq, daily, gaps
from modules.cache import diagnosis, ttl

_SUM = ("calls", "prompt_tokens", "cached_tokens", "written_tokens", "completion_tokens", "calls_cached", "cost",
        "cost_nocache", "calls_with_tools")


def _sum(rows):
    out = {k: 0 for k in _SUM}
    for r in rows:
        for k in _SUM:
            out[k] += r.get(k) or 0
    out["unsure"] = any(r.get("unsure") for r in rows)
    return out


def build(days=14):
    sites = {r["site"]: dict(r) for r in bq.query("SELECT * FROM %s" % bq.work("sites"))}
    for s in sites.values():
        s["versions"] = json.loads(s.get("versions") or "[]")
    led = daily.fetch(days)
    gap = gaps.fetch(days)
    by_site_model = defaultdict(list)
    by_gap = defaultdict(list)
    for r in led:
        by_site_model[(r["usecase"], r["site"], r["model"])].append(r)
    for g in gap:
        by_gap[(g["usecase"], g["site"], g["model"])].append(g)
    usecases = defaultdict(lambda: {"sites": {}, "days": defaultdict(lambda: {k: 0 for k in _SUM})})
    for (uc, site, model), rows in by_site_model.items():
        u = usecases[uc]
        for r in rows:
            d = u["days"][str(r["day"])]
            for k in _SUM:
                d[k] += r.get(k) or 0
        tot = _sum(rows)
        tot["model"] = model
        t = ttl.what_if(by_gap.get((uc, site, model), []), model)
        s = sites.get(site)
        diag = diagnosis.diagnose(s, tot, t) if s else None
        entry = u["sites"].setdefault(site, {"site": site, "anchors": (s or {}).get("anchors", []),
                                             "unsure": (s or {}).get("unsure", True), "versions": (s or {}).get("versions", []),
                                             "layout": {k: (s or {}).get(k) for k in ("static_chars", "dynamic_chars",
                                                                                      "static_after_dynamic", "tool_sets")},
                                             "models": {}, "days": defaultdict(lambda: {k: 0 for k in _SUM})})
        entry["models"][model] = {**tot, "ttl": t, "diagnosis": diag}
        for r in rows:
            d = entry["days"][str(r["day"])]
            for k in _SUM:
                d[k] += r.get(k) or 0
    out = {"days": days, "usecases": {}}
    for uc, u in usecases.items():
        for s in u["sites"].values():
            s["days"] = dict(s["days"])
            s["total"] = _sum(list(s["models"].values()))
            s["saving"] = round(sum((m["diagnosis"] or {}).get("saving", 0) for m in s["models"].values()), 2)
        tot = _sum([s["total"] for s in u["sites"].values()])
        out["usecases"][uc] = {"usecase": uc, "total": tot, "days": dict(u["days"]),
                               "saving": round(sum(s["saving"] for s in u["sites"].values()), 2),
                               "sites": sorted(u["sites"].values(), key=lambda s: -s["total"]["cost"])}
    out["total"] = _sum([u["total"] for u in out["usecases"].values()])
    out["saving"] = round(sum(u["saving"] for u in out["usecases"].values()), 2)
    return out
