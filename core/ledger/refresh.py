"""One refresh: sites -> call_sites -> ledger_daily -> cache_gaps in BigQuery, then the cache report into our DB.
Run once a day (a scheduler calling POST /api/suite/cache/refresh) or on demand."""
import datetime
import threading

from app.services import store
from core import profiler
from core.identity import sql as identity
from core.ledger import daily, gaps

KEY = ("cache", "report")
_lock = threading.Lock()
_state = {"running": False, "error": None, "finished": None}


def run(days=30):
    """Build everything and store the report; returns it."""
    from modules.cache import report
    with _lock:
        _state.update(running=True, error=None)
    try:
        identity.build(days)
        for site in identity.sites(days):             # name each new call-site once
            if not profiler.get(site["site"]):
                req = identity.sample_request(site["site"], days) or {}
                msgs = req.get("messages") or [{}]
                c = msgs[0].get("content")
                system = "\n".join(p.get("text", "") for p in c if isinstance(p, dict)) if isinstance(c, list) else (c or "")
                first_user = next((m.get("content") for m in msgs[1:] if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
                profiler.profile(site["site"], site["usecase"], system,
                                 [t.get("function", {}).get("name", "") for t in req.get("tools") or []], first_user)
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


def start(days=30):
    """Run in the background; False when one is already running."""
    if _state["running"]:
        return False
    threading.Thread(target=run, args=(days,), daemon=True).start()
    return True


def status():
    return dict(_state)


def latest():
    return store.peek(KEY)
