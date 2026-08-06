"""Funnel — merge the two deterministic detectors into one ranked, priced view of an agent's call-sites.

Detection is $0: for every call-site we already know the cache verdict (cache.detect) and whether a cheaper tier
applies (downgrade.candidates). This joins them by node, prices both on ONE fixed calls basis so the numbers
reconcile, and ranks costliest-first — the shortlist the user proves. Nothing here is paid; proof is prove.py.
"""
from auditor.util import PRICE, canonical_model
from app.services import cache, downgrade
from app.config import CALLS_BASIS, lever_on


def _price(model):
    return PRICE.get(canonical_model(model) or model, {})


def build(source_id, ws_id, project, calls=None, levers=None, g=None):
    """Ranked opportunities for an agent. Deterministic, no LLM. Shape is what the report/select pages render.
    `levers` overrides which levers are active for THIS view (e.g. the report's cache checkbox); default = config.
    `g` = a PINNED snapshot graph (the report holds one, from app.services.snapshot). When given, the funnel derives
    EVERYTHING from it — call-sites, per-call-site buckets, cache detection — and never re-fetches, so identity can't
    drift between rendering and proving. Only non-report callers (cli) let it build a fresh graph."""
    from app.services import graph
    calls = calls or CALLS_BASIS
    active = set(levers) if levers is not None else set(x for x in ("downgrade", "cache") if lever_on(x))

    if g is None:
        g = graph.build(source_id, ws_id, project)
    buckets = g.get("buckets", {})                           # per-call-site traces, keyed by the UNIQUE node key

    cache_by = {}
    if "cache" in active:                                    # cache detection off the SNAPSHOT buckets, keyed by key
        for n in g["nodes"]:
            d = cache.detect(buckets.get(n["key"], []), model=n["model"])
            if d:
                cache_by[n["key"]] = d
    dg_by = {c["key"]: c for c in downgrade.candidates(g["nodes"], per_calls=calls)} if "downgrade" in active else {}

    rows = []
    for n in g["nodes"]:
        node, p = n["node"], _price(n["model"])              # node = display label (may repeat across call-sites)
        cost = round((n["avg_in"] * p.get("input", 0) + n["avg_out"] * p.get("output", 0)) / 1e6 * calls, 2)
        cd = cache_by.get(n["key"])                          # keyed by the UNIQUE key — no same-label collision
        # only a BREAKER is OUR win (reorg). CACHEABLE/AUTO is handled by the gateway/provider -> not a claimed saving.
        cache_usd = round(cd["save_per_1k"] * calls / 1000, 2) if (cd and cd.get("verdict") == "BREAKER") else 0.0
        dg = dg_by.get(n["key"])                             # join by the UNIQUE key, never the label
        dg_usd = round(dg["usd"], 2) if dg else 0.0                       # already priced at `calls`
        rows.append({
            "key": n["key"], "node": node, "model": n["model"], "calls": n["calls"], "cost": cost,
            "graph_path": n.get("graph_path", ""),                 # subgraph nesting — drives the graph filter
            "cache": cd, "cache_usd": cache_usd,
            "cache_verdict": cd["verdict"] if cd else "NONE",
            "downgrade": dg, "downgrade_usd": dg_usd, "downgrade_to": dg["cheaper"] if dg else None,
            "opportunity": round(cache_usd + dg_usd, 2),
        })
    rows.sort(key=lambda r: (-r["opportunity"], -r["cost"]))
    return {
        "agent": project, "calls": calls, "rows": rows, "traces": g.get("traces", 0),   # sample size (# traces)
        "graphs": g.get("graphs", []),                             # distinct subgraphs, for the filter control
        "total": round(sum(r["opportunity"] for r in rows), 2),
        "cache_total": round(sum(r["cache_usd"] for r in rows), 2),
        "downgrade_total": round(sum(r["downgrade_usd"] for r in rows), 2),
        "wins": sum(1 for r in rows if r["opportunity"] > 0),
    }
