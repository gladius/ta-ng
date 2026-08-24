"""The CONSOLIDATED model registry: config/models.json (reference) × the gateway's DEPLOYED set (availability —
the source of truth for what can actually be called). Every lever gates its target on is_available(): recommending
a model the gateway does not serve is useless.

Availability comes ONLY from the live gateway, never fabricated:
  • gateway configured (LLM_GATEWAY_URL / *_BASE_URL set — i.e. prod) -> GET {base}/model/info ONCE, cached.
  • no gateway (dev / Anthropic-direct) -> availability is UNKNOWN: is_available() returns True for everything, so
    the downgrade lever behaves exactly as it does offline and the models page shows 'availability: unknown'.
No hardcoded deployment. NO public-catalog fallback — the public LiteLLM catalog is 'all models', NOT this
gateway's deployed set, so it must never decide availability. The gateway switch is the SAME LLM_GATEWAY_URL that
llm_client uses for transport, so 'are we on a gateway' has one answer for the whole app.
"""
import json
import urllib.request
import urllib.error

import credentials
from app.catalog import PRICE, resolve_deployed, canonical_model, is_callable, provider_of, tier, meta

_cache = {}   # lazy one-shot: {'loaded', 'deployment' (list|None), 'source', 'available' (set)}


def _gateway():
    credentials.load()
    base = credentials.get_config("LLM_GATEWAY_URL", None,
                                  aliases=("ANTHROPIC_BASE_URL", "LITELLM_BASE_URL", "LLM_BASE_URL"))
    key = credentials.get_secret("LLM_GATEWAY_KEY",
                                 aliases=("ANTHROPIC_API_KEY", "ANTHROPIC_KEY", "CLAUDE_API_KEY", "LITELLM_API_KEY"))
    return base, key


def _roots(base):
    b = base.rstrip("/")
    cands = [b] + [b[:-len(s)] for s in ("/anthropic", "/v1") if b.endswith(s)]
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c); out.append(c)
    return out


def _fetch_model_info(base, key):
    """Live GET {root}/model/info -> [{model_name, underlying, model_info}] or None (unreachable/unauthorized)."""
    for root in _roots(base):
        for path in ("/model/info", "/v1/model/info"):
            try:
                req = urllib.request.Request(root + path,
                                             headers={"Authorization": "Bearer %s" % (key or ""),
                                                      "x-api-key": key or "", "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read()).get("data") or []
                if data:
                    return [{"model_name": m.get("model_name"),
                             "underlying": (m.get("litellm_params") or {}).get("model") or m.get("model_name"),
                             "model_info": m.get("model_info") or {}} for m in data]
            except Exception:
                continue
    return None


def _load(force=False):
    if _cache.get("loaded") and not force:
        return
    base, key = _gateway()
    deployment, source = None, "unknown — no gateway configured (dev / Anthropic-direct)"
    if base:                                               # a gateway is configured -> availability is REAL
        deployment = _fetch_model_info(base, key)
        source = ("gateway %s" % base) if deployment is not None else ("gateway %s — UNREACHABLE (not gating)" % base)
    served = set()
    if deployment is not None:
        for m in deployment:
            if (m.get("model_info") or {}).get("mode", "chat") != "chat":
                continue                                   # embeddings/others aren't downgrade/cache targets
            c = resolve_deployed(m["underlying"])
            if c:
                served.add(c)
    _cache.update(loaded=True, deployment=deployment, source=source, available=served)


def refresh():
    """Re-fetch on next use (e.g. after config change)."""
    _cache.clear()


def has_gateway():
    """True if availability is REAL (a reachable gateway); False if UNKNOWN (dev / unreachable)."""
    _load()
    return _cache["deployment"] is not None


def is_available(model):
    """Can the gateway actually serve this model? UNKNOWN (no/unreachable gateway) -> True, so dev behaves exactly
    as offline and we never gate on data we don't have. With a gateway -> real deployed-set membership."""
    _load()
    if _cache["deployment"] is None:
        return True                                        # availability unknown -> do NOT gate
    return (canonical_model(model) or model) in _cache["available"]


def report():
    """Consolidated view for the models page: every reference model with its availability (True/False, or None =
    unknown when there is no gateway), plus deployed-but-uncatalogued models. No fetch happens without a gateway."""
    _load()
    dep, served = _cache["deployment"], _cache["available"]
    models = []
    for name in PRICE:
        info = meta(name)
        models.append({"model": name, "provider": provider_of(name), "tier": info.get("tier"),
                       "mode": info.get("mode", "chat"), "input": PRICE[name].get("input"),
                       "output": PRICE[name].get("output"), "context_window": info.get("context_window"),
                       "retire_date": info.get("retire_date"), "callable": is_callable(name),
                       "available": (None if dep is None else (name in served))})   # None = unknown (no gateway)
    uncatalogued = []
    if dep is not None:
        for m in dep:
            if (m.get("model_info") or {}).get("mode", "chat") == "chat" and not resolve_deployed(m["underlying"]):
                uncatalogued.append({"model_name": m["model_name"], "underlying": m["underlying"]})
    return {"source": _cache["source"], "has_gateway": dep is not None, "models": models,
            "uncatalogued": uncatalogued}
