"""Deterministic spine of the downgrade audit — parse real LangSmith shape -> group -> graph_path -> rank.
No LLM, no network, $0. Run: python -m tests.test_downgrade_spine  (from repo root)."""
import json
import os
from collections import OrderedDict

from connectors.langsmith.adapter import LangSmithAdapter, _graph_path
from connectors.datasource import DataSource
from app.services import graph as gmod, downgrade
from app.catalog import next_cheaper, PRICE

_SAMPLE = os.path.join("connectors", "langsmith", "sample_runs.json")


# ── synthetic-catalog harness: unit-test the SELECTION LOGIC deterministically, independent of live prices ────
def _p(i, o):
    return {"input": i, "output": o, "cache_read": 0.0, "cache_write": 0.0, "cache_min": 1024}


def _o(tier, rel, sibs, retire_date=None):
    return {"provider": "syn", "siblings": sibs, "tier": tier, "release": rel,
            "max_output": None, "retire_date": retire_date}


def _install_synthetic(price, order):
    """Swap the module catalog for a synthetic one; returns restore(). Works with or without pytest. retire_date
    lives on the `order` entries (a model property). next_cheaper reads only these — migration paths are separate."""
    from app import catalog as cat
    saved = (cat.PRICE, cat._ORDER, cat._CALLABLE)
    cat.PRICE, cat._ORDER = price, order
    cat._CALLABLE = {k: True for k in price}

    def restore():
        cat.PRICE, cat._ORDER, cat._CALLABLE = saved
    return restore


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
    # Real Gemini prices (LiteLLM master). gemini-2.5-pro ($1.25/$10): the newest+cheapest live flash,
    # gemini-3.7-flash ($0.75/$3.75), is Pareto-cheaper on BOTH axes -> the gentlest drop.
    tgt = next_cheaper("gemini-2.5-pro")
    assert tgt == "gemini-3.7-flash", "2.5-pro -> newest Pareto-cheaper flash: got %s" % tgt
    assert PRICE[tgt]["input"] <= PRICE["gemini-2.5-pro"]["input"] and PRICE[tgt]["output"] <= PRICE["gemini-2.5-pro"]["output"]
    # gemini-3.1-pro -> gemini-3.5-flash (most-capable balanced still Pareto-cheaper); sonnet-5 -> haiku-4-5 unchanged.
    assert next_cheaper("gemini-3.1-pro") == "gemini-3.5-flash", next_cheaper("gemini-3.1-pro")
    assert next_cheaper("claude-sonnet-5") == "claude-haiku-4-5", next_cheaper("claude-sonnet-5")
    print("[ok] pareto ladder (real prices): 2.5-pro -> 3.7-flash; 3.1-pro -> 3.5-flash; sonnet-5 -> haiku-4-5")


def test_node_aware_pick():
    # The node-aware LOGIC on a CONTROLLED catalog (independent of live prices, which change): when the NEWEST
    # lower-tier model is pricier-input / cheaper-output, the pick flips with the node's mix. syn-pro ($1.0/$10):
    #   output-heavy -> syn-new ($1.5/$5): pricier input but far cheaper output -> net saves, and it's newest.
    #   input-heavy  -> syn-old ($0.5/$8): syn-new net-LOSES on all that input, so it falls to the cheaper-input one.
    price = {"syn-pro": _p(1.0, 10.0), "syn-new": _p(1.5, 5.0), "syn-old": _p(0.5, 8.0)}
    sibs = ["syn-pro", "syn-new", "syn-old"]
    order = {"syn-pro": _o("frontier", "2026-01", sibs),
             "syn-new": _o("balanced", "2026-06", sibs),
             "syn-old": _o("balanced", "2025-01", sibs)}
    restore = _install_synthetic(price, order)
    try:
        assert next_cheaper("syn-pro", 100, 2000) == "syn-new", "output-heavy -> newest, cheaper-output"
        assert next_cheaper("syn-pro", 5000, 50) == "syn-old", "input-heavy -> older, cheaper-input (new net-loses)"
        assert next_cheaper("syn-pro", 0, 0) is None, "no measurable tokens -> no unprovable saving"
    finally:
        restore()
    # real catalog: candidates() threads the node mix through to the latest live flash.
    heavy = [{"key": "a/deep", "node": "deep", "model": "gemini-2.5-pro",
              "avg_in": 200, "avg_out": 4000, "calls": 10, "graph_path": ""}]
    c = downgrade.candidates(heavy, per_calls=10000)[0]
    assert c["cheaper"] == "gemini-3.7-flash" and c["usd"] > 0, c
    print("[ok] node-aware (synthetic logic): mix flips new<->old; real candidates -> latest 3.7-flash")


