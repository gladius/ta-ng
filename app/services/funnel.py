"""Funnel — merge the two deterministic detectors into one ranked, priced view of an agent's call-sites.

Detection is $0: for every call-site we already know the cache verdict (cache.detect) and whether a cheaper tier
applies (downgrade.candidates). This joins them by node, prices both on ONE fixed calls basis so the numbers
reconcile, and ranks costliest-first — the shortlist the user proves. Nothing here is paid; proof is prove.py.
"""
from app.catalog import PRICE, canonical_model, approx_tokens
from app.services import cache, downgrade
from app.config import CALLS_BASIS, lever_on


def _price(model):
    return PRICE.get(canonical_model(model) or model, {})


def _compressible(bucket):
    """$0 candidacy for harness (system-prompt) compression: the system prompt is STATIC across the call-site AND big
    enough to be worth shrinking. A DYNAMIC system prompt is a cache-reorg case, not a compress case — so we skip it
    here (the proof would only waste calls). Real $ is decided by the paid proof (compress.prove); this just flags it."""
    if not bucket:
        return False
    systems = {"\n".join(m.get("content") or "" for m in t.get("input_messages", []) if m.get("role") == "system")
               for t in bucket}
    return len(systems) == 1 and approx_tokens(next(iter(systems))) >= 400


def build(source_id, ws_id, project, calls=None, levers=None, g=None, dgmode="commercial"):
    """Ranked opportunities for an agent. Deterministic, no LLM. Shape is what the report/select pages render.
    `levers` overrides which levers are active for THIS view (e.g. the report's cache checkbox); default = config.
    `dgmode` selects the downgrade TARGET UNIVERSE — 'commercial' (default: migration path, else one tier down) or
    'open-weight' (the opt-in same-tier open-weight swap). It flows straight to downgrade.candidates and MUST match
    between the select render and the proof, so the chip shown is the model actually re-run.
    `g` = a PINNED snapshot graph (the report holds one, from app.services.snapshot). When given, the funnel derives
    EVERYTHING from it — call-sites, per-call-site buckets, cache detection — and never re-fetches, so identity can't
    drift between rendering and proving. Only non-report callers (cli) let it build a fresh graph."""
    from app.services import graph
    calls = calls or CALLS_BASIS
    active = set(levers) if levers is not None else set(x for x in ("downgrade", "cache", "compress") if lever_on(x))

    if g is None:
        g = graph.build(source_id, ws_id, project)
    buckets = g.get("buckets", {})                           # per-call-site traces, keyed by the UNIQUE node key

    cache_by = {}
    if "cache" in active:                                    # cache detection off the SNAPSHOT buckets, keyed by key
        for n in g["nodes"]:
            d = cache.detect(buckets.get(n["key"], []), model=n["model"])
            if d:
                cache_by[n["key"]] = d
    dg_by = {c["key"]: c for c in downgrade.candidates(g["nodes"], per_calls=calls, mode=dgmode)} if "downgrade" in active else {}

    rows = []
    for n in g["nodes"]:
        node, p = n["node"], _price(n["model"])              # node = display label (may repeat across call-sites)
        cost = round((n["avg_in"] * p.get("input", 0) + n["avg_out"] * p.get("output", 0)) / 1e6 * calls, 2)
        cd = cache_by.get(n["key"])                          # keyed by the UNIQUE key — no same-label collision
        # only a BREAKER is OUR win (reorg). CACHEABLE/AUTO is handled by the gateway/provider -> not a claimed saving.
        cache_usd = round(cd["save_per_1k"] * calls / 1000, 2) if (cd and cd.get("verdict") == "BREAKER") else 0.0
        dg = dg_by.get(n["key"])                             # join by the UNIQUE key, never the label
        dg_usd = round(dg["usd"], 2) if dg else 0.0                       # already priced at `calls`
        # compress $ can't be estimated $0 (we don't know the safe ratio until we compress) — flag candidacy only;
        # the proof fills compress_usd. So compress is a SELECTABLE opportunity even when downgrade/cache find nothing.
        compress_cand = ("compress" in active) and _compressible(buckets.get(n["key"], []))
        rows.append({
            "key": n["key"], "node": node, "model": n["model"], "calls": n["calls"], "cost": cost,
            "graph_path": n.get("graph_path", ""),                 # subgraph nesting — drives the graph filter
            "cache": cd, "cache_usd": cache_usd,
            "cache_verdict": cd["verdict"] if cd else "NONE",
            "downgrade": dg, "downgrade_usd": dg_usd, "downgrade_to": dg["cheaper"] if dg else None,
            "compress_candidate": compress_cand, "compress_usd": 0.0,      # $ decided by the paid proof
            "opportunity": round(cache_usd + dg_usd, 2),                   # deterministic $ (cache+downgrade) drives rank
            "testable": bool(cache_usd or dg_usd or compress_cand),        # selectable if ANY lever has something to try
        })
    rows.sort(key=lambda r: (-r["opportunity"], -r["cost"]))
    return {
        "agent": project, "calls": calls, "rows": rows, "traces": g.get("traces", 0),   # sample size (# traces)
        "dgmode": dgmode,                                         # which target universe these downgrade picks used
        "revision": (g.get("revisions") or [None])[0],            # the ONE version audited (fetch hard-scopes to latest)
        "graphs": g.get("graphs", []),                             # distinct subgraphs, for the filter control
        "total": round(sum(r["opportunity"] for r in rows), 2),
        "cache_total": round(sum(r["cache_usd"] for r in rows), 2),
        "downgrade_total": round(sum(r["downgrade_usd"] for r in rows), 2),
        "wins": sum(1 for r in rows if r["opportunity"] > 0),
    }
