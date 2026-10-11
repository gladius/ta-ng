"""Provider prompt-cache rules the module reasons with. Prices are the catalog's ($/M tokens); these are the
provider mechanics: the smallest prefix that caches, how a prefix gets marked, the lifetimes on offer and what each
costs on a write. Verify against the provider's current docs when they change; the what-ifs cite them.

  read      cached-token price as a share of the input price
  write     per lifetime: {ttl seconds: write price as a share of the input price}; 1.0 = no surcharge
  explicit  the request must mark the prefix (Claude's cache_control) for anything to cache
"""
PROVIDERS = {
    "anthropic": {"min": 1024, "read": 0.1, "write": {300: 1.25, 3600: 2.0}, "explicit": True,
                  "note": "cache_control on the last fixed block; Haiku needs 2048+ (4.5: 4096) tokens"},
    "openai":    {"min": 1024, "read": 0.1, "write": {300: 1.0, 86400: 1.0}, "explicit": False,
                  "note": "automatic; prompt_cache_retention='24h' keeps the prefix a day at no extra cost"},
    "google":    {"min": 1024, "read": 0.25, "write": {300: 1.0}, "explicit": False,
                  "note": "implicit caching on 2.5+ (no write cost, lifetime not settable); explicit CachedContent "
                          "would add hourly storage, not modelled"},
}
DEFAULT = {"min": 1024, "read": 0.1, "write": {300: 1.0}, "explicit": False, "note": "unknown provider"}


def for_model(model):
    """(provider name, rules, cache_min for this model)."""
    from app import catalog
    prov = catalog.provider_of(model) if model else None
    rules = PROVIDERS.get(prov, DEFAULT)
    try:
        cmin = catalog.cache_min(model) if model else rules["min"]
    except Exception:
        cmin = rules["min"]
    return prov or "unknown", rules, max(cmin or 0, rules["min"])
