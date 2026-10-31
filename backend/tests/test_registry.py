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
