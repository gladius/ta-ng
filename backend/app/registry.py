"""Model registry: config/models.json for reference, the gateway's deployed set (GET /model/info, cached once) for what
can be called. Without a reachable gateway availability is unknown and nothing is gated. Never the public LiteLLM
catalog: it lists every model, not this gateway's.
"""
import json
import urllib.request
import urllib.error

import credentials
from app.catalog import (PRICE, resolve_deployed, canonical_model, canonical_exact,
                         is_callable, provider_of, tier, meta, max_output as _cat_max_output)

_cache = {}


class ModelNotServable(Exception):
    """The gateway neither serves this model nor knows it as a catalog id; raised instead of an opaque 404.
    Never raised off-gateway."""
    def __init__(self, model, served):
        self.model, self.served = model, served
        shown = ", ".join(served[:12]) + (" …" if len(served) > 12 else "")
        super().__init__("model %r is not served by the gateway and is not a known catalog id. Served: %s"
                         % (model, shown or "(none)"))


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
    if base:
        deployment = _fetch_model_info(base, key)
        source = ("gateway %s" % base) if deployment is not None else ("gateway %s — UNREACHABLE (not gating)" % base)
    served, flavours, gwmeta = set(), {}, {}
    if deployment is not None:
        for m in deployment:
            mi = m.get("model_info") or {}
            if mi.get("mode", "chat") != "chat":
                continue                                   # embeddings/others aren't downgrade targets
            c = resolve_deployed(m["underlying"])
            if c:
                served.add(c)
                flavours.setdefault(c, []).append(m["model_name"])
                # The gateway's own limits and mode beat models.json for this deployment.
                gwmeta.setdefault(c, {"context_window": mi.get("max_input_tokens"),
                                      "max_output": mi.get("max_output_tokens"),
                                      "mode": mi.get("mode")})
    serving = {c: _pick_flavour(c, names) for c, names in flavours.items()}
    names = {n for lst in flavours.values() for n in lst}
    _cache.update(loaded=True, deployment=deployment, source=source, available=served,
                  serving=serving, serving_names=names, flavours=flavours, gwmeta=gwmeta)


def refresh():
    """Re-fetch on next use."""
    _cache.clear()


def has_gateway():
    """True if a reachable gateway gives real availability; False when it is unknown."""
    _load()
    return _cache["deployment"] is not None


def is_available(model):
    """Whether the gateway serves this model; True when availability is unknown, so we never gate on missing data."""
    _load()
    if _cache["deployment"] is None:
        return True
    return (canonical_model(model) or model) in _cache["available"]


def _pick_flavour(canonical, names):
    """The plain canonical name if the gateway exposes it, else the shortest flavour (stable order)."""
    if canonical in names:
        return canonical
    return min(names, key=lambda n: (len(n), n))


def serving_name(model):
    """Catalog id -> the exact name to send to the gateway; the one outbound seam, so ids and wire names never mix.
    A known wire name passes through, which lets a specific flavour be forced."""
    _load()
    if model in _cache.get("serving_names", set()):
        return model
    serving = _cache.get("serving", {})
    hit = canonical_exact(model)
    if hit and hit in serving:
        return serving[hit]
    if _cache.get("deployment") is not None:
        raise ModelNotServable(model, sorted(_cache.get("serving_names", set())))
    return model                                             # no gateway: the catalog id is the real id


def gateway_map():
    """Per served catalog id, the chosen flavour and every advertised one, so a wrong pick shows. Empty off-gateway."""
    _load()
    serving, flavours = _cache.get("serving", {}), _cache.get("flavours", {})
    return [{"id": c, "chosen": serving[c], "flavours": sorted(flavours.get(c, []))} for c in sorted(serving)]


def _gwmeta(model, field):
    """One field of the gateway's metadata for `model`, or None; 0 / null / '' count as missing."""
    _load()
    c = canonical_model(model) or model
    return (_cache.get("gwmeta", {}).get(c) or {}).get(field) or None


def max_output(model):
    """Max output tokens: the gateway's max_output_tokens if it serves this model, else the catalog value."""
    v = _gwmeta(model, "max_output")
    return int(v) if v else _cat_max_output(model)


def context_window(model):
    """Context window: the gateway's max_input_tokens if it serves this model, else the catalog value."""
    v = _gwmeta(model, "context_window")
    return int(v) if v else (meta(model) or {}).get("context_window")


def report():
    """Models page view: every catalog model with its availability (None = unknown), plus uncatalogued deployments."""
    _load()
    dep, served = _cache["deployment"], _cache["available"]
    models = []
    for name in PRICE:
        info = meta(name)
        models.append({"model": name, "provider": provider_of(name), "tier": info.get("tier"),
                       "mode": _gwmeta(name, "mode") or info.get("mode", "chat"), "input": PRICE[name].get("input"),
                       "output": PRICE[name].get("output"), "context_window": context_window(name),
                       "retire_date": info.get("retire_date"), "callable": is_callable(name),
                       "open_weight": info.get("open_weight", False), "family": info.get("family"),
                       "host": info.get("host"), "confidence": info.get("confidence"),
                       "purpose": info.get("purpose"), "note": info.get("note"),
                       "available": (None if dep is None else (name in served))})
    uncatalogued = []
    if dep is not None:
        for m in dep:
            if (m.get("model_info") or {}).get("mode", "chat") == "chat" and not resolve_deployed(m["underlying"]):
                uncatalogued.append({"model_name": m["model_name"], "underlying": m["underlying"]})
    return {"source": _cache["source"], "has_gateway": dep is not None, "models": models,
            "uncatalogued": uncatalogued, "gateway_map": gateway_map()}
