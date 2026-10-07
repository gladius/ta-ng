"""Dollar cost of one model call: measured tokens x the catalog price."""


def cost_of(model, usage):
    """`usage` is llm_client.usage's shape {input, output, cache_read, cache_write}. None when the model has no price."""
    from app.catalog import PRICE, canonical_model
    p = PRICE.get(canonical_model(model) or model or "")
    if not p or not usage:
        return None
    return round((usage.get("input", 0) * p["input"] + usage.get("output", 0) * p["output"]
                  + usage.get("cache_read", 0) * p.get("cache_read", p["input"] * 0.1)
                  + usage.get("cache_write", 0) * (p.get("cache_write") or p["input"] * 1.25)) / 1e6, 6)
