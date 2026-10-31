"""In-memory debug capture (AUDIT_DEBUG only); stdlib-only so any module can use it without import cycles.
Process-wide: reset at audit start and drained by snapdump, so one audit at a time (a contextvar would fix that)."""
import os
import threading

_calls = []
_judges = []
_lock = threading.Lock()


def enabled():
    return os.environ.get("AUDIT_DEBUG") not in (None, "", "0", "false", "False")


def record_call(rec):
    """Record one model call's cache, token and latency stats (no-op unless AUDIT_DEBUG)."""
    if not enabled():
        return
    with _lock:
        _calls.append(rec)


def record_judge(rec):
    """Record one fit-judge call's raw reply and what was parsed from it (no-op unless AUDIT_DEBUG)."""
    if not enabled():
        return
    with _lock:
        _judges.append(rec)


def calls():
    with _lock:
        return list(_calls)


def judges():
    with _lock:
        return list(_judges)


def reset():
    with _lock:
        _calls.clear()
        _judges.clear()
