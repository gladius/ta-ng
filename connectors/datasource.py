"""DataSource base and registry: per-platform dashboard navigation, so the web layer never branches on platform.

A platform ships `connectors/<name>/source.py` exporting `SOURCE`. workspaces() -> [{id, name}], agents(ws_id) ->
[{id, name, runs?}], pull() -> (buckets, skipped) of raw contract traces; pricing and caching are the web layer's job.
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
        """(graph, skipped). The default is a flat graph from pull()'s buckets; sources with run trees override it."""
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
        """True if this source's credentials are present (drives the home-page picker)."""
        return True


def load_sources():
    """name -> DataSource for every connector subpackage whose source.py exports SOURCE."""
    out = {}
    for _, name, ispkg in pkgutil.iter_modules(_pkg.__path__):
        if not ispkg or name.startswith("_"):
            continue
        try:
            mod = importlib.import_module("connectors.%s.source" % name)
        except ModuleNotFoundError:
            continue                                   # ingestion-only connector
        src = getattr(mod, "SOURCE", None)
        if isinstance(src, DataSource) and src.name and src.name != "base":
            out[src.name] = src
    return out


def get_source(name):
    srcs = load_sources()
    key = str(name).lower()
    if key in srcs:
        return srcs[key]
    raise ValueError("unknown data source %r (available: %s)" % (name, ", ".join(sorted(srcs)) or "none"))
