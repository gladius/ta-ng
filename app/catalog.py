"""Shared helpers: the model/price catalog + token estimation.

Pricing lives in config/models.json (providers -> models in capability order, $/1M tokens).
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
import datetime

_CATALOG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "models.json")  # repo-root/config


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
                                "max_output": m.get("max_output"),    # per-model output ceiling (optional)
                                "retire_date": m.get("retire_date"),  # announced shutdown (optional) -> lifecycle guard
                                "mode": m.get("mode", "chat"), "context_window": m.get("context_window"),
                                "supports_prompt_caching": m.get("supports_prompt_caching"),
                                # open-weight reference metadata (optional; see the open-weight block's _note)
                                "open_weight": bool(m.get("open_weight", False)), "family": m.get("family"),
                                "host": m.get("host"), "confidence": m.get("confidence"),
                                "purpose": m.get("purpose"), "note": m.get("note")}
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


def canonical_exact(raw):
    """The catalog id for a (possibly gateway-decorated) model name, or None. ONE rule: the LONGEST catalog id that
    appears in `raw` delimited by separators (string start/end, or any non-alphanumeric char). Every shape a gateway
    emits resolves the same way — 'bedrock-claude-sonnet-5', 'claude-sonnet-5-us', and
    'bedrock/us.anthropic.claude-sonnet-5-v1:0' all give 'claude-sonnet-5' — while 'gpt-oss-120bx' gives None (no
    boundary) and 'gemma-3-1b' gives None (not a catalog id, and never mis-collapsed onto 'gemma-3-12b'). Longest-wins
    is the override lever: add a specific 'bedrock-claude-sonnet-5' entry and it takes precedence for that name.
    Explicit aliases win first. This one substring match replaces the old peel-regexes + anchored matcher."""
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
            if not before.isalnum() and not after.isalnum():        # id sits between separators / string ends
                if best is None or len(name) > len(best):           # longest catalog id wins (override precedence)
                    best = name
                break
            i = rl.find(nl, i + 1)                                   # keep looking; the id may recur later in the name
    return best


def is_callable(model):
    """Is this catalog model actually callable on its provider's API? Retired / pricing-only ids (kept for the price
    ladder) are flagged `callable: false` in models.json — the ONE central place that knows this."""
    return _CALLABLE.get(model, True)


def resolve_deployed(underlying):
    """A gateway's underlying model id (or deployment name) -> our catalog id, or None. Delegates to the ONE catalog
    match (canonical_exact): the longest catalog id appearing in the name delimited by separators. That single rule
    covers every shape a gateway emits — route 'bedrock/…', namespace 'us.anthropic.…', host prefix 'bedrock-…',
    suffix '-us'/'-123', version '-v1:0' — with no shape-specific peeling. Unresolved names are surfaced, not guessed."""
    return canonical_exact(underlying)


# ── model lifecycle (retire_date is a MODEL property in config/models.json) ──────────────────────────────────
# The downgrade guard skips a model whose announced shutdown is near. Vendor MIGRATION PATHS live in app.migration.
RETIRE_HORIZON_DAYS = 180        # a model retiring within this many days is too soon to be a downgrade TARGET


def retire_date(model):
    """Announced shutdown date (YYYY-MM-DD) for a model, from config/models.json, or None."""
    info = _ORDER.get(canonical_model(model) or model)
    return info.get("retire_date") if info else None


def _as_of_date(as_of=None):
    return datetime.date.fromisoformat(as_of) if as_of else datetime.date.today()


def is_retired(model, as_of=None):
    """True if the model's announced shutdown is on/before `as_of` (default today). Retired models stay PRICED
    (old traces still cost-out) but are never offered as a downgrade target."""
    d = retire_date(model)
    return bool(d and datetime.date.fromisoformat(d) <= _as_of_date(as_of))


def retiring_within(model, days=RETIRE_HORIZON_DAYS, as_of=None):
    """True if the model retires on/before `as_of` + `days` — i.e. too soon to be a safe downgrade TARGET."""
    d = retire_date(model)
    return bool(d and datetime.date.fromisoformat(d) <= _as_of_date(as_of) + datetime.timedelta(days=days))


_TIER_RANK = {"frontier": 0, "balanced": 1, "small": 2, "nano": 3}     # capability classes, most → least capable


def next_cheaper(model, avg_in=None, avg_out=None, as_of=None, avail=None):
    """The gentlest downgrade CANDIDATE in a lower capability tier, same provider (or None).

    Node-aware pick (when a call-site's token mix `avg_in`/`avg_out` is given — the production path): the NEWEST
    callable model in the nearest lower tier that ACTUALLY net-saves for THAT mix. Pricing is NOT monotonic with
    tier — a newer 'flash' can cost MORE per input token than an older model yet be cheaper on OUTPUT — so whether a
    candidate saves depends on the node's mix. Net-saving on the real mix is strictly more precise than a both-axes
    Pareto rule, and 'newest' is our capability proxy within a tier. The pick is only a candidate — the paid audit
    re-runs it and books $ only on SAFE, so a mis-ranked pick fails as NOT-SAFE, never a false save.

    No-mix pick (CLI / tests): the legacy Pareto target — most-capable callable model in the nearest lower tier
    whose price is <= this model's on BOTH axes. Falls back to legacy file order for an untier'd model.

    Lifecycle guard: a candidate retiring within RETIRE_HORIZON_DAYS of `as_of` (retire_date in config/models.json)
    is NEVER a target. Availability guard: pass `avail` (a callable model->bool, e.g. registry.is_available) and a
    candidate the gateway does not serve is skipped — we never recommend a model that can't be called.
    `as_of` defaults to today; pass a fixed date in tests."""
    m = canonical_model(model) or model
    info = _ORDER.get(m)
    if not info:
        return None
    cur = _TIER_RANK.get(info.get("tier"))
    p0 = PRICE.get(m, {})
    in0, out0 = p0.get("input"), p0.get("output")

    def _ok(s):                                                      # callable, live (not retiring), and served
        return (_CALLABLE.get(s, True) and not retiring_within(s, as_of=as_of)
                and (avail is None or avail(s)))

    if cur is None:                                                   # untier'd model → legacy next-in-list
        sibs = info["siblings"]
        for j in range(sibs.index(m) + 1, len(sibs)):
            if _ok(sibs[j]):
                return sibs[j]
        return None

    lower = [s for s in info["siblings"]                              # callable, live, strictly lower capability tier
             if _ok(s) and _TIER_RANK.get(_ORDER[s].get("tier"), 99) > cur]
    if not lower:
        return None

    # ── node-aware: newest genuinely net-saving model in the nearest lower tier ──────────────────────────────
    if avg_in is not None and avg_out is not None and None not in (in0, out0):
        def saving(s):                                                # $ delta per call for this node's mix (unscaled)
            ps = PRICE.get(s, {})
            return avg_in * (in0 - ps.get("input", in0)) + avg_out * (out0 - ps.get("output", out0))
        savers = [s for s in lower if saving(s) > 0]
        if not savers:
            return None
        nearest = min(_TIER_RANK.get(_ORDER[s].get("tier"), 99) for s in savers)   # gentlest tier that saves
        pool = [s for s in savers if _TIER_RANK.get(_ORDER[s].get("tier"), 99) == nearest]
        return max(pool, key=lambda s: (_ORDER[s].get("release") or "", saving(s)))   # newest, then bigger saving

    # ── no-mix: legacy Pareto-cheaper, most capable in the nearest lower tier ────────────────────────────────
    cands = []
    for s in lower:
        ps = PRICE.get(s, {})
        si, so = ps.get("input"), ps.get("output")
        if None not in (in0, out0, si, so) and not (si <= in0 and so <= out0 and (si < in0 or so < out0)):
            continue                                                  # not cheaper on both axes -> illusory drop, skip
        cands.append((s, _TIER_RANK.get(_ORDER[s].get("tier"), 99), ps.get("input", 0)))
    if not cands:
        return None
    nearest = min(r for _, r, _ in cands)                             # nearest lower tier that HAS a real-drop model
    return max(((s, p) for s, r, p in cands if r == nearest), key=lambda x: x[1])[0]   # most capable in it


def open_weight_target(model, avg_in=None, avg_out=None, as_of=None, avail=None):
    """The recommended OPEN-WEIGHT downgrade target for a node currently on `model`, or None. This is the OPT-IN
    open-weight lane — a CROSS-provider jump that next_cheaper (same-provider siblings only) never makes.

    SAME-TIER swap: unlike the commercial lane (one tier DOWN), open-weight keeps the SAME capability class and
    swaps the commercial model for its open-weight peer — the saving comes from open weights being cheaper to run,
    not from losing capability. So we consider ONLY open-weight models in the SAME tier as `model`, and pick the
    STRONGEST in-tier one (first in the catalog's capability order) that: (a) is callable, live (not retiring) and
    served (`avail`); (b) has a trustworthy price (confidence != 'unverified'); (c) is a general chat model
    (purpose != 'moderation'); and (d) net-SAVES for this node's mix. There is NO open-weight 'frontier' tier, so a
    frontier node returns None. Strict: no fall-back to a commercial target. The paid proof (audit_node) is the real
    equivalence check — a nominal same-tier peer that is actually weaker fails as NOT-SAFE, never a false save."""
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
        return si <= in0 and so <= out0 and (si < in0 or so < out0)   # no-mix: both-axes cheaper (CLI/tests)

    def _cap_rank(s):                                                  # position in the capability-ordered block
        sibs = _ORDER[s].get("siblings", [])
        return sibs.index(s) if s in sibs else 99

    cands = []
    for s in PRICE:
        info = _ORDER.get(s, {})
        if not info.get("open_weight") or info.get("tier") != cur_tier:   # SAME TIER only
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
    return min(cands, key=_cap_rank)                                  # strongest in-tier by catalog capability order


def provider_of(model):
    """The provider name for a catalog model (or None if unknown)."""
    info = _ORDER.get(canonical_model(model) or model)
    return info["provider"] if info else None


def meta(model):
    """The full catalog metadata row for a model (provider, tier, release, mode, context_window, max_output,
    supports_prompt_caching, retire_date, …) as a plain dict, or {} if unknown. For the models view."""
    return dict(_ORDER.get(canonical_model(model) or model) or {})


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
