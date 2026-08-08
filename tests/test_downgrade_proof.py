"""Proof-path logic — the DETERMINISTIC verdict math of the downgrade audit, no network ($0).

We stub the two paid calls (`replay`, `judge_preserved`). `replay` returns the MODEL NAME so the judge stub can
tell which model produced an answer (cheaper vs. the original re-runs of the self-variance baseline), and each side
gets its own preserve/drift pattern. Then we assert the two-stage verdict:

  Stage 1 (cheaper, N x K): cheaper PERFECT (K/K) -> SAFE, and the baseline never runs (cost-free on clean nodes).
  Stage 2 (self-variance, DOUBTFUL inputs only): re-run the ORIGINAL to get its own noise floor, judge cheaper
                                                 RELATIVE to it. recorded output = the free +1 anchor.
    - cheaper NEVER matched (0/K)                   -> NOT-SAFE  (low is low — no evidence, whatever the original does)
    - cheaper as steady as / steadier than original -> SAFE      (even vs a noisy original: 2/3 vs 1/3 is SAFE)
    - cheaper less steady than the original         -> NOT-SAFE
  Per-input is strictly BINARY (SAFE / NOT-SAFE); BORDERLINE is a NODE rollup (some inputs drifted but most held).
  Plus the fallback (baseline OFF -> flat AUDIT_SAFE_RATIO), low-evidence abstain, and the behavior renderer.

Run: python -m tests.test_downgrade_proof   (from repo root)
"""
import json
from contextlib import contextmanager

from auditor.util import canonical_model, next_cheaper
from app.services import audit

ORIG = canonical_model("claude-sonnet-5") or "claude-sonnet-5"
CHEAP = next_cheaper(ORIG)                    # claude-haiku-4-5


def _trace(user, model="claude-sonnet-5"):
    return {"model": model,
            "input_messages": [{"role": "system", "content": "route the ticket"},
                               {"role": "user", "content": user}],
            "tools_defined": [], "output": "ORIGINAL", "usage": {"input_tokens": 700, "output_tokens": 5}}


_BUCKET = [_trace("charge on order 123 double billed"), _trace("cannot login sso okta redirect loop"),
           _trace("package damaged monitor cracked screen"), _trace("downgrade my team plan to pro please"),
           _trace("refund for subscription renewal i cancelled")]


def _every(mod):
    """Stateful predicate: preserve EVERY call EXCEPT every `mod`-th one (which drifts). With max_parallel=1 the
    tasks run in per-input order, so _every(k) drifts exactly once per k-run input."""
    st = {"n": 0}
    def f():
        st["n"] += 1
        return (st["n"] % mod) != 0
    return f


_ALWAYS = lambda: True
_NEVER = lambda: False


@contextmanager
def _patch(cheap_pred, self_pred, baseline=True):
    """Stub replay -> model name, and judge -> per-side predicate. Sequential (max_parallel=1) for determinism."""
    saved = (audit.replay, audit.judge_preserved, audit.AUDIT_MAX_PARALLEL, audit.AUDIT_SELF_BASELINE)
    audit.replay = lambda t, m, max_tokens=None: m
    audit.judge_preserved = lambda req, a, b, model=None: ((self_pred if b == ORIG else cheap_pred)(), "why")
    audit.AUDIT_MAX_PARALLEL = 1
    audit.AUDIT_SELF_BASELINE = baseline
    try:
        yield
    finally:
        audit.replay, audit.judge_preserved, audit.AUDIT_MAX_PARALLEL, audit.AUDIT_SELF_BASELINE = saved


def test_tool_schema_both_formats():
    # Replay must bind the tool's REAL parameter schema whether the trace stored it Anthropic-style (input_schema)
    # or OpenAI/LangChain-style (parameters). If we only read input_schema, an OpenAI-format tool binds EMPTY and
    # the cheaper model can't reproduce the recorded call -> false 'wrong tool/args' drift on production traces.
    oai = json.dumps({"name": "issue_refund", "description": "refund",
                      "parameters": {"type": "object", "properties": {"id": {"type": "string"},
                                                                      "amount": {"type": "number"}}, "required": ["id"]}})
    anth = json.dumps({"name": "set_category", "description": "triage",
                       "input_schema": {"type": "object", "properties": {"category": {"type": "string"}}}})
    out = audit._tools({"tools_defined": [["issue_refund", oai], ["set_category", anth]]})
    by = {t["name"]: t for t in out}
    assert set(by["issue_refund"]["schema"]["properties"]) == {"id", "amount"}, "OpenAI `parameters` must bind"
    assert set(by["set_category"]["schema"]["properties"]) == {"category"}, "Anthropic `input_schema` still works"
    # neither key present -> safe empty fallback (no crash)
    empty = audit._tools({"tools_defined": [["x", json.dumps({"name": "x"})]]})
    assert empty[0]["schema"] == {"type": "object", "properties": {}}
    print("[ok] tool schema binds from BOTH input_schema (Anthropic) and parameters (OpenAI/LangChain)")


