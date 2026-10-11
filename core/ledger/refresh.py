"""Two refreshes, both read-only on BigQuery:

  list_usecases(days)        the cheap list (usecase, calls, days) -> the usecases table; reloaded when older than STALE
  analyse(usecase, days)     one usecase: the script scoped to it (identity, series, gaps; temp tables only) -> its
                             call-site records, a profile for each new one, the cache decision on each
  analyse(None, days)        the same for the whole window (every usecase)

Opening a usecase analyses it when it never was; the router or any caller can POST the same."""
import datetime
import threading

from core import profiler
from core.identity import store
from core.ledger import build

STALE = datetime.timedelta(hours=12)
_lock = threading.Lock()
_running = set()


def _texts(req):
    msgs = req.get("messages") or [{}]
    c = msgs[0].get("content")
    system = "\n".join(p.get("text", "") for p in c if isinstance(p, dict)) if isinstance(c, list) else (c or "")
    first_user = next((m.get("content") for m in msgs[1:] if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
    tools = [t.get("function", {}).get("name", "") for t in req.get("tools") or []]
    return system, tools, first_user


def list_usecases(days=30):
    rows = build.usecases(days)
    store.list_usecases(rows, days)
    return len(rows)


def list_is_stale():
    at = store.listed_at()
    return at is None or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - at > STALE


def analyse(usecase=None, days=30):
    """Analyse one usecase (or the whole window when None). Blocking; see start()."""
    from modules.cache import report
    key = usecase if usecase is not None else "*"
    b = None
    scoped = [usecase] if usecase is not None else None
    try:
        for uc in scoped or []:
            store.mark_analysing(uc, True)
        b = build.Build(days, usecase)
        sites = b.sites()
        ledger = b.ledger()
        gaps = b.gaps()
        store.sync(sites, ledger, days, usecase)
        for site in sites:                                   # name each new call-site once
            if not profiler.get(site["site"]):
                req = b.sample_request(site["site"]) or {}
                profiler.profile(site["site"], *_texts(req))
        built_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        for site, decision in report.cache_decisions(sites, ledger, gaps, days).items():
            store.set_decision(site, "cache", {**decision, "built_at": built_at})
        for uc in (scoped if scoped is not None else {s.get("usecase") or "" for s in sites}):
            store.set_usecase_summary(uc, sum(1 for s in sites if (s.get("usecase") or "") == (uc or "")))
            store.mark_analysing(uc, False)
        return len(sites)
    except Exception as e:
        for uc in scoped or []:
            store.mark_analysing(uc, False, "%s: %s" % (type(e).__name__, e))
        raise
    finally:
        if b:
            b.close()
        with _lock:
            _running.discard(key)


def start(usecase=None, days=30):
    """Analyse in the background; False when that analysis is already running."""
    key = usecase if usecase is not None else "*"
    with _lock:
        if key in _running:
            return False
        _running.add(key)
    threading.Thread(target=analyse, args=(usecase, days), daemon=True).start()
    return True


def running():
    return sorted(_running)
