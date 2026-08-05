"""Shared helpers: the model/price catalog + token estimation.

Pricing lives in auditor/models.json (providers -> models in capability order, $/1M tokens).
Add or update models THERE, not here. This module loads it and exposes:

  PRICE                 {model: {input, output, cache_read, cache_write, cache_min}}   flat lookup
  canonical_model(raw)  map a real/provider model name onto a catalog name (or None)
  next_cheaper(model)   the gentlest downgrade target — most-capable model one TIER below (same provider), or None
  provider_of(model)    the provider name for a catalog model (or None)
  tier(model)           capability class ('frontier'|'balanced'|'small'|'nano') for the ladder + UI label
  release(model)        approximate launch (YYYY-MM) — recency signal for the UI
  cache_min(model)      the provider's smallest cacheable prefix (tokens) for this model
  cache_mode(model)     'explicit' (caching is opt-in — 'enable' is a real fix) or 'auto' (provider caches
                        automatically — 'enable' is NOT a fix). A per-PROVIDER property in the catalog.
"""

import os
import re
import json

_CATALOG_PATH = os.path.join(os.path.dirname(__file__), "models.json")


def _load_catalog(path=_CATALOG_PATH):
    with open(path, encoding="utf-8") as f:
        cat = json.load(f)
    price, order, callable_, cmode = {}, {}, {}, {}
    aliases = {str(k).lower(): v for k, v in cat.get("aliases", {}).items()}
    for prov in cat.get("providers", []):
        names = [m["name"] for m in prov["models"]]
        mode = prov.get("cache_mode", "auto")                  # per-provider: 'explicit' | 'auto'
        for m in prov["models"]:
            price[m["name"]] = {"input": m["input"],
                                "output": m["output"],
                                "cache_read": m.get("cache_read", round(m["input"] * 0.1, 4)),
                                "cache_write": m.get("cache_write", round(m["input"] * 1.25, 4)),
                                "cache_min": m.get("cache_min", 1024)}
            callable_[m["name"]] = bool(m.get("callable", True))   # retired/pricing-only ids set "callable": false
            cmode[m["name"]] = mode
        for m in prov["models"]:
            order[m["name"]] = {"provider": prov["provider"], "siblings": names,
                                "tier": m.get("tier"), "release": m.get("release"),
                                "max_output": m.get("max_output")}   # per-model output ceiling (optional)
    return cat, price, order, aliases, callable_, cmode


_, PRICE, _ORDER, _ALIASES, _CALLABLE, _CMODE = _load_catalog()

DEFAULT_MODEL = "gemini-2.5-pro"       # pricing fallback for an un-catalogued model (identity is preserved)


def canonical_model(raw):
    """Map a raw/provider model string onto a catalog model name, or None if unknown.

    Handles exact names, explicit aliases, and dated/versioned variants
    (e.g. 'claude-sonnet-4-5-20250101' -> 'claude-sonnet-4-5', 'gpt-4o-2024-08-06' -> 'gpt-4o').
    """
    if not raw:
        return None
    r = str(raw).strip()
    if r in PRICE:
        return r
    rl = r.lower()
    if rl in _ALIASES:
        return _ALIASES[rl]
    base = re.sub(r"[-_/:](\d{6,8}|v\d+|latest|preview|exp)$", "", rl)
    if base in _ALIASES:                       # a dated/versioned form of an aliased id (e.g. sonnet-4-5-<date>)
        return _ALIASES[base]
    for name in PRICE:
        nl = name.lower()
        if rl == nl or nl == base or rl.startswith(nl) or base.startswith(nl) or nl.startswith(base):
            return name
    return None


def is_callable(model):
    """Is this catalog model actually callable on its provider's API? Retired / pricing-only ids (kept for the price
    ladder) are flagged `callable: false` in models.json — the ONE central place that knows this."""
    return _CALLABLE.get(model, True)


_TIER_RANK = {"frontier": 0, "balanced": 1, "small": 2, "nano": 3}     # capability classes, most → least capable


