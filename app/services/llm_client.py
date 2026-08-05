"""The ONE place the LLM provider is configured. A single shared Anthropic client + a thin `complete()` wrapper
with bounded retry on transient errors (429 / 5xx / connection). Every paid caller (audit, cache_proof) goes
through here, so the key, the client, and the retry policy live in exactly one file — and swapping providers or
tuning retry is a one-file change.

The Anthropic client is thread-safe, so `complete()` is safe to call from the parallel proof workers.
"""
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
                _client = anthropic.Anthropic(
                    api_key=credentials.get_secret("ANTHROPIC_API_KEY", aliases=("ANTHROPIC_KEY", "CLAUDE_API_KEY")))
    return _client


def complete(**kw):
    """messages.create with retry. Retries 429 / 5xx / connection with backoff; a 4xx (bad request) raises at
    once — retrying a malformed call only wastes time and money."""
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
        r = complete(**kw)
    except Exception:
        if "thinking" in kw or "tool_choice" in kw:          # target can't reason / force -> best-effort, retry once
            kw.pop("thinking", None); kw.pop("output_config", None); kw.pop("tool_choice", None)
            r = complete(**kw)
        else:
            raise
    text, tool_calls = [], []
    for b in r.content:
        if getattr(b, "type", None) == "text":
            text.append(b.text)
        elif getattr(b, "type", None) == "tool_use":
            tool_calls.append({"name": b.name, "args": b.input})
    return {"text": "\n".join(text), "tool_calls": tool_calls}
