"""serving_name: canonical catalog id -> the CENTRAL gateway's own routing name (what we actually CALL).

The gateway is not ours to reconfigure, so the deployment names come FROM it (/model/info) and we invert the map.
Off a gateway (dev / Anthropic-direct) it's the identity, since the canonical name is already the real callable id.
This is the outbound mirror of resolve_deployed (which does the inbound half for pricing + availability)."""
import app.registry as registry


def _on_gateway(monkeypatch, deployment):
    monkeypatch.setattr(registry, "_gateway", lambda: ("http://gw.test", "k"))
    monkeypatch.setattr(registry, "_fetch_model_info", lambda base, key: deployment)
    registry.refresh()                                     # force _load to re-read our fake gateway on next use


def test_serving_name_maps_canonical_to_gateway_deployment(monkeypatch):
    # the central gateway serves gpt-oss-120b under a name we can't change
    _on_gateway(monkeypatch, [
        {"model_name": "bedrock-gpt-oss-12-b-1-0",
         "underlying": "bedrock/openai.gpt-oss-120b-v1:0",
         "model_info": {"mode": "chat"}},
    ])
    assert registry.is_available("gpt-oss-120b") is True                         # inbound: underlying -> canonical
    assert registry.serving_name("gpt-oss-120b") == "bedrock-gpt-oss-12-b-1-0"   # outbound: canonical -> call name
    registry.refresh()


def test_serving_name_identity_off_gateway(monkeypatch):
    monkeypatch.setattr(registry, "_gateway", lambda: (None, None))
    registry.refresh()
    assert registry.serving_name("gpt-oss-120b") == "gpt-oss-120b"               # no gateway -> send canonical unchanged
    registry.refresh()


def test_serving_name_unserved_falls_back_to_canonical(monkeypatch):
    # gateway is up but does NOT serve this model -> we never recommend it, and serving_name must not invent a name
    _on_gateway(monkeypatch, [
        {"model_name": "some-other-alias", "underlying": "bedrock/anthropic.claude-haiku-4-5",
         "model_info": {"mode": "chat"}},
    ])
    assert registry.is_available("gpt-oss-120b") is False
    assert registry.serving_name("gpt-oss-120b") == "gpt-oss-120b"
    registry.refresh()
