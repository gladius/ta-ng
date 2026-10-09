"""The ONE place the LLM provider is configured. A single shared OpenAI-format client to the LiteLLM gateway
(LLM_GATEWAY_URL), which serves every provider, + a thin `complete()` wrapper with bounded retry on transient errors
(429 / 5xx / connection). Every paid caller (audit, downgrade_refset, profile, the validation engine and judges) goes
through here, so the key, the client, and the retry policy live in exactly one file.

The OpenAI client is thread-safe, so `complete()` is safe to call from the parallel proof workers.
"""
import json
import time
import threading

import openai

class Unreachable(Exception):
    """The gateway could not be reached, even after retries. It says nothing about
    the model, so a caller retries later instead of recording a failed call."""


class Refused(Exception):
    """The provider or the gateway refuses the ACCOUNT, not this request: out of credit or quota, over a budget, not
    allowed (401/403), or no gateway configured. Every call gets the same answer until someone fixes it, so it is
    raised at once, unretried."""


_REFUSED = ("insufficient_quota", "credit balance is too low", "budget_exceeded", "budget has been exceeded")
# about ONE model or ONE request, not the account: recorded as that call's failure, the job goes on
_NOT_ACCOUNT = ("access model", "model_access_denied", "not allowed to access", "content policy", "content_policy",
                "guardrail", "safety")


def _refused(e):
    text = str(e).lower()
    if any(w in text for w in _NOT_ACCOUNT):
        return False
    return e.status_code in (401, 403) or any(w in text for w in _REFUSED)


_client = None
_lock = threading.Lock()


def client():
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                # LiteLLM's /v1/chat/completions takes the OpenAI format for every provider it serves (Anthropic,
                # Gemini, open-weight ...), so a logged OpenAI request re-runs on any of them as it was sent. The
                # registry resolves the same (url, key), so availability and transport can never point elsewhere.
                from app import registry                 # lazy: registry -> catalog only, no cycle back into llm_client
                base, key = registry._gateway()
                if not base:
                    raise Refused("no model gateway: set LLM_GATEWAY_URL and LLM_GATEWAY_KEY")
                _client = openai.OpenAI(api_key=key, base_url=registry._roots(base)[-1] + "/v1")
    return _client


def _complete_raw(**kw):
    """Transport only: chat.completions.create with bounded retry. Retries 429 / 5xx / connection with backoff; a 4xx (bad
    request) raises at once — retrying a malformed call only wastes time and money. No temperature policy lives here,
    so the sampling path (run) and the deterministic path (complete) can share ONE retry without sharing a default.

    The model name is translated to the wire form HERE, at the only create call in the app: canonical catalog id
    -> the central gateway's own routing name (registry.serving_name). Everything upstream — pricing, the ladder, the
    cache_control / thinking decisions that key on the model string — stays canonical; only this create sees the
    gateway's deployment alias. No-ops off-gateway, and the caller's kw is untouched (debugcap still logs canonical)."""
    from app.registry import serving_name                 # lazy: registry -> catalog only, no cycle back into llm_client
    if kw.get("model"):
        kw = {**kw, "model": serving_name(kw["model"])}
    c = client()                 # LiteLLM: its response cache would hand repeated runs one copy, and its request log
    kw = {**kw, "extra_body": {"cache": {"no-cache": True}, "no-log": True}}       # (-> BigQuery) is production traffic
    last = None
    for attempt in range(4):
        try:
            return c.chat.completions.create(**kw)
        except openai.APIStatusError as e:
            last = e
            if _refused(e):                                  # out of credit / quota / budget, or no access
                raise Refused(str(e)[:300]) from e
            if e.status_code < 500 and e.status_code != 429:
                raise                                        # client error — don't retry
        except (openai.APIConnectionError, openai.APITimeoutError) as e:
            last = e
        time.sleep(min(8.0, 1.5 * (2 ** attempt)))
    if isinstance(last, openai.APIConnectionError) and not isinstance(last, openai.APITimeoutError):
        raise Unreachable(str(last)) from last              # can't connect: the endpoint itself is down. A timeout is
    raise last                                               # one slow call: that run fails, the request is unverified