def test_lifecycle_guard():
    # A candidate retiring within the horizon is NEVER a downgrade target. 'dying' is the most-capable balanced
    # drop, but it retires 2026-09-01 -> excluded near that date, allowed long before (proves it's the guard).
    price = {"big": _p(2.0, 10.0), "dying": _p(1.5, 8.0), "safe": _p(1.0, 6.0)}
    sibs = ["big", "dying", "safe"]
    order = {"big": _o("frontier", "2026-01", sibs),
             "dying": _o("balanced", "2026-06", sibs, retire_date="2026-09-01"),
             "safe": _o("balanced", "2025-06", sibs)}
    restore = _install_synthetic(price, order)
    try:
        assert next_cheaper("big", as_of="2026-08-01") == "safe", "retiring 'dying' excluded near shutdown"
        assert next_cheaper("big", as_of="2025-01-01") == "dying", "far before shutdown, 'dying' is allowed"
    finally:
        restore()
    print("[ok] lifecycle guard: a soon-retiring model is excluded as a downgrade target")


def test_migration_path():
    from app.migration import migration_target
    # cross-provider vendor recommendation (Google deck): premium -> 3.7-flash; lite/small -> 3.5-flash-lite.
    for m in ("claude-sonnet-5", "claude-opus-4-8", "gpt-5.5", "gemini-2.5-pro", "gemini-3.6-flash"):
        assert migration_target(m)["to"] == "gemini-3.7-flash", (m, migration_target(m))
    for m in ("claude-haiku-4-5", "gpt-5-mini", "gpt-5-nano", "gemini-2.5-flash", "gemini-2.5-flash-lite"):
        assert migration_target(m)["to"] == "gemini-3.5-flash-lite", (m, migration_target(m))
    assert migration_target("gemini-3.7-flash") is None, "a current target has no onward migration"
    assert migration_target("some-unknown-xyz") is None
    # preference: candidates() picks the migration target (cross-provider) over cost-downgrade, tagged 'migration'.
    node = [{"key": "a/x", "node": "svc", "model": "claude-sonnet-5",
             "avg_in": 2000, "avg_out": 300, "calls": 10, "graph_path": ""}]
    cand = downgrade.candidates(node, per_calls=10000)[0]
    assert cand["cheaper"] == "gemini-3.7-flash" and cand["reason"] == "migration" and cand["usd"] > 0, cand
    print("[ok] migration path: family regex -> Gemini GA targets; preferred over cost-downgrade")


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
    # migration path is PREFERRED: triage runs claude-sonnet -> Google's recommended target gemini-3.7-flash.
    assert c["cheaper"] == "gemini-3.7-flash" and c["reason"] == "migration" and c["usd"] > 0, c
    print("[ok] spine: triage %s -> %s (%s)  ~$%.2f/1k calls | responder correctly not a candidate"
          % (c["model"], c["cheaper"], c["reason"], c["usd"]))


if __name__ == "__main__":
    test_graph_path_parser()
    test_pareto_ladder()
    test_node_aware_pick()
    test_lifecycle_guard()
    test_migration_path()
    test_thinking_detection()
    test_same_label_distinct_keys()
    test_spine()
    print("\nALL DETERMINISTIC SPINE TESTS PASSED ($0, no network)")