def test_tool_choice_conversion():
    tc = audit._tool_choice          # -> NEUTRAL form (llm_client translates to the provider)
    assert tc("required") == "any" and tc({"type": "required"}) == "any"
    assert tc("auto") is None and tc(None) is None and tc("none") is None
    assert tc({"type": "function", "function": {"name": "issue_refund"}}) == {"tool": "issue_refund"}
    assert tc({"type": "tool", "name": "x"}) == {"tool": "x"}
    assert tc("issue_refund") == {"tool": "issue_refund"}                           # bare tool-name form
    # the provider boundary maps neutral -> Anthropic
    assert audit.llm_client._anthropic_tool_choice("any") == {"type": "any"}
    assert audit.llm_client._anthropic_tool_choice({"tool": "x"}) == {"type": "tool", "name": "x"}
    print("[ok] tool_choice -> neutral (auto/none -> don't force); llm_client maps neutral -> Anthropic")


def test_replay_forwards_tools_and_choice():
    # End-to-end wiring: replay must bind the tool with its REAL schema AND forward a forced tool_choice, so a
    # node that forced a tool reproduces the call instead of drifting. Stub the client to capture the kwargs.
    captured = {}
    class _R:
        content = []
    orig = audit.llm_client.complete
    audit.llm_client.complete = lambda **kw: (captured.update(kw), _R())[1]
    try:
        tr = {"model": "claude-sonnet-5", "input_messages": [{"role": "user", "content": "refund order 1"}],
              "tools_defined": [["issue_refund", json.dumps({"name": "issue_refund",
                    "parameters": {"type": "object", "properties": {"id": {"type": "string"}}}})]],
              "tool_choice": "required", "usage": {"output_tokens": 5}}
        audit.replay(tr, "claude-haiku-4-5")
    finally:
        audit.llm_client.complete = orig
    assert captured.get("tool_choice") == {"type": "any"}, captured.get("tool_choice")
    assert captured.get("tools") and set(captured["tools"][0]["input_schema"]["properties"]) == {"id"}, "real schema bound"
    print("[ok] replay binds real tool schema + forwards forced tool_choice")


def test_render():
    r = audit._render("Looking it up.", [{"name": "set_category", "args": {"category": "billing"}}])
    assert "Looking it up." in r and "set_category" in r and '"category": "billing"' in r
    assert audit._render("", []) == "(empty output)"
    print("[ok] behavior renderer folds tool call into output")


def test_safe_no_baseline():
    # cheaper is PERFECT on every input -> SAFE, and the baseline must NOT run (self_kept stays None).
    with _patch(_ALWAYS, _NEVER):        # self_pred would fail if ever consulted; it must not be
        r = audit.audit_node("node", _BUCKET, n=5, k=3)
    assert r["verdict"] == "SAFE", r["verdict"]
    assert r["safe_inputs"] == 5 and all(i["kept"] == 3 and i["self_kept"] is None for i in r["inputs"])
    print("[ok] cheaper perfect -> SAFE, baseline skipped (no extra cost)")


def test_not_safe():
    # cheaper always drifts, original is rock-steady -> clearly worse than the noise floor -> NOT-SAFE.
    with _patch(_NEVER, _ALWAYS):
        r = audit.audit_node("node", _BUCKET, n=5, k=3)
    assert r["verdict"] == "NOT-SAFE", r["verdict"]
    bad = r["inputs"][0]
    assert bad["kept"] == 0 and bad["self_kept"] == 3, bad
    print("[ok] cheaper 0/3 vs original 3/3 -> NOT-SAFE")


def test_deterministic_one_worse_is_not_safe():
    # THE no-false-SAFE fix: original is deterministic (3/3), cheaper drifts once (2/3). The flat 0.66 rule would call
    # 2/3 SAFE; relative to a model that NEVER drifts the cheaper IS less consistent -> per-input NOT-SAFE (binary),
    # and with every input like that, drift is the majority -> node NOT-SAFE.
    with _patch(_every(3), _ALWAYS):
        r = audit.audit_node("node", _BUCKET, n=5, k=3)
    assert r["verdict"] == "NOT-SAFE", r["verdict"]
    i0 = r["inputs"][0]
    assert i0["kept"] == 2 and i0["self_kept"] == 3 and i0["verdict"] == "NOT-SAFE", i0
    print("[ok] original 3/3 vs cheaper 2/3 -> NOT-SAFE (binary per-input; less steady than a deterministic original)")