def cache_block(text):
    """An Anthropic prompt-cache breakpoint: mark this text part as a cacheable PREFIX (LiteLLM passes it on to Claude).
    Safe to use anywhere — complete() STRIPS cache_control before a non-Claude model (keyed on the model NAME), so it
    never reaches another provider."""
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


def _has_cache_ctl(obj):
    """True if a cache_control breakpoint SURVIVES in this request part — i.e. what we ACTUALLY send after stripping.
    The judge-cache proof records this so '0 cache writes' can be read correctly: did WE drop the breakpoint, or did
    the provider/gateway ignore one we really sent?"""
    if isinstance(obj, dict):
        return "cache_control" in obj or any(_has_cache_ctl(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_cache_ctl(v) for v in obj)
    return False


# ── the ONE usage contract — read token counts here, never a provider field directly ─────────────────────────
# Two shapes reach this reader: our calls and logged OpenAI responses (LiteLLM, OpenAI-shaped), and Anthropic-shaped
# usage in older recorded traces.
#   Anthropic: input_tokens EXCLUDES the cache; cache_read_input_tokens / cache_creation_input_tokens beside it.
#   OpenAI / LiteLLM: prompt_tokens INCLUDES the cache read (prompt_tokens_details.cached_tokens) and, for Claude, the
#   cache write (cache_creation_input_tokens) — measured on the gateway: haiku 8422 = 10 + 8412 written, nano
#   7724 = 812 + 6912 read. So input = prompt_tokens - read - write, else cost_of would charge the cache twice.
# LiteLLM issue #27763: the cache read sits under different keys per shape; usage() reads whichever is there.
def _u_get(u, *names):
    """First present numeric field across attribute-style (Anthropic SDK) or dict-style (LiteLLM/OpenAI JSON) usage."""
    for n in names:
        v = getattr(u, n, None)
        if v is None and isinstance(u, dict):
            v = u.get(n)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def _u_cached_details(u):
    """OpenAI/LiteLLM cache read nested at usage.prompt_tokens_details.cached_tokens (object OR dict)."""
    det = getattr(u, "prompt_tokens_details", None)
    if det is None and isinstance(u, dict):
        det = u.get("prompt_tokens_details")
    if det is None:
        return 0
    v = getattr(det, "cached_tokens", None)
    if v is None and isinstance(det, dict):
        v = det.get("cached_tokens")
    return int(v or 0)


def usage(resp):
    """Transport-agnostic token usage -> {input (uncached), output, cache_read, cache_write}. Accepts a full
    response, a bare usage object, or a dict; Anthropic- or OpenAI/LiteLLM-shaped. THE reader for cache read/write,
    so callers are provider/transport-agnostic."""
    u = getattr(resp, "usage", None)
    if u is None:
        u = resp.get("usage", resp) if isinstance(resp, dict) else resp   # dict may be the response OR bare usage
    if u is None:
        return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    read = max(_u_get(u, "cache_read_input_tokens", "cacheReadInputTokens", "cachedContentTokenCount",
                      "total_cached_tokens"), _u_cached_details(u))
    write = _u_get(u, "cache_creation_input_tokens", "cacheWriteInputTokens")
    inp = _u_get(u, "input_tokens") or max(0, _u_get(u, "prompt_tokens") - read - write)
    return {"input": inp, "output": _u_get(u, "output_tokens", "completion_tokens"), "cache_read": read,
            "cache_write": write}


def text(resp):
    """The answer text of a chat completion ('' when there is none, e.g. only a tool call)."""
    return (resp.choices[0].message.content or "") if resp.choices else ""


def complete(**kw):
    """EVALUATION calls — the fit judge, coherence, the profiler.

    NO temperature — DEPRECATED on current models (claude-sonnet-5 etc.), returns 400 (verified live); determinism
    comes from VOTING, not a pinned temperature. `system` (text or content parts) becomes the first message.
    cache_control is honored on a Claude model and STRIPPED for anything else, so it can never break another
    provider. Per-call cache/token/latency is recorded to the ONE debugcap store under AUDIT_DEBUG for the judge-cache
    proof. Read the reply with text()."""
    system = kw.pop("system", None)
    if system:
        kw["messages"] = [{"role": "system", "content": system}, *kw["messages"]]
    is_anth = _is_anthropic(kw.get("model"))
    if not is_anth:
        _strip_cache(kw.get("messages"))
    sent_cache = _has_cache_ctl(kw.get("messages"))                    # what actually leaves us
    t0 = time.time()
    r = _complete_raw(**kw)
    from app.services import debugcap                        # lazy: keep llm_client a leaf, no import cycle
    if debugcap.enabled():                                   # AUDIT_DEBUG: cache/token/latency -> judge-cache proof
        us = usage(r)                                        # the ONE transport-agnostic reader
        debugcap.record_call({"model": kw.get("model"),
                              "anthropic": is_anth,           # did we treat it as Claude (else cache_control stripped)?
                              "cache_sent": sent_cache,       # did a cache_control breakpoint actually leave our process?
                              "base_url": str(getattr(client(), "base_url", "") or ""),   # gateway in the path? (litellm)
                              "cache_write": us["cache_write"],
                              "cache_read": us["cache_read"],
                              "input": us["input"],
                              "output": us["output"],
                              "ms": round((time.time() - t0) * 1000)})
    return r


def _tool_choice(tc):
    """Neutral tool_choice -> OpenAI form. Neutral: None (auto), 'any' (force some tool), {'tool': name}."""
    if tc == "any":
        return "required"
    if isinstance(tc, dict) and tc.get("tool"):
        return {"type": "function", "function": {"name": tc["tool"]}}
    return None


def _args(raw):
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return raw                                           # not JSON: shown as the model wrote it


# a target may reject these (reasoning, a forced tool, a sampling setting); the second try leaves them out and the
# result says it was degraded. The gateway already drops what it knows a model doesn't take (seed on Claude, ...)
_OPTIONAL = ("reasoning_effort", "tool_choice", "parallel_tool_calls", "temperature", "top_p", "seed", "stop",
             "frequency_penalty", "presence_penalty")


def run(*, model, messages, max_tokens, system=None, tools=None, tool_choice=None, reasoning=False, params=None):
    """Provider-NEUTRAL model call. Callers pass INTENT (neutral tools/tool_choice/reasoning) or, for a logged OpenAI
    request, its own fields as `params` (tools, tool_choice, response_format, temperature ...: sent as they are, over
    the neutral ones). Returns a NEUTRAL result {text, tool_calls:[{name, args}], usage, latency_ms, model (as
    served), stop_reason, degraded}, so everything above it (detection, audit, verdict) stays provider-agnostic.

    Neutral shapes:  tools=[{name, description, schema}]  ·  tool_choice=None|'any'|{'tool': name}  ·  reasoning=bool"""
    kw = {"model": model, "max_tokens": max_tokens,
          "messages": ([{"role": "system", "content": system}] if system else []) + list(messages)}
    if tools:
        kw["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": (t.get("description") or "")[:1024],
            "parameters": t.get("schema") or {"type": "object", "properties": {}}}} for t in tools]
        tc = _tool_choice(tool_choice)
        if tc:
            kw["tool_choice"] = tc                           # only valid alongside tools
    if reasoning:                                            # recorded node used extended thinking -> reproduce it
        kw["reasoning_effort"] = "low"
        kw["max_tokens"] = max(max_tokens, 1536)
    kw.update(params or {})
    t0, degraded = time.time(), False
    try:
        r = _complete_raw(**kw)                              # NOT complete(): replay must keep the request's own
    except openai.BadRequestError:                           # temperature (or the model default) to sample its variance
        if not any(k in kw for k in _OPTIONAL):              # the target rejects one of them ->
            raise                                            # best effort once, and the result says it was degraded
        r = _complete_raw(**{k: v for k, v in kw.items() if k not in _OPTIONAL})
        degraded = True
    choice = r.choices[0] if r.choices else None
    calls = [{"name": c.function.name, "args": _args(c.function.arguments)}
             for c in ((choice.message.tool_calls if choice else None) or []) if getattr(c, "function", None)]
    return {"text": text(r), "tool_calls": calls, "usage": usage(r),
            "latency_ms": round((time.time() - t0) * 1000), "model": getattr(r, "model", None) or model,
            "stop_reason": choice.finish_reason if choice else None, "degraded": degraded}
