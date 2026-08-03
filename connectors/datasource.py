"""DataSource base + registry — the dashboard-NAVIGATION counterpart to the Adapter (ingestion).

An Adapter turns a platform's recorded runs into contract traces (INGESTION). A DataSource adds the
NAVIGATION a dashboard needs — list the auditable agents and pull a call-site's raw traces — so the web
layer never branches on platform. Adding a platform to the dashboard = drop `connectors/<name>/source.py`
exporting `SOURCE`; the web layer selects the active one by name via `get_source()`.

A source implements three platform-specific methods; the rest is derived here. Everything returns neutral
shapes (dicts / contract traces) — no platform types leak out:

    workspaces()                   -> [{id, name}]     (a platform without workspaces returns one default)
    agents(ws_id)                  -> [{id, name, runs?}]
    pull(ws_id, project, limit=,   -> (buckets, skipped)   RAW contract traces (delegates to the Adapter);
         since_hours=)                 pricing-normalization + caching are the web layer's job, not the source's.

Depends only on stdlib + `connectors` — NOT on `auditor` — so the connector layer stays self-contained.
"""

import importlib
import pkgutil

import connectors as _pkg


class DataSource:
    name = "base"

    def workspaces(self):
        raise NotImplementedError

    def agents(self, ws_id):
        raise NotImplementedError

    def pull(self, ws_id, project, limit=None, since_hours=None):
        raise NotImplementedError

    def pull_graph(self, ws_id, project, limit=None, since_hours=None):
        """(graph, skipped) — the execution graph (call-sites + edges + timing/volume). Default: derive a
        FLAT graph from pull()'s buckets (no topology). A source with real run trees overrides this to fold
        the tree (see connectors/langsmith/source.py)."""
        from connectors.graph import build_graph
        buckets, skipped = self.pull(ws_id, project, limit=limit, since_hours=since_hours)
        records = [{"id": t.get("trace_id"), "trace_id": t.get("trace_id"), "parent_run_id": None,
                    "dotted_order": "", "run_type": "llm", "name": t.get("node_id") or "llm",
                    "start_time": t.get("start_time", ""), "end_time": "",
                    "agent_hint": t.get("agent_id"), "node_hint": t.get("node_id") or None, "trace": t}
                   for b in buckets.values() for t in b]
        return build_graph(records, agent_default=project), skipped

    def workspace_name(self, ws_id):
        return next((w["name"] for w in self.workspaces() if w["id"] == ws_id), ws_id)

    def configured(self):
        """True if this source's credentials are present (drives the home-page picker). Default: assume yes."""
        return True


def load_sources():
    """name -> DataSource instance, for every connector subpackage that ships a source.py exporting SOURCE.
    A connector may be ingestion-only (adapter but no source) — it is simply skipped for the dashboard."""
    out = {}
    for _, name, ispkg in pkgutil.iter_modules(_pkg.__path__):
        if not ispkg or name.startswith("_"):
            continue
        try:
            mod = importlib.import_module("connectors.%s.source" % name)
        except ModuleNotFoundError:
            continue                                   # ingestion-only connector (no dashboard navigation)
        src = getattr(mod, "SOURCE", None)
        if isinstance(src, DataSource) and src.name and src.name != "base":
            out[src.name] = src
    return out


def list_sources():
    return sorted(load_sources())


def get_source(name):
    srcs = load_sources()
    key = str(name).lower()
    if key in srcs:
        return srcs[key]
    raise ValueError("unknown data source %r (available: %s)" % (name, ", ".join(sorted(srcs)) or "none"))
