"""Whole-DEPLOYMENT graph invariants — the failure cases the engine must survive, asserted on the golden
tree corpus (connectors/langsmith/sample_tree.py) folded through the REAL adapter + build_graph (offline,
no key). Deterministic, $0, no network. Run: python -m tests.test_tree_deployment  (from repo root).

Each test pins ONE planted failure case so a regression names itself."""
from connectors import get_adapter, pull_graph
from connectors.langsmith.sample_tree import runs

G, _SKIP = pull_graph(get_adapter("langsmith"), runs=runs(), project="support-agent")   # built once (read-only)


def _short(k):
    return k.replace("support-agent/", "")


def _nodes():
    return {_short(n["key"]): n for n in G.nodes}


def _struct():
    return {_short(n["key"]): n for n in G.structural_nodes}


def _edges():
    return {(_short(e["src"]), _short(e["dst"])): e["count"] for e in G.edges}


def test_whole_deployment_has_typed_nonllm_nodes():
    # the deployment is NOT just llm — tool + retriever runs become their own typed nodes (no tokens/cost).
    s = _struct()
    assert "billing/lookup_account" in s and s["billing/lookup_account"]["type"] == "tool"
    assert "refund/issue_refund" in s and s["refund/issue_refund"]["type"] == "tool"
    assert "kb_search" in s and s["kb_search"]["type"] == "retriever"
    assert all("model" not in n and "avg_in" not in n for n in G.structural_nodes), "structural nodes carry NO tokens"
    print("[ok] whole deployment: tool + retriever runs surfaced as typed non-llm nodes")


def test_tool_error_surfaced_with_latency():
    # a tool that ERRORED (issue_refund: gateway 504) shows error_rate + its slow wall-clock; a healthy tool doesn't.
    s = _struct()
    assert s["refund/issue_refund"]["errors"] == 1 and s["refund/issue_refund"]["error_rate"] == 1.0
    assert s["refund/issue_refund"]["avg_ms"] >= 3000, "the 3s timeout is preserved, not clamped"
    assert s["billing/lookup_account"]["error_rate"] == 0.0
    print("[ok] tool error + latency surfaced on the non-llm node")


def test_same_name_node_split_by_subgraph():
    # 'worker' exists in BOTH the billing and refund subgraphs — they must be DISTINCT call-sites, never blended.
    n = _nodes()
    billing = [k for k in n if k.startswith("billing/worker")]
    refund = [k for k in n if k.startswith("refund/worker")]
    assert billing and refund and not (set(billing) & set(refund)), (billing, refund)
    print("[ok] same-named 'worker' split across billing vs refund subgraphs")


def test_graph_path_top_level_not_self_nested():
    # the fixed bug: a top-level tagged node must NOT be nested under its own name ('supervisor/supervisor').
    n = _nodes()
    for top in ("supervisor", "research", "summarize", "responder"):
        assert top in n and n[top]["graph_path"] == "", "%s should be top-level, got %r" % (top, n[top]["graph_path"])
        assert "%s/%s" % (top, top) not in n, "%s is self-nested — graph_path fix regressed" % top
    print("[ok] top-level nodes not self-nested (graph_path exclude working)")


def test_react_pre_post_tool_split_but_one_topology_node():
    # a ReAct node emits pre-tool + post-tool llm calls = TWO call-sites (different prompts), but it is ONE node
    # in the topology (a single 'worker' the flow loops through), not two.
    n = _nodes()
    workers = [k for k in n if k.startswith("billing/worker")]
    assert len(workers) == 2, "pre/post-tool are distinct call-sites: %s" % workers
    e = _edges()
    assert ("supervisor", "billing/worker") in e, "topology collapses the split to ONE billing/worker node"
    print("[ok] ReAct pre/post-tool = 2 call-sites, 1 topology node")


def test_edges_subgraph_qualified_with_cycles():
    # edges are subgraph-qualified (billing-worker != refund-worker) and capture the ReAct tool cycles.
    e = _edges()
    assert ("supervisor", "billing/worker") in e and ("supervisor", "refund/worker") in e, "workers not disambiguated"
    assert ("billing/worker", "billing/lookup_account") in e and ("billing/lookup_account", "billing/worker") in e, \
        "billing ReAct cycle missing"
    assert ("refund/worker", "refund/issue_refund") in e and ("refund/issue_refund", "refund/worker") in e, \
        "refund ReAct cycle missing"
    print("[ok] edges subgraph-qualified; both ReAct cycles present")


def test_failed_llm_excluded_but_counted_recovered_kept():
    # T4's billing worker errored with EMPTY output -> excluded from the bucket but counted. T2's refund worker
    # errored but produced REAL output (recovered) -> KEPT. So exactly ONE sample is excluded across the deployment.
    assert G.errors_excluded == 1, "expected exactly the 1 empty-output failure excluded, got %d" % G.errors_excluded
    n = _nodes()
    assert n["billing/worker~initial"]["errors_excluded"] == 1
    assert any(k.startswith("refund/worker") for k in n), "recovered refund worker must remain a real call-site"
    print("[ok] failed llm excluded+counted; recovered error kept (errors_excluded == 1)")


def test_untagged_deep_node_flagged():
    # an llm with no langgraph_node under a generic parent falls back to run.name and is FLAGGED untagged (a warn),
    # never silently trusted as a real node label.
    n = _nodes()
    assert "ChatOpenAI" in n and n["ChatOpenAI"]["untagged"] is True
    print("[ok] untagged deep-agent node flagged, not silently trusted")


def test_window_sliced_parent_does_not_crash():
    # T6's run points at a parent OUTSIDE the fetched window (a sliced trace tree). The fold must not crash; the
    # run resolves to a top-level node. Reaching here (G built at import) already proves no exception was raised.
    assert G.trace_count == 7, "all 7 traces folded despite the sliced parent"
    print("[ok] window-sliced parent folds without crashing")


def test_mixed_model_flagged():
    # responder ran on sonnet in one trace and gpt-4o-mini in another -> mixed_model, not silently one model.
    n = _nodes()
    assert n["responder"]["mixed_model"] is True and n["responder"]["samples"] == 2
    print("[ok] mixed-model node flagged")


def test_unknown_model_preserved_not_invented():
    # an unknown model is kept verbatim (priced as unknown downstream), never coerced to a known one.
    n = _nodes()
    assert n["summarize"]["model"] == "some-new-model-9000"
    print("[ok] unknown model preserved verbatim")


def test_generic_scaffolding_filtered_everywhere():
    # LangGraph / RunnableSequence / __start__ are structure, not nodes — absent from nodes, structural, AND edges.
    labels = set(_nodes()) | set(_struct())
    for e in G.edges:
        labels.add(_short(e["src"])); labels.add(_short(e["dst"]))
    for junk in ("LangGraph", "RunnableSequence", "__start__"):
        assert not any(junk in lbl for lbl in labels), "%s leaked into the graph" % junk
    print("[ok] generic scaffolding filtered from nodes + edges")


def test_revisions_captured_distinct_sorted():
    assert G.revisions == ["sha1", "sha2"], G.revisions
    print("[ok] deployment versions (revisions) captured distinct + sorted")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nALL WHOLE-DEPLOYMENT INVARIANTS PASSED ($0, no network)")
