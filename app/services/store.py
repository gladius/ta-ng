"""Computed-view + proof store — a small in-memory key->value store.

Holds immutable / frozen values keyed by an explicit key: a pinned report snapshot (`("snapshot", id)`) and a
completed proof (`proof_key(...)`). No TTL — a snapshot is pinned per report and a proof is frozen; a re-audit mints
a NEW key rather than mutating one. Bounded by an LRU so memory stays flat.

DEPLOYMENT: this is per-PROCESS. On Cloud Run run with `--max-instances=1` (optionally `--min-instances=1` to avoid
cold starts) so ONE instance serves a user's whole flow (page-load + prove + the after-prove re-fetch). That's the
simple, correct setup for a low-traffic advisory tool — no shared database needed. If you ever need to scale
horizontally, swap ONLY this module's backend for a shared store (GCS/Redis); callers touch just put/peek/clear.
"""
import os
import threading
from collections import OrderedDict

_CACHE = OrderedDict()                                        # key -> value (LRU-ordered)
_MAX = int(os.environ.get("AUDIT_STORE_MAX", "200"))         # bound memory: most-recent N keys
_LOCK = threading.Lock()


def put(key, value):
    """Store `value` under `key` (overwrites). Bounds the store to the most-recent MAX keys."""
    with _LOCK:
        _CACHE[key] = value
        _CACHE.move_to_end(key)
        while len(_CACHE) > _MAX:
            _CACHE.popitem(last=False)
    return value


def peek(key):
    """The stored value for `key`, or None — never builds (a page GET shows a paid artifact only if already run)."""
    with _LOCK:
        v = _CACHE.get(key)
        if key in _CACHE:
            _CACHE.move_to_end(key)
    return v


def clear(key=None):
    """Bust one key (re-audit) or everything."""
    with _LOCK:
        _CACHE.clear() if key is None else _CACHE.pop(key, None)
