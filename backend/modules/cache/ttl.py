"""TTL what-if from the recorded gaps: under a lifetime T, every call whose gap since the previous call sharing its
prefix is <= T reads the prefix; the rest write it. Priced per provider, it says what each lifetime would have cost
over the window, and the saving of the best one. Where a write costs more than plain input (Claude), "no caching"
(option 0: every call pays the prefix at the input price) is compared too: caching that is rarely read is a loss.
An upper bound: a gap under the lifetime doesn't guarantee a hit (eviction, routing), so the measured reuse is shown
beside it. No call is re-sent; this is arithmetic on the gap buckets."""

NO_CACHE = 0
from core.ledger.build import BUCKETS
from modules.cache import rules

_SECS = dict(BUCKETS)


def _price(model):
    from app.catalog import PRICE, canonical_model
    return PRICE.get(canonical_model(model) or model or "")


def what_if(gap_rows, model):
    """gap_rows: the cache_gaps rows of one site+model -> {ttl: {reads, writes, cost}}, current {...}, best, saving.
    Costs are prefix costs only (the part a TTL changes); None when the model has no price."""
    prov, r, _ = rules.for_model(model)
    p = _price(model)
    if not p:
        return None
    by = {}
    for g in gap_rows:
        b = by.setdefault(g["bucket"], {"calls": 0, "prefix_tokens": 0, "cached": 0, "written": 0})
        for k in b:
            b[k] += g[k] or 0
    total_calls = sum(b["calls"] for b in by.values())
    out = {}
    for ttl, write_x in sorted(r["write"].items()):
        reads = sum(b["calls"] for name, b in by.items() if name in _SECS and _SECS[name] <= ttl)
        read_tokens = sum(b["prefix_tokens"] for name, b in by.items() if name in _SECS and _SECS[name] <= ttl)
        write_tokens = sum(b["prefix_tokens"] for name, b in by.items() if not (name in _SECS and _SECS[name] <= ttl))
        cost = (read_tokens * p["input"] * r["read"] + write_tokens * p["input"] * write_x) / 1e6
        out[ttl] = {"reads": reads, "writes": total_calls - reads, "cost": round(cost, 4)}
    observed = sum(b["cached"] for b in by.values())
    written = sum(b["written"] for b in by.values())
    prefix_all = sum(b["prefix_tokens"] for b in by.values())
    base = min(r["write"])                            # the provider's default lifetime
    if max(r["write"].values()) > 1.0:                # a write surcharge: not caching at all may be cheaper
        out[NO_CACHE] = {"reads": 0, "writes": 0, "cost": round(prefix_all * p["input"] / 1e6, 4)}
    rest = max(prefix_all - observed - written, 0)    # neither read nor written: paid at the input price
    current = {"cached_tokens": observed, "written_tokens": written,
               "cost": round((observed * r["read"] + written * r["write"][base] + rest) * p["input"] / 1e6, 4)}
    best = min(out, key=lambda t: out[t]["cost"])
    beyond = sum(b["calls"] for name, b in by.items() if name not in _SECS or _SECS[name] > base)
    loss = NO_CACHE in out and current["cost"] > out[NO_CACHE]["cost"]
    return {"provider": prov, "options": out, "current": current, "default_ttl": base, "best_ttl": best,
            "loss": round(current["cost"] - out[NO_CACHE]["cost"], 4) if loss else 0.0,   # caching costs more than not
            "saving": round(out[base]["cost"] - out[best]["cost"], 4), "calls": total_calls,
            "beyond_default": beyond,                 # calls that came after the default lifetime (or first ever)
            "histogram": {name: by.get(name, {}).get("calls", 0) for name in ["first"] + [n for n, _ in BUCKETS] + ["more"]}}
