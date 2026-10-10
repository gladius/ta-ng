"""Funnel: an agent's call-sites priced on one calls basis and ranked by saving. Free; the paid proof is prove.py."""
from app.catalog import PRICE, canonical_model
from app.services import downgrade
from app.config import CALLS_BASIS


def _price(model):
    return PRICE.get(canonical_model(model) or model, {})


def build(source_id, ws_id, project, calls=None, g=None, dgmode="commercial"):
    """Ranked opportunities for an agent (no LLM). `dgmode` must match between the shown list and the proof, so the
    target shown is the one re-run. A pinned snapshot graph `g` is never re-fetched, so render and proof agree."""
    from app.services import graph
    calls = calls or CALLS_BASIS

    if g is None:
        g = graph.build(source_id, ws_id, project)

    dg_by = {c["key"]: c for c in downgrade.candidates(g["nodes"], per_calls=calls, mode=dgmode)}

    rows = []
    for n in g["nodes"]:
        node, p = n["node"], _price(n["model"])
        cost = round((n["avg_in"] * p.get("input", 0) + n["avg_out"] * p.get("output", 0)) / 1e6 * calls, 2)
        dg = dg_by.get(n["key"])                             # by key: labels repeat across call-sites
        dg_usd = round(dg["usd"], 2) if dg else 0.0
        rows.append({
            "key": n["key"], "node": node, "model": n["model"], "calls": n["calls"], "cost": cost,
            "graph_path": n.get("graph_path", ""),
            "downgrade": dg, "downgrade_usd": dg_usd, "downgrade_to": dg["cheaper"] if dg else None,
            "opportunity": dg_usd,
            "testable": bool(dg_usd),
        })
    rows.sort(key=lambda r: (-r["opportunity"], -r["cost"]))
    return {
        "agent": project, "calls": calls, "rows": rows, "traces": g.get("traces", 0),
        "dgmode": dgmode,
        "revision": (g.get("revisions") or [None])[0],            # the one version audited (fetch scopes to latest)
        "graphs": g.get("graphs", []),
        "total": round(sum(r["opportunity"] for r in rows), 2),
        "downgrade_total": round(sum(r["downgrade_usd"] for r in rows), 2),
        "wins": sum(1 for r in rows if r["opportunity"] > 0),
    }
