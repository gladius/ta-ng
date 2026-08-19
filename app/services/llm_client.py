"""The ONE place the LLM provider is configured. A single shared Anthropic client + a thin `complete()` wrapper
with bounded retry on transient errors (429 / 5xx / connection). Every paid caller (audit, cache_proof) goes
through here, so the key, the client, and the retry policy live in exactly one file — and swapping providers or
tuning retry is a one-file change.

The Anthropic client is thread-safe, so `complete()` is safe to call from the parallel proof workers.
"""
import os
import time
import threading

import credentials
import anthropic

_client = None
_lock = threading.Lock()


def client():
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                # base_url points at the real Anthropic API by default, OR at a litellm gateway when set — litellm's
                # /v1/messages is Anthropic-format and translates the SAME request to OpenAI/Gemini/etc., so replay
                # can PROVE non-Anthropic call-sites through the gateway (no code change per provider). None => default.
                _client = anthropic.Anthropic(
                    api_key=credentials.get_secret("ANTHROPIC_API_KEY",
                                                   aliases=("ANTHROPIC_KEY", "CLAUDE_API_KEY", "LITELLM_API_KEY")),
                    base_url=credentials.get_config("ANTHROPIC_BASE_URL", None,
                                                    aliases=("LITELLM_BASE_URL", "LLM_BASE_URL")))
    return _client


def _complete_raw(**kw):
    """Transport only: messages.create with bounded retry. Retries 429 / 5xx / connection with backoff; a 4xx (bad
    request) raises at once — retrying a malformed call only wastes time and money. No temperature policy lives here,
    so the sampling path (run) and the deterministic path (complete) can share ONE retry without sharing a default."""
    last = None
    for attempt in range(4):
        try:
            return client().messages.create(**kw)
        except anthropic.APIStatusError as e:
            last = e
            if e.status_code < 500 and e.status_code != 429:
                raise                                        # client error — don't retry
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            last = e
        time.sleep(min(8.0, 1.5 * (2 ** attempt)))
    raise last


def cache_block(text):
    """An Anthropic prompt-cache breakpoint: mark this text as a cacheable PREFIX. Safe to use anywhere — complete()
    STRIPS cache_control before a non-Claude model (keyed on the model NAME, so it works whether Claude is reached
    directly OR via a litellm gateway, and never reaches another provider)."""
    return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}


def _is_anthropic(model):
    return "claude" in (model or "").lower()


def _strip_cache(obj):
    """Recursively drop cache_control keys so a non-Claude provider (via litellm) can't choke on them."""
    if isinstance(obj, dict):
        obj.pop("cache_control", None)
        for v in obj.values():
            _strip_cache(v)
    elif isinstance(obj, list):
        for v in obj:
            _strip_cache(v)


def complete(**kw):
    """EVALUATION / REWRITE calls — the fit judge, coherence, the compress optimizer, the profiler.

    NO temperature — DEPRECATED on current models (claude-sonnet-5 etc.), returns 400 (verified live); determinism
    comes from VOTING, not a pinned temperature. cache_control is honored on a Claude model (direct or via litellm)
    and STRIPPED for anything else, so it can never break another provider. Per-call cache/token/latency is recorded
    to the ONE debugcap store under AUDIT_DEBUG for the judge-cache proof."""
    if not _is_anthropic(kw.get("model")):
        _strip_cache(kw.get("messages"))
        _strip_cache(kw.get("system"))
    t0 = time.time()
    r = _complete_raw(**kw)
    from app.services import debugcap                        # lazy: keep llm_client a leaf, no import cycle
    if debugcap.enabled():                                   # AUDIT_DEBUG: cache/token/latency -> judge-cache proof
        u = getattr(r, "usage", None)
        debugcap.record_call({"model": kw.get("model"),
                              "cache_write": int(getattr(u, "cache_creation_input_tokens", 0) or 0),
                              "cache_read": int(getattr(u, "cache_read_input_tokens", 0) or 0),
                              "input": int(getattr(u, "input_tokens", 0) or 0),
                              "output": int(getattr(u, "output_tokens", 0) or 0),
                              "ms": round((time.time() - t0) * 1000)})
    return r


def _anthropic_tool_choice(tc):
    """Neutral tool_choice -> Anthropic form. Neutral: None (auto), 'any' (force some tool), {'tool': name}."""
    if tc == "any":
        return {"type": "any"}
    if isinstance(tc, dict) and tc.get("tool"):
        return {"type": "tool", "name": tc["tool"]}
    return None


def run(*, model, messages, max_tokens, system=None, tools=None, tool_choice=None, reasoning=False):
    """Provider-NEUTRAL model call — THE ONE boundary to swap for a litellm gateway in production. Callers pass
    INTENT (neutral tools/tool_choice/reasoning); this translates to the provider's request shape and returns a
    NEUTRAL result {text, tool_calls:[{name, args}]}. Everything above this (detection, audit, verdict) stays
    provider-agnostic — to run through litellm, reimplement only this function.

    Neutral shapes:  tools=[{name, description, schema}]  ·  tool_choice=None|'any'|{'tool': name}  ·  reasoning=bool
    Anthropic implementation below."""
    kw = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if system:
        kw["system"] = system
    if tools:
        kw["tools"] = [{"name": t["name"], "description": (t.get("description") or "")[:1024],
                        "input_schema": t.get("schema") or {"type": "object", "properties": {}}} for t in tools]
        atc = _anthropic_tool_choice(tool_choice)
        if atc:
            kw["tool_choice"] = atc                          # only valid alongside tools
    if reasoning:                                            # recorded node used extended thinking -> reproduce it
        kw["thinking"] = {"type": "adaptive"}
        kw["output_config"] = {"effort": "low"}
        kw["max_tokens"] = max(max_tokens, 1536)
    try:
        r = _complete_raw(**kw)                              # NOT complete(): replay must keep the model-default
    except Exception:                                        # temperature to sample natural run-to-run variance
        if "thinking" in kw or "tool_choice" in kw:          # target can't reason / force -> best-effort, retry once
            kw.pop("thinking", None); kw.pop("output_config", None); kw.pop("tool_choice", None)
            r = _complete_raw(**kw)
        else:
            raise
    text, tool_calls = [], []
    for b in r.content:
        if getattr(b, "type", None) == "text":
            text.append(b.text)
        elif getattr(b, "type", None) == "tool_use":
            tool_calls.append({"name": b.name, "args": b.input})
    return {"text": "\n".join(text), "tool_calls": tool_calls}
