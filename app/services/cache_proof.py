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
    """(cache_write_tokens, cache_read_tokens) from the provider's OWN counters on this one call.

    Reads cache signals from BOTH response shapes so the round-trip proves a real cache READ no matter how the
    request is routed:
      - Anthropic-native (direct API, or Anthropic through litellm): `cache_creation_input_tokens` /
        `cache_read_input_tokens` on `usage`.
      - litellm-normalized (OpenAI / Google through the gateway report cached reads OpenAI-style):
        `usage.prompt_tokens_details.cached_tokens`.
    Anthropic caching is EXPLICIT (the `cache_control` breakpoint on the prefix); OpenAI/Google cache
    AUTOMATICALLY, so there the breakpoint is a harmless no-op and the cached read only surfaces in
    `prompt_tokens_details`. We take whichever field is populated. NOTE: confirm the exact litellm field for your
    gateway version — "read both" is the robust default. A non-zero read on the 2nd (warm) call is the ground
    truth that this exact prefix caches.
    """
    r = llm_client.complete(model=model, max_tokens=1, system=system_blocks,
                            messages=[{"role": "user", "content": user}])
    u = r.usage
    write = getattr(u, "cache_creation_input_tokens", 0) or 0
    read = getattr(u, "cache_read_input_tokens", 0) or 0
    if not read:                                        # non-Anthropic via litellm -> cached read is OpenAI-shaped
        det = getattr(u, "prompt_tokens_details", None)
        cached = getattr(det, "cached_tokens", None) if det is not None else None
        if cached is None and isinstance(det, dict):    # some gateways hand back usage as plain dicts
            cached = det.get("cached_tokens")
        read = int(cached or 0)
    return (write, read)


def prove_prefix(model, prefix, cmin=None):
    """Round-trip an EXPLICIT prefix (e.g. a REORGED one) on the real provider: 1st call writes it to cache, 2nd
    (different user turn) reads it back. Paid: exactly 2 completions, max_tokens=1 each (cents). The returned
    `cache_read_input_tokens` is provider ground truth that THIS exact prefix caches. -> {write, read, prefix_tok,
    proven}."""
    model = canonical_model(model) or model
    prefix_tok = approx_tokens(prefix)
    cmin = cache_min(model) if cmin is None else cmin
    if prefix_tok < cmin:                                                  # honest: below the min, nothing caches
        return {"model": model, "write": 0, "read": 0, "prefix_tok": prefix_tok, "proven": False,
                "reason": "prefix %d tok < model minimum %d" % (prefix_tok, cmin)}
    blocks = [{"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}]
    write, _ = _usage(model, blocks, "ping")                              # 1st call writes the prefix to cache
    _, read = _usage(model, blocks, "pong")                               # 2nd (diff user turn) reads it back
    return {"model": model, "write": write, "read": read, "prefix_tok": prefix_tok, "proven": read > 0}


def prove(bucket, model=None):
    """Round-trip the byte-compare recovered prefix (the current cache lever). Delegates to prove_prefix."""
    if not bucket:
        return None
    model = canonical_model(model or bucket[0].get("model")) or (model or bucket[0].get("model"))
    return prove_prefix(model, _recoverable_text([_prefix_text(t) for t in bucket]))
