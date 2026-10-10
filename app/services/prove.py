"""Prove: the paid step. For each selected call-site, audit.audit_node re-runs the cheaper model on distinct inputs
and judges it against the original. `stream()` yields progress and stores the frozen result, so every reload matches."""
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.services import audit, funnel, store, snapshot, snapdump, debugcap


def proof_key(source, ws, project, snap=""):
    return (source, ws, project, "proof", snap)             # tied to the snapshot it ran against


def _step_msg(r):
    return ("downgrade → %s · proving live" % r["downgrade_to"]) if r["downgrade"] else "proving live"


def _prove_one(key, r, b):
    """Prove one call-site in a worker thread (paid calls overlap). Only a SAFE downgrade keeps its $."""
    node = r["node"]
    out = {"key": key, "node": node, "graph_path": r.get("graph_path", ""),
           "model": r["model"], "cost": r["cost"], "downgrade_usd": 0.0}
    if r["downgrade"] and b:
        a = audit.audit_node(node, b, cheaper=r["downgrade_to"])
        out["downgrade"] = a
        out["downgrade_usd"] = r["downgrade_usd"] if a["verdict"] == "SAFE" else 0.0
    return out


def stream(source, ws, project, keys, snap, calls=None, dgmode="commercial"):
    """Prove the selected call-sites concurrently, yielding SSE progress, then store the frozen result by snapshot.
    Rows and buckets come from the pinned snapshot, so keys can't drift; `dgmode` must match the one shown."""
    g = snapshot.get(snap, source, ws, project)
    if g is None:                                                          # snapshot gone: stale, store nothing
        yield {"type": "start", "total": 0, "agent": project}
        yield {"type": "complete", "total_usd": 0.0, "stale": True}
        return
    f = funnel.build(source, ws, project, calls=calls, g=g, dgmode=dgmode)
    rows = {r["key"]: r for r in f["rows"] if r["key"] in keys}
    order = [k for k in keys if k in rows]
    if not order:                                                          # stale selection: an all-zero proof
        yield {"type": "start", "total": 0, "agent": project}              # would replace the prior one
        yield {"type": "complete", "total_usd": 0.0, "stale": True}
        return
    by_key = g.get("buckets", {})

    yield {"type": "start", "total": len(order), "agent": project}
    for k in order:
        yield {"type": "step", "key": k, "node": rows[k]["node"], "msg": _step_msg(rows[k])}

    done = {}
    if debugcap.enabled():
        debugcap.reset()
    with ThreadPoolExecutor(max_workers=min(4, len(order) or 1)) as ex:
        futs = {ex.submit(_prove_one, k, rows[k], by_key.get(k, [])): k for k in order}
        for fut in as_completed(futs):
            k = futs[fut]
            try:
                done[k] = fut.result()
            except Exception as e:                                         # one call-site failing must not kill the run
                import sys, traceback
                print("[prove] call-site %r failed: %s" % (k, e), file=sys.stderr)
                traceback.print_exc()
                r = rows[k]
                done[k] = {"key": k, "node": r["node"], "graph_path": r.get("graph_path", ""),
                           "model": r["model"], "cost": r["cost"], "downgrade_usd": 0.0, "error": str(e)[:200]}
            yield {"type": "done_node", "key": k, "node": rows[k]["node"]}

    results = [done[k] for k in order]
    total = round(sum(x.get("downgrade_usd", 0) for x in results), 2)
    res = {"agent": project, "calls": f["calls"], "results": results, "dgmode": dgmode,
           "downgrade_usd": total, "total": total}
    store.put(proof_key(source, ws, project, snap), res)
    snapdump.dump_results(snap, res)
    snapdump.dump_judge_cache(snap)
    snapdump.dump_judge_raw(snap)
    yield {"type": "complete", "total_usd": res["total"]}
