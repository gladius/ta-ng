"""Immutable per-report SNAPSHOT — the ONE honest source of a report's data for its whole lifecycle.

Problem it solves: an agent's traces are fetched LIVE from the platform. If we re-fetch/rebuild between rendering
the report and proving it, the call-site set and their keys drift under the user (new traces arrive, the fetch
window shifts), the selected keys go stale, the proof matches nothing, and every $ resets to 0. So we capture the
graph ONCE, pin it under a snapshot id that the report URL carries, and every step — view, prove, reprove, download
— reads THAT snapshot by id. Nothing re-derives from the platform mid-flow, so identity can't move under the user.
A NEW snapshot (fresh data) is minted only on an explicit re-audit.

Held in the in-memory `store` (see its deployment note: run ONE Cloud Run instance). No TTL — an immutable snapshot
can't go stale.
"""
import secrets


def new_id():
    return secrets.token_urlsafe(9)


def build_into(sid, source, ws, project):
    """Fetch + build the graph ONCE and pin it under the GIVEN snapshot id; returns the graph. Splitting the
    id from the build lets the web layer mint an id, render a progress page immediately, then run this in the
    background under that same id (see server.build_stream) — so the multi-second fetch isn't a frozen page."""
    from app.services import graph, store
    g = graph.build(source, ws, project)
    store.put(("snapshot", sid), {"source": source, "ws": ws, "project": project, "graph": g})
    return g


def create(source, ws, project):
    """Fetch + build the graph ONCE and pin it under a fresh snapshot id. Returns (snap_id, graph)."""
    sid = new_id()
    return sid, build_into(sid, source, ws, project)


def get(snap_id, source=None, ws=None, project=None):
    """The pinned graph for `snap_id`, or None (unknown/evicted). If source/ws/project are given they must match —
    guards a stale or cross-agent id pasted from a bookmarked URL."""
    from app.services import store
    s = store.peek(("snapshot", snap_id))
    if not s:
        return None
    if project is not None and (s["source"], s["ws"], s["project"]) != (source, ws, project):
        return None
    return s["graph"]
