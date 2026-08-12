"""Shared audit machinery — the provider-neutral pieces the profiled downgrade flow relies on ($0, no network).

The downgrade VERDICT math lives in tests/test_downgrade_profiled.py (it tests app/services/profile_downgrade.py).
This file covers what `replay` depends on and reuses: tool-schema binding (both Anthropic and OpenAI shapes),
tool_choice translation, replay wiring, and the behaviour renderer.

Run: python -m tests.test_downgrade_proof   (from repo root)
"""
import json

from app.services import audit


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


if __name__ == "__main__":
    test_tool_schema_both_formats()
    test_tool_choice_conversion()
    test_replay_forwards_tools_and_choice()
    test_render()
    print("\nALL SHARED-MACHINERY TESTS PASSED ($0, no network)")
