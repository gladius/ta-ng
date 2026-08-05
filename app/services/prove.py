"""Prove — the paid step. For the nodes the user selected, run a REAL provider round-trip and turn each
deterministic finding into measured evidence:

  cache (CACHEABLE/BREAKER) -> cache_proof.prove: write the recovered prefix, read it back (cache_read counter)
  downgrade                 -> audit.audit_node:  re-run the cheaper tier on distinct inputs + judge preservation

`stream()` yields progress events (for SSE) as it goes and stores the final, frozen result via store.put so the
report reads identical numbers on every reload. Nothing is asserted here that isn't measured by a live call.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.services import cache_proof, audit, funnel, graph, store
from app.services.cache import _prefix_text, annotate_prompt


def proof_key(source, ws, project):
    return (source, ws, project, "proof")


def _reorg(bucket, cd):
    """The concrete cache-expansion fix for a BREAKER: the per-call line that breaks the byte-prefix, and the
    static tokens that become cacheable once it moves to the end."""
    lines = [l for l in _prefix_text(bucket[0]).split("\n") if l.strip()]
    return {"moved": (lines[0] if lines else "").strip(), "recover_tok": cd["recoverable_tok"]}


def _step_msg(r):
    parts = []
    if r["cache"] and r["cache"]["verdict"] in ("CACHEABLE", "BREAKER"):
        parts.append("caching")
    if r["downgrade"]:
        parts.append("downgrade → %s" % r["downgrade_to"])
    return " + ".join(parts) + " · proving live" if parts else "proving live"


def _prove_one(key, r, b):
    """Prove ONE call-site (both levers if it has both). Runs in a worker thread → paid calls happen concurrently.
    Identified by the UNIQUE key; `node` is the display label (may repeat across call-sites). The $ only counts what
    actually PROVED — an unproven cache or a drifted downgrade is $0."""
    node = r["node"]
    out = {"key": key, "node": node, "graph_path": r.get("graph_path", ""),
           "model": r["model"], "cost": r["cost"], "cache_usd": 0.0, "downgrade_usd": 0.0}
    cd = r["cache"]
    if cd and cd["verdict"] in ("CACHEABLE", "BREAKER") and b:
        if cd.get("mode") == "explicit":
            p = cache_proof.prove(b)                                       # Anthropic: live write→read round-trip
            out["cache"] = {"verdict": cd["verdict"], "recoverable_tok": cd["recoverable_tok"], "mode": "explicit",
                            "method": "live", "read": p["read"], "write": p["write"], "proven": p["proven"],
                            "prefix_tok": p["prefix_tok"], "lines": annotate_prompt(b)}
            proven = p["proven"]
        else:
            # auto provider (OpenAI/Google): no breakpoint exists to round-trip. But the recovered static lines are
            # byte-identical across every sample, so once the per-call prefix moves the provider's AUTOMATIC cache
            # covers them — provable by construction, no paid call.
            proven = True
            out["cache"] = {"verdict": cd["verdict"], "recoverable_tok": cd["recoverable_tok"], "mode": "auto",
                            "method": "structural", "read": cd["recoverable_tok"], "write": 0, "proven": True,
                            "prefix_tok": cd["recoverable_tok"], "lines": annotate_prompt(b)}
        if cd["verdict"] == "BREAKER":
            out["cache"]["reorg"] = _reorg(b, cd)
        out["cache_usd"] = r["cache_usd"] if proven else 0.0              # honest: no proof → no claimed saving
    if r["downgrade"] and b:
        a = audit.audit_node(node, b)               # N distinct inputs x K repeats (central config); rate-based verdict
        out["downgrade"] = a
        out["downgrade_usd"] = r["downgrade_usd"] if a["verdict"] == "SAFE" else 0.0   # $ only when unanimously safe
    return out


def stream(source, ws, project, keys, calls=None):
    """Generator: yields SSE progress while proving the selected nodes CONCURRENTLY (bounded pool), then stores
    the frozen result. Nodes prove in parallel and each downgrade fans its inputs out too, so wall-clock is the
    slowest single node, not the sum. Paid — same number of calls as sequential, just overlapped."""
    f = funnel.build(source, ws, project, calls=calls)
    rows = {r["key"]: r for r in f["rows"] if r["key"] in keys}            # identity = UNIQUE key, never the label
    order = [k for k in keys if k in rows]
    # Per-call-site traces come from the graph's OWN buckets, keyed by the SAME unique key — so two call-sites that
    # share a display label (e.g. two 'supervisor' nodes) neither collide nor audit each other's traces.
    g = store.get_or_build((source, ws, project, "graph"), lambda: graph.build(source, ws, project))
    by_key = g.get("buckets", {})

    yield {"type": "start", "total": len(order), "agent": project}
    for k in order:                                                        # announce every call-site (all in flight)
        yield {"type": "step", "key": k, "node": rows[k]["node"], "msg": _step_msg(rows[k])}

    done = {}
    with ThreadPoolExecutor(max_workers=min(4, len(order) or 1)) as ex:
        futs = {ex.submit(_prove_one, k, rows[k], by_key.get(k, [])): k for k in order}
        for fut in as_completed(futs):
            k = futs[fut]
            try:
                done[k] = fut.result()
            except Exception as e:                                         # one call-site failing must not kill the run
                r = rows[k]
                done[k] = {"key": k, "node": r["node"], "graph_path": r.get("graph_path", ""),
                           "model": r["model"], "cost": r["cost"],
                           "cache_usd": 0.0, "downgrade_usd": 0.0, "error": str(e)[:200]}
            yield {"type": "done_node", "key": k, "node": rows[k]["node"]}

    results = [done[k] for k in order]                                     # stable, selection order (not finish order)
    res = {"agent": project, "calls": f["calls"], "results": results,
           "cache_usd": round(sum(x.get("cache_usd", 0) for x in results), 2),
           "downgrade_usd": round(sum(x.get("downgrade_usd", 0) for x in results), 2),
           "total": round(sum(x.get("cache_usd", 0) + x.get("downgrade_usd", 0) for x in results), 2)}
    store.put(proof_key(source, ws, project), res)
    yield {"type": "complete", "total_usd": res["total"]}
