"""Offline DataSource `recorded`: the shipped fixtures/*.json contract traces, so the whole pipeline runs without keys.
Fixture traces already carry a node_id, so they use the base pull_graph; the built-in tree corpus folds its run tree.
"""
import os
import json
import glob

from connectors.datasource import DataSource

_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
_TREE = "support-agent"                      # the built-in tree corpus (connectors.langsmith.sample_tree)


def _fixtures():
    """{fixture-name -> path} for every fixtures/*.json."""
    return {os.path.splitext(os.path.basename(p))[0]: p for p in glob.glob(os.path.join(_DIR, "*.json"))}


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _path_for(project):
    """The fixture file whose agent (or filename) matches `project`."""
    fx = _fixtures()
    if project in fx:
        return fx[project]
    for p in fx.values():
        try:
            if _load(p).get("agent") == project:
                return p
        except Exception:
            continue
    return None


class RecordedSource(DataSource):
    name = "recorded"

    def configured(self):
        """Dev and demo only: shown when AUDIT_DEBUG is set (unset in production). Read from the env directly so the
        connector layer doesn't depend on app.*."""
        return os.environ.get("AUDIT_DEBUG") not in (None, "", "0", "false", "False")

    def workspaces(self):
        return [{"id": "recorded", "name": "Recorded fixtures"}]

    def agents(self, ws_id):
        out = []
        for name, path in sorted(_fixtures().items()):
            try:
                d = _load(path)
            except Exception:
                continue
            out.append({"id": d.get("agent", name), "name": d.get("agent", name),
                        "runs": len(d.get("traces", []))})
        # The built-in tree corpus: a multi-subgraph deployment folded through the same pipeline as live data.
        from connectors.langsmith import sample_tree
        out.append({"id": _TREE, "name": "support-agent (deployment demo)",
                    "runs": len({r.get("trace_id") for r in sample_tree.runs()})})
        return out

    def pull_graph(self, ws_id, project, limit=None, since_hours=None):
        """The tree corpus folds its run tree (topology and non-llm nodes); flat fixtures use the base."""
        if project == _TREE:
            from connectors import get_adapter, pull_graph
            from connectors.langsmith import sample_tree
            return pull_graph(get_adapter("langsmith"), runs=sample_tree.runs(), project=project)
        return super().pull_graph(ws_id, project, limit=limit, since_hours=since_hours)

    def pull(self, ws_id, project, limit=None, since_hours=None):
        """(buckets by node_id, skipped): raw contract traces of one fixture."""
        if project == _TREE:
            g, skipped = self.pull_graph(ws_id, project)
            return g.buckets, skipped
        path = _path_for(project)
        if not path:
            return {}, []
        buckets = {}
        for t in _load(path).get("traces", []):
            buckets.setdefault(t.get("node_id") or "llm", []).append(t)
        return buckets, []


SOURCE = RecordedSource()
