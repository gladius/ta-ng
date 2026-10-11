"""One refresh: sites -> call_sites -> ledger_daily -> cache_gaps in BigQuery, then the cache report into our DB.
Run once a day (a scheduler calling POST /api/suite/cache/refresh) or on demand."""
import datetime
import threading

from app.services import store
from core.ledger import daily, gaps, sites

KEY = ("cache", "report")
_lock = threading.Lock()
_state = {"running": False, "error": None, "finished": None}


def run(days=14):
    """Build everything and store the report; returns it."""
    from modules.cache import report
    with _lock:
        _state.update(running=True, error=None)
    try:
        sites.build(days)
        daily.build(days)
        gaps.build(days)
        rep = report.build(days)
        rep["built_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        store.put(KEY, rep)
        return rep
    except Exception as e:
        _state["error"] = "%s: %s" % (type(e).__name__, e)
        raise
    finally:
        _state.update(running=False, finished=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))


def start(days=14):
    """Run in the background; False when one is already running."""
    if _state["running"]:
        return False
    threading.Thread(target=run, args=(days,), daemon=True).start()
    return True


def status():
    return dict(_state)


def latest():
    return store.peek(KEY)
