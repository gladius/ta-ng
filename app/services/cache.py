"""Prompt-cache detection — deterministic, $0. For a call-site's bucket of recorded traces, find how much of the
prompt's stable prefix (tools + system) actually caches today vs. how much a reorganization could recover.

Providers cache a CONTIGUOUS, byte-identical leading prefix (send order: tools -> system -> messages). Anything
that perturbs the leading bytes — a per-call timestamp, an injected RAG chunk, a reordered tool list — breaks it.

  cached-today : the longest byte-identical leading prefix across the bucket (what caches as-is).
  recoverable  : the stable content recoverable by a reorg (every line byte-identical across the bucket) — the
                 real ceiling once the per-call bits are moved to the end.
  verdict      : CACHEABLE   — a big stable prefix, caching just isn't on (Anthropic: add a breakpoint).
                 BREAKER     — a per-call value breaks the leading prefix, but a reorg recovers a cacheable one.
                 TOO-SMALL   — even reorganized, the stable prefix is below the model's cache minimum.
                 ALREADY     — the provider is already serving this prefix from cache.

No LLM. The paid write->read PROOF (that the recovered prefix actually caches) lives in app/services/cache_proof.py.
"""
from app.catalog import PRICE, approx_tokens, canonical_model, cache_min, cache_mode
# cache_min / cache_mode come straight from the model catalog (config/models.json) — the single source of truth.
# cache_mode gates the whole lever: 'explicit' providers (Anthropic) need a breakpoint → "enable caching" is a
# real, provable fix; 'auto' providers (OpenAI, Google) cache eligible prefixes on their own → "enable" is a
# no-op, but a per-call prefix that BREAKS the stable region still defeats that auto-cache, so reorg still applies.


def _tools_text(trace):
    parts = []
    for item in trace.get("tools_defined") or []:
        name, schema = (item + ["", ""])[:2] if isinstance(item, (list, tuple)) else (str(item), "")
        parts.append("%s %s" % (name, schema))
    return "\n".join(parts)


def _prefix_text(trace):
    """tools + system — the cacheable region, in provider send order (before the variable messages)."""
    tools = _tools_text(trace)
    system = next((m.get("content", "") for m in trace.get("input_messages", []) if m.get("role") == "system"), "")
    return (tools + "\n" if tools else "") + (system or "")


def _common_prefix(strings):
    if not strings:
        return ""
    common = strings[0]
    for s in strings[1:]:
        i = 0
        m = min(len(common), len(s))
        while i < m and common[i] == s[i]:
            i += 1
        common = common[:i]
        if not common:
            break
    return common


def _recoverable_text(prefixes):
    """The lines byte-identical across EVERY sample — the static content a reorg can pull into one stable prefix,
    even when per-call bits are scattered through it. Preserves the first sample's order."""
    if len(prefixes) < 2:
        return prefixes[0] if prefixes else ""
    common = set.intersection(*[set(l for l in p.split("\n") if l.strip()) for p in prefixes])
    return "\n".join(l for l in prefixes[0].split("\n") if l in common)


def annotate_prompt(bucket):
    """The full prompt (from one sample), line by line, each tagged 'static' (byte-identical across the whole
    bucket → part of the cacheable prefix) or 'dynamic' (varies per call → what breaks the prefix). Drives the
    highlighted before-view so you SEE which bytes cost you the cache."""
    prefixes = [_prefix_text(t) for t in bucket]
    common = set.intersection(*[set(p.split("\n")) for p in prefixes]) if len(prefixes) > 1 \
        else set(prefixes[0].split("\n"))
    return [{"t": ln, "kind": "static" if (ln in common or not ln.strip()) else "dynamic"}
            for ln in prefixes[0].split("\n")]


def detect(bucket, model=None):
    """One call-site's cache finding. Deterministic. Returns a dict the report/funnel render directly."""
    if not bucket:
        return None
    model = model or bucket[0].get("model") or ""
    price = PRICE.get(canonical_model(model) or model, {})
    cmin = cache_min(model)
    mode = cache_mode(model)                                         # 'explicit' (Anthropic) | 'auto' (OpenAI/Google)

    prefixes = [_prefix_text(t) for t in bucket]
    common = _common_prefix(prefixes)
    cur_tok = approx_tokens(common)                                  # caches today (as-is)
    recoverable_tok = approx_tokens(_recoverable_text(prefixes))     # caches after a reorg
    n_distinct = len(set(prefixes))

    # already served from cache? (provider ground truth from the recorded usage)
    cached_avg = sum((t.get("usage") or {}).get("cached_tokens", 0) for t in bucket) / max(1, len(bucket))

    if cached_avg > 0:
        verdict = "ALREADY"                                         # provider is already returning cache hits
    elif mode == "none":
        verdict = "NONE"                                            # host offers no prompt caching (open-weight) -> no lever
    elif recoverable_tok < cmin:
        verdict = "TOO-SMALL"                                       # even reorganized, below the provider minimum
    elif mode == "explicit":
        # Anthropic: nothing caches until you add a breakpoint.
        verdict = "CACHEABLE" if cur_tok >= cmin else "BREAKER"     # stable prefix -> enable; broken -> reorg+enable
    else:
        # OpenAI / Google: eligible prefixes cache automatically. A stable prefix is ALREADY saving (no action);
        # a per-call prefix that breaks the stable region silently defeats that auto-cache -> reorg unlocks it.
        verdict = "AUTO" if cur_tok >= cmin else "BREAKER"

    # the break: where the byte-identical prefix ends, and the per-call span that broke it
    at = len(common)
    dyn_span = (prefixes[0][at:at + 60] or "").replace("\n", " ").strip()

    # $ per 1,000 calls unlocked by ACTING: enable (explicit CACHEABLE) or reorg (BREAKER). AUTO already saves →
    # nothing to unlock; ALREADY/TOO-SMALL → 0. cache-read is ~90% cheaper than fresh input.
    save_tok = cur_tok if verdict == "CACHEABLE" else (recoverable_tok if verdict == "BREAKER" else 0)
    save_per_1k = round((price.get("input", 0) - price.get("cache_read", 0)) / 1e6 * save_tok * 1000, 2)

    return {
        "verdict": verdict, "mode": mode, "model": model, "samples": len(bucket), "n_distinct": n_distinct,
        "cached_today_tok": cur_tok, "recoverable_tok": recoverable_tok, "cache_min": cmin,
        "break_at": at, "dyn_span": dyn_span, "save_per_1k": save_per_1k,
        "cacheable": verdict in ("CACHEABLE", "BREAKER"),          # an opportunity to act on (AUTO already saves)
    }
