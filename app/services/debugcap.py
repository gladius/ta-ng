"""ONE dedicated in-memory debug-capture store (AUDIT_DEBUG only), so debug capture isn't scattered across modules.

Low-level + dependency-free (stdlib only), so llm_client / snapdump / prove can all touch it without import cycles.

Scope: the store is reset at the START of an audit and drained by the dumper — SINGLE-INSTANCE / SERIAL-audit scoped
(the app runs --max-instances=1 and audits are triggered one at a time). It is NOT keyed per snap, because the events
originate deep inside nested thread pools (prove -> audit -> judge) where the snap id isn't available; the PER-SNAP
boundary is the disk dump under .audit_debug/<snap>/. If this ever needs to be concurrent-safe per snap, carry the
snap via a contextvar propagated into the worker pools — deliberately not done for a dev-only flag."""
import os
import threading

_calls = []
_lock = threading.Lock()


def enabled():
    return os.environ.get("AUDIT_DEBUG") not in (None, "", "0", "false", "False")


def record_call(rec):
    """Record ONE eval/judge model call's cache + token + latency stats (no-op unless AUDIT_DEBUG)."""
    if not enabled():
        return
    with _lock:
        _calls.append(rec)


def calls():
    with _lock:
        return list(_calls)


def reset():
    with _lock:
        _calls.clear()
