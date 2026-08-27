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
from app.catalog import (PRICE, resolve_deployed, canonical_model, canonical_exact,
                         is_callable, provider_of, tier, meta, max_output as _cat_max_output)

_cache = {}   # lazy one-shot: {'loaded', 'deployment' (list|None), 'source', 'available' (set), 'serving', ...}


class ModelNotServable(Exception):
    """Requested for a live call, but the configured gateway neither serves this model nor recognizes it as a catalog
    id — raised instead of silently sending a name the gateway can't route (which would 404 opaquely). Off-gateway this
    never fires (the canonical name IS the callable id). Set the model to a catalog id, or the exact deployment name."""
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
    if base:                                               # a gateway is configured -> availability is REAL
        deployment = _fetch_model_info(base, key)
        source = ("gateway %s" % base) if deployment is not None else ("gateway %s — UNREACHABLE (not gating)" % base)
    served, flavours, gwmeta = set(), {}, {}
    if deployment is not None:
        for m in deployment:
            mi = m.get("model_info") or {}
            if mi.get("mode", "chat") != "chat":
                continue                                   # embeddings/others aren't downgrade/cache targets
            c = resolve_deployed(m["underlying"])
            if c:
                served.add(c)
                flavours.setdefault(c, []).append(m["model_name"])   # every gateway flavour of this catalog model
                gwmeta.setdefault(c, {"context_window": mi.get("max_input_tokens"),   # the gateway's OWN limits/mode —
                                      "max_output": mi.get("max_output_tokens"),        # truth for THIS deployment, used
                                      "mode": mi.get("mode")})                          # over models.json; first wins
    serving = {c: _pick_flavour(c, names) for c, names in flavours.items()}     # ONE deterministic name we CALL / model
    names = {n for lst in flavours.values() for n in lst}                        # all wire names (idempotent passthrough)
    _cache.update(loaded=True, deployment=deployment, source=source, available=served,
                  serving=serving, serving_names=names, flavours=flavours, gwmeta=gwmeta)


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


def _pick_flavour(canonical, names):
    """Deterministic pick among a model's gateway deployment flavours (claude-sonnet-5-us / -1234 / bedrock-…): the
    plain canonical name if the gateway exposes it, else the SHORTEST name (lexicographic tiebreak) — the least-suffixed
    one, stable regardless of /model/info order. A specific flavour is always forceable by naming it exactly, since
    serving_name passes a known wire name straight through."""
    if canonical in names:
        return canonical
    return min(names, key=lambda n: (len(n), n))


def serving_name(model):
    """Canonical catalog id -> the exact name to SEND on the wire. THE single outbound seam; the app reasons in catalog
    ids everywhere and only this converts to a gateway deployment name, so identity and address can never be mixed up.

    Resolution order:
      1. already a known gateway wire name -> pass through unchanged (idempotent; lets a specific flavour be forced).
      2. resolves EXACTLY / by anchored suffix to a served catalog id -> the gateway's own deployment name for it.
      3. a gateway is configured but it's neither of the above -> raise ModelNotServable (LOUD, not a silent wrong send).
      4. no gateway (dev / Anthropic-direct) -> the name unchanged (canonical already IS the real callable id).
    canonical_exact never reverse-guesses, so a lookalike can't be silently mapped to the wrong model."""
    _load()
    if model in _cache.get("serving_names", set()):          # (1) already the gateway's own name -> send as-is
        return model
    serving = _cache.get("serving", {})
    hit = canonical_exact(model)                              # (2) longest catalog id in the name (boundary-delimited)
    if hit and hit in serving:
        return serving[hit]
    if _cache.get("deployment") is not None:                 # (3) gateway up but unplaceable -> fail loudly with options
        raise ModelNotServable(model, sorted(_cache.get("serving_names", set())))
    return model                                             # (4) no gateway -> canonical == the real id


def gateway_map():
    """The resolved gateway mapping, for one-glance verification (the /models page renders it). Per catalog id the app
    can call: the flavour we CHOSE and every flavour the gateway advertised — so a wrong pick or an unresolved name is
    caught by looking, not by trusting the resolver. Empty off-gateway."""
    _load()
    serving, flavours = _cache.get("serving", {}), _cache.get("flavours", {})
    return [{"id": c, "chosen": serving[c], "flavours": sorted(flavours.get(c, []))} for c in sorted(serving)]


def _gwmeta(model, field):
    """One field of the gateway's own metadata for `model` (its max limits / mode), or None when the gateway doesn't
    serve it or omits the field. 0 / null / '' count as omitted, so the models.json fallback kicks in."""
    _load()
    c = canonical_model(model) or model
    return (_cache.get("gwmeta", {}).get(c) or {}).get(field) or None


def max_output(model):
    """Max OUTPUT tokens: the GATEWAY's max_output_tokens if it serves this model (the truth for what the deployment
    actually allows), else the catalog value. A replay never asks for more than the served model can return."""
    v = _gwmeta(model, "max_output")
    return int(v) if v else _cat_max_output(model)


def context_window(model):
    """Context window: the gateway's max_input_tokens if it serves this model, else the catalog value."""
    v = _gwmeta(model, "context_window")
    return int(v) if v else (meta(model) or {}).get("context_window")


def report():
    """Consolidated view for the models page: every reference model with its availability (True/False, or None =
    unknown when there is no gateway), plus deployed-but-uncatalogued models. No fetch happens without a gateway."""
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
                       "available": (None if dep is None else (name in served))})   # None = unknown (no gateway)
    uncatalogued = []
    if dep is not None:
        for m in dep:
            if (m.get("model_info") or {}).get("mode", "chat") == "chat" and not resolve_deployed(m["underlying"]):
                uncatalogued.append({"model_name": m["model_name"], "underlying": m["underlying"]})
    return {"source": _cache["source"], "has_gateway": dep is not None, "models": models,
            "uncatalogued": uncatalogued, "gateway_map": gateway_map()}
