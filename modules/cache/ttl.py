"""TTL what-if from the recorded gaps: under a lifetime T, every call whose gap since the previous call sharing its
prefix is <= T reads the prefix; the rest write it. Priced per provider, it says what each lifetime would have cost
over the window, and the saving of the best one. No call is re-sent; this is arithmetic on the gap buckets."""
from core.ledger.gaps import BUCKETS
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
    current = {"cached_tokens": observed, "written_tokens": written,
               "cost": round((observed * p["input"] * r["read"] + max(prefix_all - observed, 0) * p["input"]) / 1e6, 4)}
    best = min(out, key=lambda t: out[t]["cost"])
    base = min(out)                                   # the provider's default lifetime
    beyond = sum(b["calls"] for name, b in by.items() if name not in _SECS or _SECS[name] > base)
    return {"provider": prov, "options": out, "current": current, "default_ttl": base, "best_ttl": best,
            "saving": round(out[base]["cost"] - out[best]["cost"], 4), "calls": total_calls,
            "beyond_default": beyond,                 # calls that came after the default lifetime (or first ever)
            "histogram": {name: by.get(name, {}).get("calls", 0) for name in ["first"] + [n for n, _ in BUCKETS] + ["more"]}}
