"""Fact-pack invariants — the deterministic $0 profile fact-pack on the recorded fixtures. No LLM, no network.
Run: python -m tests.test_facts  (from repo root)."""
from app.services import graph
from app.services.profile.facts import fact_pack, _fs_distinct


def test_frame_stripped_distinct():
    # the review fix: a TEMPLATED output (varying id) must collapse to ONE structural form, not read as free-form.
    assert _fs_distinct(["Refund for order 123", "Refund for order 456", "Refund for order 789"]) == 1
    assert _fs_distinct(["billing", "refund", "billing", "security"]) == 3, "distinct labels stay distinct"
    assert _fs_distinct(["The weather is nice", "Quantum physics is hard", "I like pizza"]) == 3, "free-form stays high"
    print("[ok] frame-strip: templated->1, labels->3, free-form->3")


def test_meridian_llm_pack():
    fp = fact_pack(graph.build("recorded", "recorded", "meridian-support"))
    assert len(fp) == 6 and all(f["kind"] == "llm" for f in fp.values()), "6 llm nodes, all llm-kind"
    for f in fp.values():
        assert f["distinct_system"] >= 1
        assert f["output_type"] in ("json", "short", "free-form")
        assert f["flow"] == {"upstream": [], "downstream": []}, "flat source -> no edges -> empty flow"
    assert fp["meridian-support/route"]["output_type"] == "short", "route is the short-output classifier"
    # a node whose system VARIES per call is surfaced — exactly the compress gap the profiler's fact fixes.
    assert fp["meridian-support/triage"]["distinct_system"] > 1, "triage has a dynamic system -> flagged"
    print("[ok] meridian: 6 llm packs; output_type + distinct_system surfaced (dynamic-system node flagged)")


def test_support_agent_two_variants_and_flow():
    fp = fact_pack(graph.build("recorded", "recorded", "support-agent"))
    nonllm = {k: v for k, v in fp.items() if v["kind"] in ("tool", "retriever")}
    assert len(nonllm) == 3, "3 typed non-llm nodes (2 tool + 1 retriever)"
    for v in nonllm.values():
        assert v["op"] == v["node"] and "distinct_system" not in v, "non-llm: name IS op, no llm facts"
    # flow-context populated from the tree edges — both directions, both node kinds.
    assert "worker" in fp["support-agent/supervisor"]["flow"]["downstream"], "supervisor feeds the worker"
    kb = fp["support-agent/kb_search"]
    assert kb["flow"]["upstream"] == ["research"] and kb["flow"]["downstream"] == ["summarize"], \
        "retriever sits research -> summarize"
    print("[ok] support-agent: 2 fact-pack variants; non-llm name=op; flow-context wired from edges")


if __name__ == "__main__":
    test_frame_stripped_distinct()
    test_meridian_llm_pack()
    test_support_agent_two_variants_and_flow()
    print("\nALL FACT-PACK INVARIANTS PASSED ($0, no network)")
