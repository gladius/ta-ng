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