def test_matches_noise_is_safe():
    # cheaper drifts once (2/3), but the original is ITSELF noisy and also lands 2/3 -> cheaper is no worse than the
    # model's own noise -> SAFE. (self: recorded +1, then _every(2) drifts one of the 2 re-runs -> 1+1 = 2/3.)
    with _patch(_every(3), _every(2)):
        r = audit.audit_node("node", _BUCKET, n=5, k=3)
    assert r["verdict"] == "SAFE", r["verdict"]
    i0 = r["inputs"][0]
    assert i0["kept"] == 2 and i0["self_kept"] == 2 and i0["verdict"] == "SAFE", i0
    print("[ok] cheaper 2/3 vs equally-noisy original 2/3 -> SAFE (drift was noise, not the downgrade)")


def test_cheaper_steadier_than_noisy_original_is_safe():
    # original is very noisy (reproduces its own recorded output only 1/3); the cheaper reproduces it MORE (2/3) ->
    # the cheaper is no worse than the model's own noise, so SAFE. Binary: no "can't verify" state, no floor.
    with _patch(_every(3), _NEVER):      # cheaper 2/3 ; self: recorded +1, both re-runs drift -> self_kept = 1
        r = audit.audit_node("node", _BUCKET, n=5, k=3)
    assert r["verdict"] == "SAFE", r["verdict"]
    i0 = r["inputs"][0]
    assert i0["kept"] == 2 and i0["self_kept"] == 1 and i0["verdict"] == "SAFE", i0
    print("[ok] cheaper 2/3 vs noisy original 1/3 -> SAFE (cheaper is steadier than the original)")


def test_mixed_node_mostly_safe_is_borderline():
    # THE new node rule: 3 inputs safe (cheaper perfect) + 2 not-safe (cheaper drifts vs a deterministic original).
    # Drift is the MINORITY, so the node is BORDERLINE ("mostly safe, your call"), never a hard NOT-SAFE.
    st = {"n": 0}
    def cheap():                          # calls 1-9 = inputs 0,1,2 preserve (3/3); 10-15 = inputs 3,4 drift (0/3)
        st["n"] += 1
        return st["n"] <= 9
    with _patch(cheap, _ALWAYS):          # baseline runs only on the 2 doubtful inputs; original deterministic (3/3)
        r = audit.audit_node("node", _BUCKET, n=5, k=3)
    assert r["verdict"] == "BORDERLINE", r["verdict"]
    assert r["safe_inputs"] == 3, r["safe_inputs"]
    assert sorted(i["verdict"] for i in r["inputs"]) == ["NOT-SAFE", "NOT-SAFE", "SAFE", "SAFE", "SAFE"]
    print("[ok] 3 safe + 2 not-safe -> node BORDERLINE (mostly safe, minority drift)")


def test_fallback_ratio_when_baseline_off():
    # baseline disabled -> flat AUDIT_SAFE_RATIO rule: 2/3 >= 0.66 -> SAFE, and no self_kept is ever computed.
    with _patch(_every(3), _NEVER, baseline=False):
        r = audit.audit_node("node", _BUCKET, n=5, k=3)
    assert r["verdict"] == "SAFE", r["verdict"]
    assert all(i["self_kept"] is None for i in r["inputs"]), "baseline off -> no self-variance calls"
    print("[ok] baseline OFF -> falls back to flat 0.66 ratio (2/3 -> SAFE)")


def test_low_evidence():
    calls = {"n": 0}
    with _patch(_ALWAYS, _ALWAYS):
        audit.replay = lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), "x")[1]
        r = audit.audit_node("node", [_trace("only one ticket")], n=5, k=3)   # 1 distinct < MIN_EVIDENCE
    assert r["verdict"] == "LOW-EVIDENCE", r["verdict"]
    assert calls["n"] == 0, "abstain must NOT spend any replay"
    print("[ok] < min evidence -> LOW-EVIDENCE, zero spend")


if __name__ == "__main__":
    test_tool_schema_both_formats()
    test_tool_choice_conversion()
    test_replay_forwards_tools_and_choice()
    test_render()
    test_safe_no_baseline()
    test_not_safe()
    test_deterministic_tightens_to_borderline()
    test_matches_noise_is_safe()
    test_noisy_anchor_is_borderline()
    test_fallback_ratio_when_baseline_off()
    test_low_evidence()
    print("\nALL PROOF-PATH TESTS PASSED ($0, no network)")
