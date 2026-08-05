"""Deterministic spine of the downgrade audit — parse real LangSmith shape -> group -> graph_path -> rank.
No LLM, no network, $0. Run: python -m tests.test_downgrade_spine  (from repo root)."""
import json
import os
from collections import OrderedDict

from connectors.langsmith.adapter import LangSmithAdapter, _graph_path
from connectors.datasource import DataSource
from app.services import graph as gmod, downgrade

_SAMPLE = os.path.join("connectors", "langsmith", "sample_runs.json")


def _parse_sample():
    runs = json.load(open(_SAMPLE, encoding="utf-8"))
    ad = LangSmithAdapter()
    return [t for t in (ad.to_trace(r, project="support-agent") for r in runs) if t]


def _build_graph(traces):
    """Drive the REAL app graph.build (-> the ONE rich builder via pull_graph) over the parsed traces.
    The stub subclasses DataSource, so it inherits the base pull_graph that folds pull()'s records."""
    class _Stub(DataSource):
        name = "stub"

        def pull(self, ws, proj, limit=None, since_hours=None):
            b = OrderedDict()
            for t in traces:
                b.setdefault(t["node_id"], []).append(t)
            return b, []
    orig = gmod.get_source
    gmod.get_source = lambda _id: _Stub()
    try:
        return gmod.build("stub", "ws", "support-agent")
    finally:
        gmod.get_source = orig


def test_graph_path_parser():
    # LangGraph checkpoint_ns -> node path, uuids + generic segments dropped
    assert _graph_path({"langgraph_checkpoint_ns": "billing:abc-123|refund:def-456"}) == "billing/refund"
    assert _graph_path({"langgraph_checkpoint_ns": "__start__:x|triage:y"}) == "triage"
    assert _graph_path({"subgraph": "payments"}) == "payments"       # explicit hint fallback
    assert _graph_path({}) == ""                                     # single-level agent -> empty
    print("[ok] graph_path parser")


def test_spine():
    traces = _parse_sample()
    assert len(traces) == 4, "4 llm runs parsed, the chain span skipped (got %d)" % len(traces)
    assert all("graph_path" in t for t in traces), "graph_path carried on every trace"
    assert all(t["graph_path"] == "" for t in traces), "sample is single-level -> empty graph_path"

    g = _build_graph(traces)
    nodes = {n["node"]: n for n in g["nodes"]}
    assert set(nodes) == {"triage", "responder"}, "grouped by framework node identity: %s" % set(nodes)
    assert nodes["triage"]["model"].startswith("claude-sonnet"), nodes["triage"]["model"]
    assert nodes["responder"]["mixed"], "responder saw two unknown models -> mixed"
    assert g["graphs"] == [], "no subgraphs in the sample"

    cands = downgrade.candidates(g["nodes"])
    names = [c["node"] for c in cands]
    assert names == ["triage"], "only triage is a real candidate (responder models are off-catalog): %s" % names
    c = cands[0]
    assert c["cheaper"] == "claude-haiku-4-5" and c["usd"] > 0, c
    print("[ok] spine: triage %s -> %s  ~$%.2f/1k calls | responder correctly not a candidate"
          % (c["model"], c["cheaper"], c["usd"]))


if __name__ == "__main__":
    test_graph_path_parser()
    test_spine()
    print("\nALL DETERMINISTIC SPINE TESTS PASSED ($0, no network)")
