"""Funnel — merge the two deterministic detectors into one ranked, priced view of an agent's call-sites.

Detection is $0: for every call-site we already know the cache verdict (cache.detect) and whether a cheaper tier
applies (downgrade.candidates). This joins them by node, prices both on ONE assumed monthly volume so the numbers
reconcile, and ranks costliest-first — the shortlist the user proves. Nothing here is paid; proof is prove.py.
"""
from auditor.util import PRICE, canonical_model
from app.services import cache, downgrade
from app.config import DISPLAY_CALLS, lever_on


def _price(model):
    return PRICE.get(canonical_model(model) or model, {})


def build(source_id, ws_id, project, calls=None):
    """Ranked opportunities for an agent. Deterministic, $0. Shape is what the report/select pages render."""
    from connectors.datasource import get_source
    from app.services import graph, store
    calls = calls or DISPLAY_CALLS

    g = store.get_or_build((source_id, ws_id, project, "graph"),
                           lambda: graph.build(source_id, ws_id, project))
    buckets, _ = get_source(source_id).pull(ws_id, project, limit=300)

    cache_by = {}
    if lever_on("cache"):                               # levers come from config.LEVERS — one source of truth
        for k, b in buckets.items():
            d = cache.detect(b, model=b[0].get("model"))
            if d:
                cache_by[k.split("/")[-1]] = d
    dg_by = {c["node"]: c for c in downgrade.candidates(g["nodes"], per_calls=calls)} if lever_on("downgrade") else {}

    rows = []
    for n in g["nodes"]:
        node, p = n["node"], _price(n["model"])
        cost = round((n["avg_in"] * p.get("input", 0) + n["avg_out"] * p.get("output", 0)) / 1e6 * calls, 2)
        cd = cache_by.get(node)
        cache_usd = round(cd["save_per_1k"] * calls / 1000, 2) if (cd and cd["cacheable"]) else 0.0
        dg = dg_by.get(node)
        dg_usd = round(dg["usd"], 2) if dg else 0.0                       # already priced at `calls`
        rows.append({
            "key": node, "node": node, "model": n["model"], "calls": n["calls"], "cost": cost,
            "graph_path": n.get("graph_path", ""),                 # subgraph nesting — drives the graph filter
            "cache": cd, "cache_usd": cache_usd,
            "cache_verdict": cd["verdict"] if cd else "NONE",
            "downgrade": dg, "downgrade_usd": dg_usd, "downgrade_to": dg["cheaper"] if dg else None,
            "opportunity": round(cache_usd + dg_usd, 2),
        })
    rows.sort(key=lambda r: (-r["opportunity"], -r["cost"]))
    return {
        "agent": project, "calls": calls, "rows": rows,
        "graphs": g.get("graphs", []),                             # distinct subgraphs, for the filter control
        "total": round(sum(r["opportunity"] for r in rows), 2),
        "cache_total": round(sum(r["cache_usd"] for r in rows), 2),
        "downgrade_total": round(sum(r["downgrade_usd"] for r in rows), 2),
        "wins": sum(1 for r in rows if r["opportunity"] > 0),
    }
