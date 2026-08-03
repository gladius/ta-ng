"""Per-agent computed-view store — compute once per key, read many.

Deliberately simple: a dict + a TTL + per-key locks. No background jobs, no polling, no request-context
globals. Callers pass an explicit `key` and a `build` function; `get_or_build` computes on first access
(under a per-key lock so concurrent requests never double-compute), serves cached until the TTL expires,
and `clear(key)` busts one agent for a re-audit. When version-scoping lands, `version` joins the key so
invalidation is just "the key changed" — never a staleness guess.
"""
import os
import threading
import time

TTL = int(os.environ.get("VIEW_TTL", "300"))       # seconds a computed view stays fresh

_CACHE = {}                                         # key -> (ts, value)
_LOCKS = {}                                         # key -> Lock  (per-key: builds serialize per agent, not globally)
_GUARD = threading.Lock()


def _lock_for(key):
    with _GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def get_or_build(key, build, ttl=TTL):
    """Cached value for `key`, or build it once (safe under concurrency) and cache it."""
    e = _CACHE.get(key)
    if e and (time.time() - e[0]) <= ttl:
        return e[1]
    with _lock_for(key):
        e = _CACHE.get(key)                          # re-check inside the lock — another thread may have built it
        if e and (time.time() - e[0]) <= ttl:
            return e[1]
        value = build()
        _CACHE[key] = (time.time(), value)
        return value


def put(key, value):
    """Store a value computed elsewhere (e.g. a streamed, paid proof) so later GETs read it without rebuilding."""
    _CACHE[key] = (time.time(), value)
    return value


def peek(key):
    """Return a cached value if present, else None — WITHOUT building. For paid artifacts (a downgrade
    audit): a page GET should show the result only if it was already computed, never trigger the paid run."""
    e = _CACHE.get(key)
    return e[1] if e else None


def clear(key=None):
    """Bust one agent (re-audit) or everything."""
    if key is None:
        _CACHE.clear()
    else:
        _CACHE.pop(key, None)
