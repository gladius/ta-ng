"""Immutable per-report snapshot: the graph is fetched once and pinned under an id the report URL carries, so every
step reads the same data (a live re-fetch would drift the keys and zero every $). Held in `store`, no TTL."""
import secrets


def new_id():
    return secrets.token_urlsafe(9)


def build_into(sid, source, ws, project):
    """Fetch and build the graph once and pin it under `sid` (minted first, so the page can show progress)."""
    from app.services import graph, store, snapdump
    g = graph.build(source, ws, project)
    store.put(("snapshot", sid), {"source": source, "ws": ws, "project": project, "graph": g})
    snapdump.dump_graph(sid, g)
    return g


def get(snap_id, source=None, ws=None, project=None):
    """The pinned graph for `snap_id`, or None. Given source/ws/project must match (rejects a cross-agent id)."""
    from app.services import store
    s = store.peek(("snapshot", snap_id))
    if not s:
        return None
    if project is not None and (s["source"], s["ws"], s["project"]) != (source, ws, project):
        return None
    return s["graph"]
