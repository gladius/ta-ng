"""Immutable per-report SNAPSHOT — the ONE honest source of a report's data for its whole lifecycle.

Problem it solves: an agent's traces are fetched LIVE from the platform. If we re-fetch/rebuild between rendering
the report and proving it, the call-site set and their keys drift under the user (new traces arrive, the fetch
window shifts), the selected keys go stale, the proof matches nothing, and every $ resets to 0. So we capture the
graph ONCE, pin it under a snapshot id that the report URL carries, and every step — view, prove, reprove, download
— reads THAT snapshot by id. Nothing re-derives from the platform mid-flow, so identity can't move under the user.
A NEW snapshot (fresh data) is minted only on an explicit re-audit.

Durability: the snapshot lives in the SQLite-backed `store` (shared across workers, survives restarts) — so a
page-load worker and a prove worker see the SAME snapshot. A small in-process LRU caches the immutable snapshot for
speed (immutable -> no staleness). No TTL: an immutable snapshot can't go stale.
"""
import secrets
import threading
from collections import OrderedDict

_MEM = OrderedDict()            # snap_id -> (source, ws, project, graph) — in-proc read cache (immutable, safe)
_MAX = 32
_LOCK = threading.Lock()


def new_id():
    return secrets.token_urlsafe(9)


def _cache(sid, entry):
    with _LOCK:
        _MEM[sid] = entry
        _MEM.move_to_end(sid)
        while len(_MEM) > _MAX:
            _MEM.popitem(last=False)


def create(source, ws, project):
    """Fetch + build the graph ONCE, pin it under a fresh snapshot id (durable in the shared store). Returns
    (snap_id, graph)."""
    from app.services import graph, store
    g = graph.build(source, ws, project)
    sid = new_id()
    store.put(("snapshot", sid), {"source": source, "ws": ws, "project": project, "graph": g})
    _cache(sid, (source, ws, project, g))
    return sid, g


def get(snap_id, source=None, ws=None, project=None):
    """The pinned graph for `snap_id`, or None (unknown/evicted). If source/ws/project are given they must match —
    guards a stale or cross-agent id pasted from a bookmarked URL. Reads the in-proc cache, else the shared store."""
    from app.services import store
    with _LOCK:
        entry = _MEM.get(snap_id)
        if entry:
            _MEM.move_to_end(snap_id)
    if entry is None:
        s = store.peek(("snapshot", snap_id))
        if not s:
            return None
        entry = (s["source"], s["ws"], s["project"], s["graph"])
        _cache(snap_id, entry)
    src, w, p, g = entry
    if project is not None and (src, w, p) != (source, ws, project):
        return None
    return g
