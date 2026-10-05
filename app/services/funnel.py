"""Funnel — the ranked, priced view of an agent's call-sites.

Detection is $0: for every call-site we already know whether a cheaper model applies (downgrade.candidates). This
prices it on ONE fixed calls basis so the numbers reconcile, and ranks costliest-first — the shortlist the user
proves. Nothing here is paid; proof is prove.py.
"""
from app.catalog import PRICE, canonical_model
from app.services import downgrade
from app.config import CALLS_BASIS


def _price(model):
    return PRICE.get(canonical_model(model) or model, {})


def build(source_id, ws_id, project, calls=None, g=None, dgmode="commercial"):
    """Ranked opportunities for an agent. Deterministic, no LLM. Shape is what the report/select pages render.
    `dgmode` selects the downgrade TARGET UNIVERSE — 'commercial' (default: migration path, else one tier down) or
    'open-weight' (the opt-in same-tier open-weight swap). It flows straight to downgrade.candidates and MUST match
    between the select render and the proof, so the chip shown is the model actually re-run.
    `g` = a PINNED snapshot graph (the report holds one, from app.services.snapshot). When given, the funnel derives
    EVERYTHING from it and never re-fetches, so identity can't drift between rendering and proving."""
    from app.services import graph
    calls = calls or CALLS_BASIS

    if g is None:
        g = graph.build(source_id, ws_id, project)

    dg_by = {c["key"]: c for c in downgrade.candidates(g["nodes"], per_calls=calls, mode=dgmode)}

    rows = []
    for n in g["nodes"]:
        node, p = n["node"], _price(n["model"])              # node = display label (may repeat across call-sites)
        cost = round((n["avg_in"] * p.get("input", 0) + n["avg_out"] * p.get("output", 0)) / 1e6 * calls, 2)
        dg = dg_by.get(n["key"])                             # join by the UNIQUE key, never the label
        dg_usd = round(dg["usd"], 2) if dg else 0.0                       # already priced at `calls`
        rows.append({
            "key": n["key"], "node": node, "model": n["model"], "calls": n["calls"], "cost": cost,
            "graph_path": n.get("graph_path", ""),                 # subgraph nesting — drives the graph filter
            "downgrade": dg, "downgrade_usd": dg_usd, "downgrade_to": dg["cheaper"] if dg else None,
            "opportunity": dg_usd,                                         # deterministic $ drives rank
            "testable": bool(dg_usd),
        })
    rows.sort(key=lambda r: (-r["opportunity"], -r["cost"]))
    return {
        "agent": project, "calls": calls, "rows": rows, "traces": g.get("traces", 0),   # sample size (# traces)
        "dgmode": dgmode,                                         # which target universe these downgrade picks used
        "revision": (g.get("revisions") or [None])[0],            # the ONE version audited (fetch hard-scopes to latest)
        "graphs": g.get("graphs", []),                             # distinct subgraphs, for the filter control
        "total": round(sum(r["opportunity"] for r in rows), 2),
        "downgrade_total": round(sum(r["downgrade_usd"] for r in rows), 2),
        "wins": sum(1 for r in rows if r["opportunity"] > 0),
    }
