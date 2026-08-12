"""Prove — the paid step. For the nodes the user selected, run a REAL provider round-trip and turn each
deterministic finding into measured evidence:

  cache (CACHEABLE/BREAKER) -> cache_proof.prove: write the recovered prefix, read it back (cache_read counter)
  downgrade                 -> audit.audit_node:  re-run the cheaper tier on distinct inputs + judge preservation

`stream()` yields progress events (for SSE) as it goes and stores the final, frozen result via store.put so the
report reads identical numbers on every reload. Nothing is asserted here that isn't measured by a live call.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.services import audit, funnel, store, cache_reorg, compress, snapshot, snapdump
from app.config import LEVERS      # enabled levers (default: downgrade only on this branch)


def proof_key(source, ws, project, snap=""):
    return (source, ws, project, "proof", snap)             # proof is tied to the SNAPSHOT it was run against


def _step_msg(r):
    parts = []
    if r["cache"] and r["cache"].get("verdict") == "BREAKER":
        parts.append("cache reorg")
    if r["downgrade"]:
        parts.append("downgrade → %s" % r["downgrade_to"])
    if r.get("compress_candidate"):
        parts.append("compress")
    return " + ".join(parts) + " · proving live" if parts else "proving live"


def _prove_one(key, r, b, calls, levers):
    """Prove ONE call-site (every ENABLED lever it qualifies for). Runs in a worker thread → paid calls happen
    concurrently. Identified by the UNIQUE key; `node` is the display label (may repeat). `calls` is the $-basis.
    The $ only counts what actually PROVED — an unproven cache reorg, a drifted downgrade, or a NOT-SAFE compress is $0.
    Every lever reuses the SAME proof engine (audit.prove_transform); only the transform differs."""
    node = r["node"]
    out = {"key": key, "node": node, "graph_path": r.get("graph_path", ""),
           "model": r["model"], "cost": r["cost"], "cache_usd": 0.0, "downgrade_usd": 0.0, "compress_usd": 0.0}
    cd = r["cache"]
    if "cache" in levers and cd and b and cd.get("verdict") == "BREAKER":
        # the unique win: a prompt whose dynamic content breaks the prefix -> reorg it, then PROVE both behaviour
        # (judge) and caching (before/after round-trip). $ only when SAFE + cache-proven.
        cr = cache_reorg.prove(node, b)
        out["cache"] = cr
        out["cache_usd"] = round((cr.get("save_per_1k") or 0) * calls / 1000, 2) if cr.get("recommend") else 0.0
    elif "cache" in levers and cd and cd.get("verdict") in ("CACHEABLE", "AUTO"):
        # already a contiguous static prefix — a gateway (litellm) or auto-caching provider handles this. Not our win.
        out["cache"] = {"verdict": cd["verdict"], "informational": True,
                        "note": "already cacheable — a gateway or the provider caches this prefix automatically"}
    if "downgrade" in levers and r["downgrade"] and b:
        a = audit.audit_node(node, b, cheaper=r["downgrade_to"])   # re-run the SAME target the funnel picked
        out["downgrade"] = a
        out["downgrade_usd"] = r["downgrade_usd"] if a["verdict"] == "SAFE" else 0.0   # $ only when unanimously safe
    if "compress" in levers and b and r.get("compress_candidate"):
        # harness (system-prompt) compression: shrink it once, PROVE behaviour across the node's diverse inputs.
        cp = compress.prove(node, b)
        out["compress"] = cp
        out["compress_usd"] = round((cp.get("save_per_1k") or 0) * calls / 1000, 2) if cp.get("verdict") == "SAFE" else 0.0
    return out


def stream(source, ws, project, keys, snap, calls=None, levers=None):
    """Generator: yields SSE progress while proving the selected nodes CONCURRENTLY (bounded pool), then stores
    the frozen result keyed by the SNAPSHOT. Everything — rows AND per-call-site buckets — comes from the ONE pinned
    snapshot graph, so the keys the checkboxes captured can't drift here. `levers` = which levers to prove (default
    all three). Paid; overlapped."""
    levers = levers or list(LEVERS)      # default to config-enabled levers (downgrade only on this branch)
    g = snapshot.get(snap, source, ws, project)
    if g is None:                                                          # snapshot evicted -> don't clobber; reload
        yield {"type": "start", "total": 0, "agent": project}
        yield {"type": "complete", "total_usd": 0.0, "stale": True}
        return
    f = funnel.build(source, ws, project, calls=calls, levers=levers, g=g)   # the enabled levers, pinned graph
    rows = {r["key"]: r for r in f["rows"] if r["key"] in keys}            # identity = UNIQUE key, never the label
    order = [k for k in keys if k in rows]
    if not order:                                                          # selection went stale (no key matched) —
        yield {"type": "start", "total": 0, "agent": project}              # do NOT store an all-zero proof (that
        yield {"type": "complete", "total_usd": 0.0, "stale": True}        # would reset the hero to $0); keep prior
        return                                                             # state and let the user re-select
    by_key = g.get("buckets", {})                                          # same pinned graph -> keys always match

    yield {"type": "start", "total": len(order), "agent": project}
    for k in order:                                                        # announce every call-site (all in flight)
        yield {"type": "step", "key": k, "node": rows[k]["node"], "msg": _step_msg(rows[k])}

    done = {}
    with ThreadPoolExecutor(max_workers=min(4, len(order) or 1)) as ex:
        futs = {ex.submit(_prove_one, k, rows[k], by_key.get(k, []), f["calls"], levers): k for k in order}
        for fut in as_completed(futs):
            k = futs[fut]
            try:
                done[k] = fut.result()
            except Exception as e:                                         # one call-site failing must not kill the run
                import sys, traceback
                print("[prove] call-site %r failed: %s" % (k, e), file=sys.stderr)   # SURFACE it (was silent) so the
                traceback.print_exc()                                      # log shows the real cause (API/credits/etc.)
                r = rows[k]
                done[k] = {"key": k, "node": r["node"], "graph_path": r.get("graph_path", ""),
                           "model": r["model"], "cost": r["cost"],
                           "cache_usd": 0.0, "downgrade_usd": 0.0, "compress_usd": 0.0, "error": str(e)[:200]}
            yield {"type": "done_node", "key": k, "node": rows[k]["node"]}

    results = [done[k] for k in order]                                     # stable, selection order (not finish order)
    res = {"agent": project, "calls": f["calls"], "results": results,
           "cache_usd": round(sum(x.get("cache_usd", 0) for x in results), 2),
           "downgrade_usd": round(sum(x.get("downgrade_usd", 0) for x in results), 2),
           "compress_usd": round(sum(x.get("compress_usd", 0) for x in results), 2),
           "total": round(sum(x.get("cache_usd", 0) + x.get("downgrade_usd", 0) + x.get("compress_usd", 0)
                             for x in results), 2)}
    store.put(proof_key(source, ws, project, snap), res)
    snapdump.dump_results(snap, res)                          # dev-only (AUDIT_DEBUG): results/ for this snap
    yield {"type": "complete", "total_usd": res["total"]}
