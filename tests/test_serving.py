"""Gateway model-name resolution — the id (catalog identity) vs name (gateway deployment address) contract.

The app reasons in catalog ids everywhere; exactly one seam converts an id to the gateway's own deployment name, so
identity and address can't be mixed up. These tests pin: host-prefix + suffix resolution (bedrock-… and …-123 both),
no wrong collapse of lookalikes, deterministic flavour pick, idempotent pass-through of a wire name, and loud failure
on an unplaceable name when a gateway is configured."""
import pytest

import app.registry as registry
from app.catalog import resolve_deployed, canonical_exact


def _on_gateway(monkeypatch, deployment):
    monkeypatch.setattr(registry, "_gateway", lambda: ("http://gw.test", "k"))
    monkeypatch.setattr(registry, "_fetch_model_info", lambda base, key: deployment)
    registry.refresh()


def _dep(model_name, underlying):
    return {"model_name": model_name, "underlying": underlying, "model_info": {"mode": "chat"}}


# ── resolve_deployed: peel host prefix + version, match exactly or by anchored suffix, never reverse-guess ──
@pytest.mark.parametrize("underlying, expected", [
    ("bedrock/anthropic.claude-sonnet-5-v1:0", "claude-sonnet-5"),   # slash + dot namespace + version
    ("bedrock-claude-sonnet-5",                "claude-sonnet-5"),   # HYPHEN host prefix (used to break)
    ("bedrock-claude-sonnet-5-123",            "claude-sonnet-5"),   # host prefix + suffix flavour
    ("claude-sonnet-5-123",                    "claude-sonnet-5"),   # suffix flavour only (already worked)
    ("bedrock-gpt-oss-120b",                   "gpt-oss-120b"),      # open-weight, host prefix
    ("bedrock/openai.gpt-oss-120b-v1:0",       "gpt-oss-120b"),
    ("bedrock-gemma-3-27b",                    "gemma-3-27b"),
    ("gemma-3-1b",                             None),                # NOT in catalog -> must NOT collapse to gemma-3-12b
    ("gemma-3-12b",                            "gemma-3-12b"),
])
def test_resolve_deployed(underlying, expected):
    assert resolve_deployed(underlying) == expected


def test_canonical_exact_never_reverse_maps():
    # the reverse direction (a catalog id that starts with the input) is the unsafe one — it must stay None
    assert canonical_exact("gemma-3-1b") is None
    assert canonical_exact("claude-sonnet-5") == "claude-sonnet-5"
    assert canonical_exact("claude-sonnet-5-1234") == "claude-sonnet-5"     # anchored suffix is fine


# ── serving_name: the one outbound seam ──
def test_serving_name_maps_id_to_gateway_deployment(monkeypatch):
    _on_gateway(monkeypatch, [_dep("bedrock-claude-sonnet-5", "bedrock/anthropic.claude-sonnet-5-v1:0")])
    assert registry.is_available("claude-sonnet-5") is True
    assert registry.serving_name("claude-sonnet-5") == "bedrock-claude-sonnet-5"     # id -> gateway name
    registry.refresh()


def test_serving_name_passthrough_wire_name_is_idempotent(monkeypatch):
    _on_gateway(monkeypatch, [_dep("bedrock-claude-sonnet-5", "bedrock/anthropic.claude-sonnet-5-v1:0")])
    n = registry.serving_name("claude-sonnet-5")
    assert n == "bedrock-claude-sonnet-5"
    assert registry.serving_name(n) == n                     # feeding the wire name back is a no-op (idempotent)
    registry.refresh()


def test_serving_name_deterministic_flavour_pick(monkeypatch):
    # three flavours of the same model, no plain name -> the SHORTEST wins, regardless of /model/info order
    _on_gateway(monkeypatch, [
        _dep("claude-sonnet-5-1234", "us.anthropic.claude-sonnet-5-v1:0"),
        _dep("claude-sonnet-5-us",   "us.anthropic.claude-sonnet-5"),
        _dep("claude-sonnet-5-eu",   "eu.anthropic.claude-sonnet-5"),
    ])
    assert registry.serving_name("claude-sonnet-5") == "claude-sonnet-5-eu"   # shortest of the three, stable
    registry.refresh()


def test_serving_name_prefers_plain_name_when_present(monkeypatch):
    _on_gateway(monkeypatch, [
        _dep("bedrock-claude-sonnet-5", "bedrock/anthropic.claude-sonnet-5-v1:0"),
        _dep("claude-sonnet-5",         "anthropic.claude-sonnet-5"),
    ])
    assert registry.serving_name("claude-sonnet-5") == "claude-sonnet-5"      # plain canonical name preferred
    registry.refresh()


def test_serving_name_loud_when_gateway_up_but_unplaceable(monkeypatch):
    _on_gateway(monkeypatch, [_dep("bedrock-claude-sonnet-5", "bedrock/anthropic.claude-sonnet-5-v1:0")])
    with pytest.raises(registry.ModelNotServable):
        registry.serving_name("claude-5")                    # not a catalog id, not a wire name -> LOUD, not silent
    registry.refresh()


def test_serving_name_identity_off_gateway(monkeypatch):
    monkeypatch.setattr(registry, "_gateway", lambda: (None, None))
    registry.refresh()
    assert registry.serving_name("claude-sonnet-5") == "claude-sonnet-5"     # no gateway -> send canonical unchanged
    assert registry.serving_name("anything-goes") == "anything-goes"         # and never raises off-gateway
    registry.refresh()


def test_gateway_limits_take_precedence(monkeypatch):
    _on_gateway(monkeypatch, [{"model_name": "claude-sonnet-5", "underlying": "anthropic.claude-sonnet-5",
                               "model_info": {"mode": "chat", "max_input_tokens": 200000, "max_output_tokens": 64000}}])
    assert registry.max_output("claude-sonnet-5") == 64000          # gateway's ceiling, not the catalog's
    assert registry.context_window("claude-sonnet-5") == 200000
    registry.refresh()


def test_gateway_limits_fall_back_to_catalog_when_omitted(monkeypatch):
    from app import catalog
    _on_gateway(monkeypatch, [{"model_name": "claude-sonnet-5", "underlying": "anthropic.claude-sonnet-5",
                               "model_info": {"mode": "chat"}}])   # served, but no limits advertised
    assert registry.max_output("claude-sonnet-5") == catalog.max_output("claude-sonnet-5")
    registry.refresh()


def test_limits_off_gateway_are_catalog(monkeypatch):
    from app import catalog
    monkeypatch.setattr(registry, "_gateway", lambda: (None, None))
    registry.refresh()
    assert registry.max_output("claude-sonnet-5") == catalog.max_output("claude-sonnet-5")
    assert registry.context_window("claude-sonnet-5") == (catalog.meta("claude-sonnet-5") or {}).get("context_window")
    registry.refresh()


def test_gateway_map_surfaces_chosen_and_flavours(monkeypatch):
    _on_gateway(monkeypatch, [
        _dep("claude-sonnet-5-us", "us.anthropic.claude-sonnet-5"),
        _dep("claude-sonnet-5-eu", "eu.anthropic.claude-sonnet-5"),
    ])
    gm = {g["id"]: g for g in registry.gateway_map()}
    assert gm["claude-sonnet-5"]["chosen"] == "claude-sonnet-5-eu"
    assert gm["claude-sonnet-5"]["flavours"] == ["claude-sonnet-5-eu", "claude-sonnet-5-us"]
    registry.refresh()
