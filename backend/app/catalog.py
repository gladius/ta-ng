"""Model/price catalog and token estimation, loaded from config/models.json ($ per 1M tokens).

Edit models in that file, not here. Providers list models in capability order.
"""

import os
import re
import json
import datetime

_CATALOG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "models.json")


def _load_catalog(path=_CATALOG_PATH):
    with open(path, encoding="utf-8") as f:
        cat = json.load(f)
    price, order, callable_, cmode = {}, {}, {}, {}
    aliases = {str(k).lower(): v for k, v in cat.get("aliases", {}).items()}
    for prov in cat.get("providers", []):
        names = [m["name"] for m in prov["models"]]
        mode = prov.get("cache_mode", "auto")
        for m in prov["models"]:
            price[m["name"]] = {"input": m["input"],
                                "output": m["output"],
                                "cache_read": m.get("cache_read", round(m["input"] * 0.1, 4)),
                                "cache_write": m.get("cache_write", round(m["input"] * 1.25, 4)),
                                "cache_min": m.get("cache_min", 1024)}
            callable_[m["name"]] = bool(m.get("callable", True))
            cmode[m["name"]] = mode
        for m in prov["models"]:
            order[m["name"]] = {"provider": prov["provider"], "siblings": names,
                                "tier": m.get("tier"), "release": m.get("release"),
                                "max_output": m.get("max_output"),
                                "retire_date": m.get("retire_date"),
                                "mode": m.get("mode", "chat"), "context_window": m.get("context_window"),
                                "supports_prompt_caching": m.get("supports_prompt_caching"),
                                "open_weight": bool(m.get("open_weight", False)), "family": m.get("family"),
                                "host": m.get("host"), "confidence": m.get("confidence"),
                                "purpose": m.get("purpose"), "note": m.get("note")}
    return cat, price, order, aliases, callable_, cmode


_, PRICE, _ORDER, _ALIASES, _CALLABLE, _CMODE = _load_catalog()


def canonical_model(raw):
    """Map a raw/provider model string (exact, alias, or dated/versioned variant) onto a catalog name, or None."""
    if not raw:
        return None
    r = str(raw).strip()
    if r in PRICE:
        return r
    rl = r.lower()
    if rl in _ALIASES:
        return _ALIASES[rl]
    base = re.sub(r"[-_/:](\d{4}-\d{2}-\d{2}|\d{6,8}|v\d+|latest|preview|exp)$", "", rl)
    if base in PRICE:
        return base
    if base in _ALIASES:
        return _ALIASES[base]
    # the LONGEST catalog name that matches: 'gpt-5.4-…' is gpt-5.4, never its prefix gpt-5
    hits = [name for name in PRICE if (lambda nl: rl == nl or nl == base or rl.startswith(nl) or base.startswith(nl)
                                       or nl.startswith(base))(name.lower())]
    return max(hits, key=len) if hits else None


def canonical_exact(raw):
    """After exact and alias matches, the longest catalog id in `raw` bounded by non-alphanumerics or ends, or None.
    'bedrock/us.anthropic.claude-sonnet-5-v1:0' -> 'claude-sonnet-5'; 'gemma-3-1b' never collapses onto 'gemma-3-12b'.
    Longest wins, so adding a specific id such as 'bedrock-claude-sonnet-5' overrides for that name."""
    if not raw:
        return None
    r = str(raw).strip()
    if r in PRICE:
        return r
    rl = r.lower()
    if rl in _ALIASES:
        return _ALIASES[rl]
    best = None
    for name in PRICE:
        nl = name.lower()
        i = rl.find(nl)
        while i != -1:
            before = rl[i - 1] if i > 0 else ""
            after = rl[i + len(nl)] if i + len(nl) < len(rl) else ""
            if not before.isalnum() and not after.isalnum():
                if best is None or len(name) > len(best):
                    best = name
                break
            i = rl.find(nl, i + 1)
    return best


def is_callable(model):
    """False for retired or pricing-only ids (`callable: false` in models.json)."""
    return _CALLABLE.get(model, True)


def resolve_deployed(underlying):
    """A gateway's underlying model id or deployment name -> catalog id, or None (never guessed)."""
    return canonical_exact(underlying)


RETIRE_HORIZON_DAYS = 180        # a model retiring within this many days is never a downgrade target


def retire_date(model):
    """Announced shutdown date (YYYY-MM-DD), or None."""
    info = _ORDER.get(canonical_model(model) or model)
    return info.get("retire_date") if info else None


def _as_of_date(as_of=None):
    return datetime.date.fromisoformat(as_of) if as_of else datetime.date.today()


def retiring_within(model, days=RETIRE_HORIZON_DAYS, as_of=None):
    """True if the model retires on or before `as_of` + `days`."""
    d = retire_date(model)
    return bool(d and datetime.date.fromisoformat(d) <= _as_of_date(as_of) + datetime.timedelta(days=days))


_TIER_RANK = {"frontier": 0, "balanced": 1, "small": 2, "nano": 3}


