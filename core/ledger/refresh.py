"""One refresh: the BigQuery script (identity, ledger, gaps; temp tables only) -> the app's tables -> profiles for
new call-sites -> the cache diagnoses. Run from the Cache analysis page or POST /api/suite/cache/refresh."""
import datetime
import threading

from core import profiler
from core.identity import store as callsite_store
from core.ledger import build, tables

_lock = threading.Lock()
_state = {"running": False, "error": None, "finished": None, "built_at": None}


def run(days=30):
    from modules.cache import report
    with _lock:
        _state.update(running=True, error=None)
    b = None
    try:
        b = build.Build(days)
        sites = b.sites()
        callsite_store.sync(sites, days)
        tables.store_usecase_lines(b.usecase_lines())
        ledger = b.ledger()
        gaps = b.gaps()
        tables.store_ledger(ledger)
        tables.store_gaps(gaps)
        for site in sites:                                   # name each new call-site once
            have = profiler.get(site["site"])
            if have and not have.get("usecase") and site["usecase"]:
                profiler.put(site["site"], site["usecase"], have)
            if not have:
                req = b.sample_request(site["site"]) or {}
                msgs = req.get("messages") or [{}]
                c = msgs[0].get("content")
                system = "\n".join(p.get("text", "") for p in c if isinstance(p, dict)) if isinstance(c, list) else (c or "")
                first_user = next((m.get("content") for m in msgs[1:] if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
                profiler.profile(site["site"], site["usecase"], system,
                                 [t.get("function", {}).get("name", "") for t in req.get("tools") or []], first_user)
        built_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        tables.store_diagnoses(report.diagnoses(sites, ledger, gaps, days), built_at)
        _state["built_at"] = built_at.isoformat(timespec="seconds")
    except Exception as e:
        _state["error"] = "%s: %s" % (type(e).__name__, e)
        raise
    finally:
        if b:
            b.close()
        _state.update(running=False, finished=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))


def start(days=30):
    if _state["running"]:
        return False
    threading.Thread(target=run, args=(days,), daemon=True).start()
    return True


def status():
    return dict(_state)
