"""One refresh: the BigQuery script (identity, ledger, gaps; temp tables only, nothing written there) -> the
call-site records and the daily ledger in the app's database -> a profile for each new call-site -> the cache
decision on each record. Run from the Cache analysis page or POST /api/suite/cache/refresh."""
import datetime
import threading

from core import profiler
from core.identity import store
from core.ledger import build, tables

_lock = threading.Lock()
_state = {"running": False, "error": None, "finished": None, "built_at": None}


def _texts(req):
    msgs = req.get("messages") or [{}]
    c = msgs[0].get("content")
    system = "\n".join(p.get("text", "") for p in c if isinstance(p, dict)) if isinstance(c, list) else (c or "")
    first_user = next((m.get("content") for m in msgs[1:] if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
    tools = [t.get("function", {}).get("name", "") for t in req.get("tools") or []]
    return system, tools, first_user


def run(days=30):
    from modules.cache import report
    with _lock:
        _state.update(running=True, error=None)
    b = None
    try:
        b = build.Build(days)
        sites = b.sites()
        ids = store.sync(sites, days)
        store.drop_missing(ids)
        ledger = b.ledger()
        gaps = b.gaps()
        tables.store_ledger(ledger)
        for site in sites:                                   # name each new call-site once
            if not profiler.get(site["site"]):
                req = b.sample_request(site["site"]) or {}
                profiler.profile(site["site"], *_texts(req))
        built_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        for site, decision in report.cache_decisions(sites, ledger, gaps, days).items():
            store.set_decision(site, "cache", {**decision, "built_at": built_at.isoformat(timespec="seconds")})
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
