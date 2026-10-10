"""Datasource navigation: the one seam between the web layer and the connectors. Read-only and never raises: a
missing credential or API hiccup degrades to an empty list."""
from connectors.datasource import load_sources, get_source


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def datasources():
    """[{id, name, configured}] for configured connectors only; unconfigured ones are hidden."""
    return [{"id": sid, "name": sid.replace("_", " ").title(), "configured": True}
            for sid, src in sorted(load_sources().items())
            if _safe(src.configured, False)]


def workspaces(source_id):
    """[{id, name}] for the source (empty if unconfigured / unreachable)."""
    return _safe(lambda: get_source(source_id).workspaces(), [])


def agents(source_id, ws_id):
    """[{id, name, runs?}]: the auditable agents in a workspace."""
    return _safe(lambda: get_source(source_id).agents(ws_id), [])


def workspace_name(source_id, ws_id):
    return _safe(lambda: get_source(source_id).workspace_name(ws_id), ws_id)
