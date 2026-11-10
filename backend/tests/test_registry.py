"""The consolidated registry: reference (models.json) x gateway availability (/model/info). Availability is REAL
only with a gateway; UNKNOWN (dev) -> is_available True so nothing is gated on data we don't have. $0, no network —
the gateway + fetch are monkeypatched with a sample /model/info payload (the SAME shape the prod gateway returns)."""
from app import registry

# a representative GET /model/info deployment (bedrock-Claude, vertex-Gemini, an open-weight, an embedding)
_SAMPLE = [
    {"model_name": "bedrock-claude-sonnet-5", "underlying": "bedrock/us.anthropic.claude-sonnet-5", "model_info": {"mode": "chat"}},
    {"model_name": "gem37",  "underlying": "vertex_ai/gemini-3.7-flash", "model_info": {"mode": "chat"}},
    {"model_name": "llama",  "underlying": "groq/llama-3.3-70b-versatile", "model_info": {"mode": "chat"}},   # uncatalogued
    {"model_name": "embed",  "underlying": "openai/text-embedding-3-small", "model_info": {"mode": "embedding"}},  # non-chat
]


def test_availability_with_gateway(monkeypatch):
    monkeypatch.setattr(registry, "_gateway", lambda: ("https://gw.example", "k"))
    monkeypatch.setattr(registry, "_fetch_model_info", lambda b, k: _SAMPLE)
    registry.refresh()
    try:
        assert registry.has_gateway() is True
        # served, resolved across bedrock/vertex shapes:
        assert registry.is_available("claude-sonnet-5") is True
        assert registry.is_available("gemini-3.7-flash") is True
        # NOT in the deployment -> gated out (this is the whole point):
        assert registry.is_available("gemini-3.5-flash-lite") is False
        assert registry.is_available("claude-opus-4-8") is False
        rep = registry.report()
        assert rep["has_gateway"] is True
        assert any(u["underlying"].startswith("groq/") for u in rep["uncatalogued"]), rep["uncatalogued"]
        by = {r["model"]: r for r in rep["models"]}
        assert by["gemini-3.7-flash"]["available"] is True and by["gemini-3.5-flash-lite"]["available"] is False
    finally:
        registry.refresh()   # clear cache so later tests re-read the real (dev, no-gateway) env


def test_unknown_without_gateway(monkeypatch):
    monkeypatch.setattr(registry, "_gateway", lambda: (None, None))
    registry.refresh()
    try:
        assert registry.has_gateway() is False
        # UNKNOWN -> nothing is gated (returns True), dev behaves exactly as offline:
        assert registry.is_available("gemini-3.5-flash-lite") is True
        assert registry.is_available("literally-anything") is True
        rep = registry.report()
        assert rep["has_gateway"] is False and all(r["available"] is None for r in rep["models"])
    finally:
        registry.refresh()


def test_served_models_are_priced_by_the_gateway(monkeypatch):
    """The gateway's /model/info prices ($ per token) win for what it serves; the rest keep config/models.json's;
    refresh() puts the JSON back."""
    from app.catalog import PRICE
    from validation.pricing import cost_of
    json_sonnet, json_gem = dict(PRICE["claude-sonnet-5"]), dict(PRICE["gemini-3.7-flash"])
    sample = [
        {"model_name": "bedrock-claude-sonnet-5", "underlying": "bedrock/us.anthropic.claude-sonnet-5",
         "model_info": {"mode": "chat", "input_cost_per_token": 1.1e-06, "output_cost_per_token": 7e-06,
                        "cache_read_input_token_cost": 1.1e-07}},
        {"model_name": "gem37", "underlying": "vertex_ai/gemini-3.7-flash", "model_info": {"mode": "chat"}},  # no price
        {"model_name": "house-model", "underlying": "hosted_vllm/house-model",
         "model_info": {"mode": "chat", "input_cost_per_token": 5e-07, "output_cost_per_token": 1e-06}},
        {"model_name": "jev-router", "underlying": "auto_router/complexity_router", "model_info": {}},
    ]
    monkeypatch.setattr(registry, "_gateway", lambda: ("https://gw.example", "k"))
    monkeypatch.setattr(registry, "_fetch_model_info", lambda b, k: sample)
    registry.refresh()
    try:
        registry.load()
        assert PRICE["claude-sonnet-5"] == dict(json_sonnet, input=1.1, output=7.0, cache_read=0.11)   # cache write: JSON's
        assert PRICE["gemini-3.7-flash"] == json_gem                       # the gateway gives no price: JSON's
        assert cost_of("house-model", {"input": 1_000_000, "output": 1_000_000}) == 1.5   # not in the catalog, still priced
        assert "jev-router" not in PRICE
        by = {r["model"]: r for r in registry.report()["models"]}
        assert by["claude-sonnet-5"]["price_from"] == "gateway" and by["gemini-3.7-flash"]["price_from"] == "catalog"
    finally:
        registry.refresh()
    assert PRICE["claude-sonnet-5"] == json_sonnet and "house-model" not in PRICE
