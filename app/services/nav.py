"""Datasource navigation service — the ONE seam between the web layer and the connectors.

Routes call this; this calls `connectors.datasource`. Nothing in `app/web` touches a connector directly, so
the platform stays behind the connector boundary. Read-only; a nav call NEVER raises into a page (a missing
credential / API hiccup degrades to an empty list, not a 500).
"""
from connectors.datasource import load_sources, get_source


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def datasources():
    """[{id, name, configured}] for every connector that is CONFIGURED (has credentials / is enabled). An
    unconfigured source (e.g. Galileo with no key) is HIDDEN entirely, not shown greyed-out. `recorded` gates
    itself to non-prod (see connectors/recorded/source.py), so it drops off in production."""
    return [{"id": sid, "name": sid.replace("_", " ").title(), "configured": True}
            for sid, src in sorted(load_sources().items())
            if _safe(src.configured, False)]


def workspaces(source_id):
    """[{id, name}] for the source (empty if unconfigured / unreachable)."""
    return _safe(lambda: get_source(source_id).workspaces(), [])


def agents(source_id, ws_id):
    """[{id, name, runs?}] — the auditable agents/projects in a workspace."""
    return _safe(lambda: get_source(source_id).agents(ws_id), [])


def workspace_name(source_id, ws_id):
    return _safe(lambda: get_source(source_id).workspace_name(ws_id), ws_id)
