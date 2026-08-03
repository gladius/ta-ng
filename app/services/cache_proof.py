"""Cache proof — the paid half of the cache lever. Deterministic, no disk.

Detection ($0) says a prefix SHOULD cache. This proves it caches for real: send the recovered prefix twice with a
cache breakpoint and read the provider's own counters back — 1st call WRITES it to cache, 2nd call READS it. The
`cache_read_input_tokens` the API returns on the 2nd call is ground truth, not an estimate.

Caching cannot change model output (Anthropic: "Prompt caching has no effect on output token generation. The
response is identical."), so there is NO behavior judge here — the round-trip counters are the whole proof.
"""
from auditor.util import approx_tokens, canonical_model, cache_min
from app.services.cache import _prefix_text, _recoverable_text
from app.services import llm_client


def _usage(model, system_blocks, user):
    r = llm_client.complete(model=model, max_tokens=1, system=system_blocks,
                            messages=[{"role": "user", "content": user}])
    u = r.usage
    return (getattr(u, "cache_creation_input_tokens", 0) or 0,
            getattr(u, "cache_read_input_tokens", 0) or 0)


def prove(bucket, model=None):
    """Round-trip the recovered prefix on the real provider. Paid: exactly 2 completions, max_tokens=1 each
    (~a few thousand input tok total, cents). Returns {write, read, prefix_tok, model, proven}."""
    if not bucket:
        return None
    model = canonical_model(model or bucket[0].get("model")) or (model or bucket[0].get("model"))
    prefix = _recoverable_text([_prefix_text(t) for t in bucket])          # the reorged, byte-stable prefix
    prefix_tok = approx_tokens(prefix)
    cmin = cache_min(model)
    if prefix_tok < cmin:                                                  # honest: below the min, nothing caches
        return {"model": model, "write": 0, "read": 0, "prefix_tok": prefix_tok, "proven": False,
                "reason": "prefix %d tok < model minimum %d" % (prefix_tok, cmin)}

    blocks = [{"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}]
    write, _ = _usage(model, blocks, "ping")                              # 1st call writes the prefix to cache
    _, read = _usage(model, blocks, "pong")                               # 2nd (diff user turn) reads it back
    return {"model": model, "write": write, "read": read, "prefix_tok": prefix_tok, "proven": read > 0}