def next_cheaper(model):
    """The gentlest downgrade target that is ACTUALLY cheaper: the most-capable callable model in the nearest
    lower capability TIER whose price is <= this model's on BOTH input AND output (a real drop on every axis),
    same provider (or None).

    Why the both-axes test: pricing is NOT monotonic with tier — a newer premium 'flash' can cost MORE per input
    token than an older, soon-retiring 'pro' (e.g. gemini-3.6-flash $1.50-in vs gemini-2.5-pro $1.25-in). Picking
    'most capable in the next tier' alone then lands on a target that's pricier on input, and an input-heavy node
    shows a near-zero saving. Requiring Pareto-cheaper skips those and picks the most capable model that genuinely
    costs less (gemini-2.5-pro -> gemini-3-flash, not 3.6-flash). Falls back to legacy file order for an untier'd
    model, and keeps a candidate whose price is unknown (can't test it, don't silently drop it)."""
    m = canonical_model(model) or model
    info = _ORDER.get(m)
    if not info:
        return None
    cur = _TIER_RANK.get(info.get("tier"))
    if cur is None:                                                    # untier'd model → legacy next-in-list
        sibs = info["siblings"]
        for j in range(sibs.index(m) + 1, len(sibs)):
            if _CALLABLE.get(sibs[j], True):
                return sibs[j]
        return None
    p0 = PRICE.get(m, {})
    in0, out0 = p0.get("input"), p0.get("output")
    cands = []
    for s in info["siblings"]:
        r = _TIER_RANK.get(_ORDER[s].get("tier"), 99)
        if not (_CALLABLE.get(s, True) and r > cur):                  # callable + strictly lower capability tier
            continue
        ps = PRICE.get(s, {})
        si, so = ps.get("input"), ps.get("output")
        if None not in (in0, out0, si, so) and not (si <= in0 and so <= out0 and (si < in0 or so < out0)):
            continue                                                  # not cheaper on both axes -> illusory drop, skip
        cands.append((s, r, ps.get("input", 0)))
    if not cands:
        return None
    nearest = min(r for _, r, _ in cands)                             # nearest lower tier that HAS a real-drop model
    return max(((s, p) for s, r, p in cands if r == nearest), key=lambda x: x[1])[0]   # most capable in it


def provider_of(model):
    """The provider name for a catalog model (or None if unknown)."""
    info = _ORDER.get(canonical_model(model) or model)
    return info["provider"] if info else None


def tier(model):
    """Capability class from the catalog: 'frontier' | 'balanced' | 'small' | 'nano' (or None). The downgrade
    ladder + the report's tier-transition label read this."""
    info = _ORDER.get(canonical_model(model) or model)
    return info.get("tier") if info else None


def release(model):
    """Approximate launch (YYYY-MM) from the catalog, or None. Recency signal for the UI — verify before quoting."""
    info = _ORDER.get(canonical_model(model) or model)
    return info.get("release") if info else None


DEFAULT_MAX_OUTPUT = 32768        # fallback ceiling when a model has no `max_output` in the catalog


def max_output(model):
    """The model's max OUTPUT tokens, from the catalog `max_output` (optional per model), else DEFAULT_MAX_OUTPUT.
    Used to bound a replay's budget by the model's real capability instead of a hardcoded number — so a long
    production output is never truncated, and we never ask a model for more than it can return."""
    info = _ORDER.get(canonical_model(model) or model)
    mo = info.get("max_output") if info else None
    return int(mo) if mo else DEFAULT_MAX_OUTPUT


def cache_min(model):
    """The provider's smallest cacheable prefix (tokens) for this model, from the catalog."""
    return PRICE.get(canonical_model(model) or model, {}).get("cache_min", 1024)


def cache_mode(model):
    """'explicit' (caching is opt-in — 'enable caching' is a real fix we can prove) or 'auto' (the provider caches
    eligible prefixes automatically — 'enable' is NOT a fix). Unknown models default to 'auto' so we NEVER advise
    adding a breakpoint unless the catalog says the provider needs one."""
    return _CMODE.get(canonical_model(model) or model, "auto")


def approx_tokens(text):
    """Rough token estimate (~4 chars/token). Replace with a real tokenizer."""
    return max(1, len(str(text)) // 4)


def usd(tokens, price_per_million):
    return tokens / 1_000_000 * price_per_million


def normalize(t):
    """Load-time trace normalization — EXPLICIT, never silent. An un-catalogued model is priced at
    DEFAULT_MODEL so detection never crashes, but its real identity is preserved (`model_raw`) and the trace
    is flagged `model_known=False` — a cost tool must never quietly relabel a call-site's model. Missing token
    counts are flagged `usage_assumed` and left at ZERO rather than invented, so no $ figure ever rests on a
    fabricated count. Downstream (model-tier-downgrade, the UI) honours these flags. Used by web/lsource as
    traces stream in."""
    raw = t.get("model")
    canon = canonical_model(raw)
    if canon is None:                                      # unknown model: price at a default, but say so
        t.setdefault("model_raw", raw)
        t["model_known"] = False
        t["model_priced_as"] = DEFAULT_MODEL
        t["model"] = DEFAULT_MODEL
    else:
        t.setdefault("model_raw", raw)
        t.setdefault("model_known", True)
        t["model"] = canon                                # canonicalize (dated -> catalog id) so pricing is right
    u = t.get("usage")
    if not isinstance(u, dict) or "input_tokens" not in u:  # unknown volume: claim nothing, don't invent it
        t["usage"] = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0}
        t["usage_assumed"] = True
    t.setdefault("task_type", "unknown")
    return t
