"""Prove — the paid step. For the nodes the user selected, run a REAL provider round-trip and turn each
deterministic downgrade candidate into measured evidence: audit.audit_node re-runs the cheaper model on distinct
inputs and judges it against the original's own outputs.

`stream()` yields progress events (for SSE) as it goes and stores the final, frozen result via store.put so the
report reads identical numbers on every reload. Nothing is asserted here that isn't measured by a live call.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.services import audit, funnel, store, snapshot, snapdump, debugcap


def proof_key(source, ws, project, snap=""):
    return (source, ws, project, "proof", snap)             # proof is tied to the SNAPSHOT it was run against


def _step_msg(r):
    return ("downgrade → %s · proving live" % r["downgrade_to"]) if r["downgrade"] else "proving live"


def _prove_one(key, r, b):
    """Prove ONE call-site. Runs in a worker thread → paid calls happen concurrently. Identified by the UNIQUE key;
    `node` is the display label (may repeat). The $ only counts what actually PROVED — a drifted downgrade is $0."""
    node = r["node"]
    out = {"key": key, "node": node, "graph_path": r.get("graph_path", ""),
           "model": r["model"], "cost": r["cost"], "downgrade_usd": 0.0}
    if r["downgrade"] and b:
        a = audit.audit_node(node, b, cheaper=r["downgrade_to"])   # re-run the SAME target the funnel picked
        out["downgrade"] = a
        out["downgrade_usd"] = r["downgrade_usd"] if a["verdict"] == "SAFE" else 0.0
    return out


def stream(source, ws, project, keys, snap, calls=None, dgmode="commercial"):
    """Generator: yields SSE progress while proving the selected nodes CONCURRENTLY (bounded pool), then stores
    the frozen result keyed by the SNAPSHOT. Everything — rows AND per-call-site buckets — comes from the ONE pinned
    snapshot graph, so the keys the checkboxes captured can't drift here. `dgmode` = the downgrade target universe
    ('commercial' | 'open-weight'); it MUST match what the select page showed, so we re-run the very model the user
    saw, and it is frozen into the proof for the report. Paid; overlapped."""
    g = snapshot.get(snap, source, ws, project)
    if g is None:                                                          # snapshot evicted -> don't clobber; reload
        yield {"type": "start", "total": 0, "agent": project}
        yield {"type": "complete", "total_usd": 0.0, "stale": True}
        return
    f = funnel.build(source, ws, project, calls=calls, g=g, dgmode=dgmode)   # SAME universe as select
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
    if debugcap.enabled():
        debugcap.reset()                                                   # AUDIT_DEBUG: fresh judge-cache tally
    with ThreadPoolExecutor(max_workers=min(4, len(order) or 1)) as ex:
        futs = {ex.submit(_prove_one, k, rows[k], by_key.get(k, [])): k for k in order}
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
                           "model": r["model"], "cost": r["cost"], "downgrade_usd": 0.0, "error": str(e)[:200]}
            yield {"type": "done_node", "key": k, "node": rows[k]["node"]}

    results = [done[k] for k in order]                                     # stable, selection order (not finish order)
    total = round(sum(x.get("downgrade_usd", 0) for x in results), 2)
    res = {"agent": project, "calls": f["calls"], "results": results, "dgmode": dgmode,   # frozen: which universe
           "downgrade_usd": total, "total": total}
    store.put(proof_key(source, ws, project, snap), res)
    snapdump.dump_results(snap, res)                          # dev-only (AUDIT_DEBUG): results/ for this snap
    snapdump.dump_judge_cache(snap)                           # dev-only (AUDIT_DEBUG): the judge-cache proof
    snapdump.dump_judge_raw(snap)                             # dev-only (AUDIT_DEBUG): the RAW judge replies vs parsed
    yield {"type": "complete", "total_usd": res["total"]}