def next_cheaper(model, avg_in=None, avg_out=None, as_of=None, avail=None):
    """Gentlest downgrade candidate in a lower tier of the same provider, or None. Skips models retiring within
    RETIRE_HORIZON_DAYS of `as_of` (default today) or rejected by `avail` (model -> bool). With a token mix
    (`avg_in`/`avg_out`): the newest net-saving model in the nearest lower tier, since price is not monotonic with
    tier. Without one: the most capable model there cheaper on both axes. The paid audit re-runs the pick."""
    m = canonical_model(model) or model
    info = _ORDER.get(m)
    if not info:
        return None
    cur = _TIER_RANK.get(info.get("tier"))
    p0 = PRICE.get(m, {})
    in0, out0 = p0.get("input"), p0.get("output")

    def _ok(s):
        return (_CALLABLE.get(s, True) and not retiring_within(s, as_of=as_of)
                and (avail is None or avail(s)))

    if cur is None:                                                   # untiered model: next in file order
        sibs = info["siblings"]
        for j in range(sibs.index(m) + 1, len(sibs)):
            if _ok(sibs[j]):
                return sibs[j]
        return None

    lower = [s for s in info["siblings"]
             if _ok(s) and _TIER_RANK.get(_ORDER[s].get("tier"), 99) > cur]
    if not lower:
        return None

    if avg_in is not None and avg_out is not None and None not in (in0, out0):
        def saving(s):
            ps = PRICE.get(s, {})
            return avg_in * (in0 - ps.get("input", in0)) + avg_out * (out0 - ps.get("output", out0))
        savers = [s for s in lower if saving(s) > 0]
        if not savers:
            return None
        nearest = min(_TIER_RANK.get(_ORDER[s].get("tier"), 99) for s in savers)
        pool = [s for s in savers if _TIER_RANK.get(_ORDER[s].get("tier"), 99) == nearest]
        return max(pool, key=lambda s: (_ORDER[s].get("release") or "", saving(s)))   # newest, then bigger saving

    cands = []
    for s in lower:
        ps = PRICE.get(s, {})
        si, so = ps.get("input"), ps.get("output")
        if None not in (in0, out0, si, so) and not (si <= in0 and so <= out0 and (si < in0 or so < out0)):
            continue
        cands.append((s, _TIER_RANK.get(_ORDER[s].get("tier"), 99), ps.get("input", 0)))
    if not cands:
        return None
    nearest = min(r for _, r, _ in cands)
    return max(((s, p) for s, r, p in cands if r == nearest), key=lambda x: x[1])[0]   # priciest input = most capable


def open_weight_target(model, avg_in=None, avg_out=None, as_of=None, avail=None):
    """Opt-in open-weight target for a node on `model`, or None: a cross-provider swap within the same tier.
    The strongest in-tier open-weight model that is callable, live, served, confidently priced, not a moderation
    model, and net-saving. No open-weight frontier tier exists, and there is no commercial fallback."""
    m = canonical_model(model) or model
    info0 = _ORDER.get(m, {})
    cur_tier = info0.get("tier")
    p0 = PRICE.get(m, {})
    in0, out0 = p0.get("input"), p0.get("output")
    if cur_tier is None or None in (in0, out0):
        return None

    def _saves(si, so):
        if avg_in is not None and avg_out is not None:
            return avg_in * (in0 - si) + avg_out * (out0 - so) > 0
        return si <= in0 and so <= out0 and (si < in0 or so < out0)

    def _cap_rank(s):
        sibs = _ORDER[s].get("siblings", [])
        return sibs.index(s) if s in sibs else 99

    cands = []
    for s in PRICE:
        info = _ORDER.get(s, {})
        if not info.get("open_weight") or info.get("tier") != cur_tier:
            continue
        if info.get("confidence") == "unverified" or info.get("purpose") == "moderation":
            continue
        if not (_CALLABLE.get(s, True) and not retiring_within(s, as_of=as_of) and (avail is None or avail(s))):
            continue
        ps = PRICE.get(s, {})
        si, so = ps.get("input"), ps.get("output")
        if None in (si, so) or not _saves(si, so):
            continue
        cands.append(s)
    if not cands:
        return None
    return min(cands, key=_cap_rank)                                  # catalog order is capability order


def provider_of(model):
    """The provider name for a catalog model, or None."""
    info = _ORDER.get(canonical_model(model) or model)
    return info["provider"] if info else None


def meta(model):
    """The model's full catalog metadata row as a plain dict, or {}."""
    return dict(_ORDER.get(canonical_model(model) or model) or {})


def tier(model):
    """Capability class: 'frontier' | 'balanced' | 'small' | 'nano', or None."""
    info = _ORDER.get(canonical_model(model) or model)
    return info.get("tier") if info else None


def release(model):
    """Approximate launch (YYYY-MM), or None; verify before quoting."""
    info = _ORDER.get(canonical_model(model) or model)
    return info.get("release") if info else None


DEFAULT_MAX_OUTPUT = 32768


def max_output(model):
    """Max output tokens from the catalog, else DEFAULT_MAX_OUTPUT; bounds a replay so it is never truncated."""
    info = _ORDER.get(canonical_model(model) or model)
    mo = info.get("max_output") if info else None
    return int(mo) if mo else DEFAULT_MAX_OUTPUT


def cache_min(model):
    """The provider's smallest cacheable prefix (tokens) for this model."""
    return PRICE.get(canonical_model(model) or model, {}).get("cache_min", 1024)


def cache_mode(model):
    """'explicit' (opt-in, so enabling caching is a real fix) or 'auto' (provider caches by itself). Unknown models
    are 'auto', so we never advise a cache breakpoint the catalog doesn't call for."""
    return _CMODE.get(canonical_model(model) or model, "auto")


def approx_tokens(text):
    """Rough token estimate (~4 chars/token). Replace with a real tokenizer."""
    return max(1, len(str(text)) // 4)


def usd(tokens, price_per_million):
    return tokens / 1_000_000 * price_per_million
