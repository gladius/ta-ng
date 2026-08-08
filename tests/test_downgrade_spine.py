"""Deterministic spine of the downgrade audit — parse real LangSmith shape -> group -> graph_path -> rank.
No LLM, no network, $0. Run: python -m tests.test_downgrade_spine  (from repo root)."""
import json
import os
from collections import OrderedDict

from connectors.langsmith.adapter import LangSmithAdapter, _graph_path
from connectors.datasource import DataSource
from app.services import graph as gmod, downgrade
from auditor.util import next_cheaper, PRICE

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


def test_pareto_ladder():
    # The downgrade target must be cheaper on BOTH axes. gemini-2.5-pro ($1.25/$10): the newer gemini-3.6-flash
    # ($1.50/$7.50) is PRICIER on input, so it must NOT be chosen — 3-flash ($0.50/$3.0) is the real drop.
    tgt = next_cheaper("gemini-2.5-pro")
    assert tgt == "gemini-3-flash", "2.5-pro must skip the pricier-input flash: got %s" % tgt
    assert PRICE[tgt]["input"] <= PRICE["gemini-2.5-pro"]["input"] and PRICE[tgt]["output"] <= PRICE["gemini-2.5-pro"]["output"]
    # healthy cases unchanged: 3.1-pro -> 3.6-flash IS cheaper on both, and sonnet-5 -> haiku-4-5 as before.
    assert next_cheaper("gemini-3.1-pro") == "gemini-3.6-flash", next_cheaper("gemini-3.1-pro")
    assert next_cheaper("claude-sonnet-5") == "claude-haiku-4-5", next_cheaper("claude-sonnet-5")
    print("[ok] pareto ladder: 2.5-pro -> 3-flash (skips pricier-input 3.6-flash); healthy drops unchanged")


def test_node_aware_pick():
    # With a call-site's real token mix, the target is the NEWEST lower-tier model that genuinely net-saves for
    # THAT mix — not a single fixed Pareto pick. gemini-2.5-pro ($1.25/$10):
    #   output-heavy node -> gemini-3.6-flash: pricier input ($1.50) but far cheaper output ($7.50) -> net saves,
    #                        and it's the LATEST flash, so the gentlest capable drop.
    #   input-heavy node  -> gemini-3-flash: 3.6-flash net-LOSES on all that input, so it falls to the cheaper one.
    assert next_cheaper("gemini-2.5-pro", 100, 2000) == "gemini-3.6-flash", next_cheaper("gemini-2.5-pro", 100, 2000)
    assert next_cheaper("gemini-2.5-pro", 5000, 50) == "gemini-3-flash", next_cheaper("gemini-2.5-pro", 5000, 50)
    # a node with no measurable tokens (unknown volume) yields NO candidate rather than an unprovable saving.
    assert next_cheaper("gemini-2.5-pro", 0, 0) is None
    # candidates() threads the node mix through: an output-heavy pro node lands on the latest flash.
    heavy = [{"key": "a/deep", "node": "deep", "model": "gemini-2.5-pro",
              "avg_in": 200, "avg_out": 4000, "calls": 10, "graph_path": ""}]
    c = downgrade.candidates(heavy, per_calls=10000)[0]
    assert c["cheaper"] == "gemini-3.6-flash" and c["usd"] > 0, c
    print("[ok] node-aware pick: output-heavy -> latest 3.6-flash, input-heavy -> cheaper 3-flash, no-tokens -> none")


def test_thinking_detection():
    ad = LangSmithAdapter()

    def rec(tok_details=None, inv_extra=None):
        um = {"prompt_tokens": 100, "completion_tokens": 10}
        if tok_details is not None:
            um["completion_tokens_details"] = tok_details
        return {"run_type": "llm", "id": "1", "trace_id": "t", "name": "ChatX",
                "inputs": {"messages": [{"role": "user", "content": "hi"}]},
                "outputs": {"llm_output": {"token_usage": um}},
                "extra": {"metadata": {"agent_id": "a", "langgraph_node": "n"},
                          "invocation_params": dict({"model": "claude-sonnet-5"}, **(inv_extra or {}))},
                "prompt_tokens": 100, "completion_tokens": 10}

    # 1) reasoning-token COUNT (the universal signal — how it looks through a litellm/OpenAI-normalized gateway)
    assert ad.to_trace(rec(tok_details={"reasoning_tokens": 64}))["thinking_enabled"] is True
    # 2) invocation param flag (reasoning_effort / thinking), no token detail
    assert ad.to_trace(rec(inv_extra={"reasoning_effort": "high"}))["thinking_enabled"] is True
    # 3) no signal -> not thinking (don't force reasoning onto a plain node)
    assert ad.to_trace(rec())["thinking_enabled"] is False
    print("[ok] thinking detected from reasoning_tokens (litellm-uniform) AND param flags; off when absent")


def test_same_label_distinct_keys():
    # Two call-sites share the display label 'supervisor' but are DISTINCT call-sites (distinct keys). They must
    # stay two rows and both survive prove's key-indexed dict — the old label-keyed dict silently dropped one.
    nodes = [
        {"key": "agent/supervisor~plan", "node": "supervisor", "model": "claude-sonnet-5",
         "avg_in": 2000, "avg_out": 300, "calls": 10, "graph_path": "researcher"},
        {"key": "agent/supervisor~critique", "node": "supervisor", "model": "claude-sonnet-5",
         "avg_in": 2000, "avg_out": 300, "calls": 10, "graph_path": "writer"},
    ]
    cands = downgrade.candidates(nodes, per_calls=10000)
    assert len(cands) == 2 and {c["key"] for c in cands} == {"agent/supervisor~plan", "agent/supervisor~critique"}
    assert all(c["node"] == "supervisor" for c in cands)
    assert len({c["key"]: c for c in cands}) == 2, "key-indexed (the fix): both kept"
    assert len({c["node"]: c for c in cands}) == 1, "label-indexed (the old bug): WOULD collapse to one"
    print("[ok] same-label call-sites keep distinct keys through candidates + prove indexing")


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
    test_pareto_ladder()
    test_node_aware_pick()
    test_thinking_detection()
    test_same_label_distinct_keys()
    test_spine()
    print("\nALL DETERMINISTIC SPINE TESTS PASSED ($0, no network)")
