"""Every paid model call goes through here: one shared, thread-safe OpenAI-format client to the LiteLLM gateway
(LLM_GATEWAY_URL), which serves every provider, with bounded retry on 429 / 5xx / connection errors."""
import json
import time
import threading

import openai

class Unreachable(Exception):
    """Gateway unreachable after retries. That says nothing about the model: retry later, don't record a failure."""


class Refused(Exception):
    """The provider or gateway refuses the account, not this request (credit, quota, budget, 401/403, no gateway).
    Every call would get the same answer, so it is raised at once, unretried."""


_REFUSED = ("insufficient_quota", "credit balance is too low", "budget_exceeded", "budget has been exceeded")
# about one model or one request, not the account: recorded as that call's failure, the job goes on
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
                # LiteLLM takes the OpenAI format for every provider, so a logged OpenAI request re-runs as it was sent.
                from app import registry                 # lazy: no import cycle
                base, key = registry._gateway()
                if not base:
                    raise Refused("no model gateway: set LLM_GATEWAY_URL and LLM_GATEWAY_KEY")
                # max_retries=0: the SDK's own retries (2, timeouts included) stacked on _complete_raw's 4 made one
                # hung call cost 12 timeouts; _complete_raw is the only retry
                _client = openai.OpenAI(api_key=key, base_url=registry._roots(base)[-1] + "/v1", max_retries=0)
    return _client


def _complete_raw(retry_timeout=True, **kw):
    """Transport only: chat.completions.create, retrying 429 / 5xx / connection; another 4xx raises at once (a retried
    bad request only costs money). A timeout is retried too unless `retry_timeout` is False (a caller with its own
    fallback: the grouping loop logs the seed failed instead of waiting out four timeouts). Only here does the
    canonical model id become the gateway's routing name."""
    from app.registry import serving_name
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
            if _refused(e):
                raise Refused(str(e)[:300]) from e
            if e.status_code < 500 and e.status_code != 429:
                raise
        except (openai.APIConnectionError, openai.APITimeoutError) as e:
            last = e
            if isinstance(e, openai.APITimeoutError) and not retry_timeout:
                raise
        time.sleep(min(8.0, 1.5 * (2 ** attempt)))
    if isinstance(last, openai.APIConnectionError) and not isinstance(last, openai.APITimeoutError):
        raise Unreachable(str(last)) from last              # can't connect: the endpoint itself is down. A timeout is
    raise last                                               # one slow call: that run fails, the request is unverified


def cache_block(text):
    """A text part marked as an Anthropic prompt-cache prefix; complete() strips the mark for non-Claude models."""
    return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}


def _is_anthropic(model):
    return "claude" in (model or "").lower()


def _strip_cache(obj):
    if isinstance(obj, dict):
        obj.pop("cache_control", None)
        for v in obj.values():
            _strip_cache(v)
    elif isinstance(obj, list):
        for v in obj:
            _strip_cache(v)


def _has_cache_ctl(obj):
    if isinstance(obj, dict):
        return "cache_control" in obj or any(_has_cache_ctl(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_cache_ctl(v) for v in obj)
    return False


# ── usage contract: read token counts through usage(), never a provider field directly ─────────────────────────
# Anthropic shape (older recorded traces): input_tokens excludes the cache; cache read/write fields sit beside it.
# OpenAI / LiteLLM shape: prompt_tokens includes the cache read and, for Claude, the cache write (measured on the
# gateway), so input = prompt_tokens - read - write, else cost_of charges the cache twice.
# LiteLLM #27763: the cache read sits under different keys per shape; usage() reads whichever is there.
def _u_get(u, *names):
    for n in names:
        v = getattr(u, n, None)
        if v is None and isinstance(u, dict):
            v = u.get(n)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def _u_cached_details(u):
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
    """Token usage of a response, usage object or dict -> {input (uncached), output, cache_read, cache_write}."""
    u = getattr(resp, "usage", None)
    if u is None:
        u = resp.get("usage", resp) if isinstance(resp, dict) else resp
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
    """Evaluation calls (judges, coherence, profiler); read the reply with text(). No temperature: current models
    reject it (400), and determinism comes from voting. cache_control is kept for Claude only."""
    system = kw.pop("system", None)
    if system:
        kw["messages"] = [{"role": "system", "content": system}, *kw["messages"]]
    is_anth = _is_anthropic(kw.get("model"))
    if not is_anth:
        _strip_cache(kw.get("messages"))
    sent_cache = _has_cache_ctl(kw.get("messages"))
    t0 = time.time()
    r = _complete_raw(**kw)
    from app.services import debugcap                        # lazy: no import cycle
    if debugcap.enabled():
        us = usage(r)
        debugcap.record_call({"model": kw.get("model"),
                              "anthropic": is_anth,
                              "cache_sent": sent_cache,
                              "base_url": str(getattr(client(), "base_url", "") or ""),
                              "cache_write": us["cache_write"],
                              "cache_read": us["cache_read"],
                              "input": us["input"],
                              "output": us["output"],
                              "ms": round((time.time() - t0) * 1000)})
    return r


def _tool_choice(tc):
    """Neutral tool_choice (None, 'any', {'tool': name}) -> OpenAI form."""
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


# a target may reject these; the second try leaves them out and marks the result degraded
_OPTIONAL = ("reasoning_effort", "tool_choice", "parallel_tool_calls", "temperature", "top_p", "seed", "stop",
             "frequency_penalty", "presence_penalty")


def run(*, model, messages, max_tokens, system=None, tools=None, tool_choice=None, reasoning=False, params=None):
    """Provider-neutral model call: tools=[{name, description, schema}], tool_choice=None|'any'|{'tool': name}; a logged
    OpenAI request passes its own fields as `params`, sent as they are. Returns {text, tool_calls:[{name, args}], usage,
    latency_ms, model (as served), stop_reason, degraded}."""
    kw = {"model": model, "max_tokens": max_tokens,
          "messages": ([{"role": "system", "content": system}] if system else []) + list(messages)}
    if tools:
        kw["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": (t.get("description") or "")[:1024],
            "parameters": t.get("schema") or {"type": "object", "properties": {}}}} for t in tools]
        tc = _tool_choice(tool_choice)
        if tc:
            kw["tool_choice"] = tc
    if reasoning:
        kw["reasoning_effort"] = "low"
        kw["max_tokens"] = max(max_tokens, 1536)
    kw.update(params or {})
    t0, degraded = time.time(), False
    try:
        r = _complete_raw(**kw)                              # not complete(): replay keeps the request's temperature
    except openai.BadRequestError:
        if not any(k in kw for k in _OPTIONAL):
            raise
        r = _complete_raw(**{k: v for k, v in kw.items() if k not in _OPTIONAL})
        degraded = True
    choice = r.choices[0] if r.choices else None
    calls = [{"name": c.function.name, "args": _args(c.function.arguments)}
             for c in ((choice.message.tool_calls if choice else None) or []) if getattr(c, "function", None)]
    return {"text": text(r), "tool_calls": calls, "usage": usage(r),
            "latency_ms": round((time.time() - t0) * 1000), "model": getattr(r, "model", None) or model,
            "stop_reason": choice.finish_reason if choice else None, "degraded": degraded}
