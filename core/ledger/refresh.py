"""Two refreshes, both read-only on BigQuery:

  list_usecases(days)        the cheap list (usecase, calls, days) -> the usecases table; reloaded when older than STALE
  analyse(usecase, days)     one usecase: the script scoped to it (identity, series, gaps; temp tables only) -> its
                             call-site records, a profile for each new one, the cache decision on each
  analyse(None, days)        the same for the whole window (every usecase)

Opening a usecase analyses it when it never was; the router or any caller can POST the same. An analysis is a task
on the database queue (core/tasks.py): any worker runs it, a dead worker's is taken over, and one usecase is never
analysed twice at once. "Analysing" is read from the queue, not stored."""
import datetime

from core import profiler, tasks
from core.identity import store
from core.ledger import build

STALE = datetime.timedelta(hours=12)


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
    """Analyse one usecase (or the whole window when None). Blocking: the task start() queues runs it."""
    from modules.cache import report
    b = None
    scoped = [usecase] if usecase is not None else None
    try:
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
            store.set_cache_findings(site, {**decision, "built_at": built_at})
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


def start(usecase=None, days=30):
    """Queue the analysis; False when that one is already waiting or running."""
    key = usecase if usecase is not None else "*"
    if key in running():
        return False
    tasks.add("analyse_usecase", {"usecase": usecase, "days": days}, key="analyse:" + key)
    return True


def running():
    """The usecases being analysed or waiting for it ("*" is the whole window)."""
    return [k[len("analyse:"):] for k in tasks.pending("analyse:")]
