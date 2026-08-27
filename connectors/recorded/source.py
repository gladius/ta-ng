"""Offline DataSource — serves the shipped `fixtures/*.json` as a real, KEYLESS data source, so the whole
pipeline (graph -> snapshot -> profile / report) runs end-to-end with no LangSmith credentials. The fixtures
are contract traces (see build_fixture.py); this exposes them as a source the web layer selects as
`recorded`. Flat: it inherits the base `pull_graph` (no run tree — fixture traces are already resolved to a
`node_id`), so a tagged fixture yields clean call-sites.
"""
import os
import json
import glob

from connectors.datasource import DataSource

_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
_TREE = "support-agent"                      # id of the built-in tree-corpus agent (connectors.langsmith.sample_tree)


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
        """DEV/DEMO source — visible only in non-prod. Gated on AUDIT_DEBUG (same truthiness as app's debugcap /
        snapdump), so with no AUDIT_DEBUG set (production) it drops off the home-page picker entirely. Read from the
        env directly to keep the connector layer self-contained (no dependency on app.*)."""
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
        # built-in TREE corpus — a rich multi-subgraph LangGraph deployment (tool/retriever nodes, subgraphs,
        # ReAct cycles + the failure cases). Folded through the SAME real pipeline as live, just keyless.
        from connectors.langsmith import sample_tree
        out.append({"id": _TREE, "name": "support-agent (deployment demo)",
                    "runs": len({r.get("trace_id") for r in sample_tree.runs()})})
        return out

    def pull_graph(self, ws_id, project, limit=None, since_hours=None):
        """The tree corpus folds the REAL run tree (topology + typed non-llm nodes); flat fixtures use the base."""
        if project == _TREE:
            from connectors import get_adapter, pull_graph
            from connectors.langsmith import sample_tree
            return pull_graph(get_adapter("langsmith"), runs=sample_tree.runs(), project=project)
        return super().pull_graph(ws_id, project, limit=limit, since_hours=since_hours)

    def pull(self, ws_id, project, limit=None, since_hours=None):
        """Raw contract traces for one fixture, bucketed by node_id. (buckets, skipped[])."""
        if project == _TREE:                                # tree corpus -> the folded graph's call-site buckets
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
