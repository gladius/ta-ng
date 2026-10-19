"""Days in, call-sites out. Read-only on BigQuery; everything lands in the app's database.

  list_usecases(days)   the cheap list (usecase, calls, days) -> the usecases table; reloaded when older than STALE
  process_day(day)      one day for every usecase (or ONLY_USECASES): the day script reads the day and places the
                        calls by the fingerprints known (build.READ); the loop seeds the rest and makes call-sites and
                        versions with a model, proved by count (core/identity/loop.py); the script computes the ledger,
                        the cache gaps and the layout (build.FINISH); the numbers, coverage, samples and unplaced calls
                        are written; the cache findings of the call-sites touched are redone; the day's checks are
                        queued. A day is processed once; again replaces it; `redo` keeps the day's read (its session,
                        24 h) and runs the loop again.
  process_missing()     the window's days not processed yet, oldest first, one after another; start() queues it

A day is processed for all usecases at once: BigQuery bills the columns it reads for every day in range whatever
the usecase filter, so one usecase alone would cost the same. The window is ANALYSIS_WINDOW_DAYS; older days drop."""
import datetime
from collections import defaultdict

import credentials
from core import tasks
from core.identity import loop, store
from core.ledger import build

STALE = datetime.timedelta(hours=12)
KEY = "process_days"


def window():
    """Days of history kept and processed (ANALYSIS_WINDOW_DAYS, default 7)."""
    return int(credentials.get_config("ANALYSIS_WINDOW_DAYS") or 7)


def _today():
    return datetime.datetime.now(datetime.timezone.utc).date()


def _texts(req):
    msgs = req.get("messages") or [{}]
    c = msgs[0].get("content")
    system = "\n".join(p.get("text", "") for p in c if isinstance(p, dict)) if isinstance(c, list) else (c or "")
    first_user = next((m.get("content") for m in msgs[1:] if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
    tools = [t.get("function", {}).get("name", "") for t in req.get("tools") or []]
    return system, tools, first_user


def list_usecases(days=None):
    days = days or window()
    rows = build.usecases(days)
    store.list_usecases(rows, days)
    return len(rows)


def list_is_stale():
    at = store.listed_at()
    return at is None or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - at > STALE


# ---------------------------------------------------------------------------------------------------- processing
def process_day(day, redo=False):
    """Process one day (ISO date) for every usecase. Blocking: the task start() queues runs it."""
    day = str(day)
    tasks.note("%s: starting" % day)
    b = build.Build(day, day, store.fingerprints(), session_id=store.day_session(day), redo=redo)
    store.day_session(day, b.s.id)
    try:
        if b.resumed:
            tasks.note("%s: resumed from the last run's BigQuery session (the day was not read again)" % day)
        else:
            made = loop.run(b, day, store.brief)
            tasks.note("%s: %d call-sites made or given a version; totals" % (day, len(made)))
            b.finish(day)
        tasks.note("%s: writing the day" % day)
        day_sites, ledger, gaps, layout, pool, grams = b.day_sites(), b.ledger(), b.gaps(), b.layout(), b.pool(), b.grams()
        leftover, n_left = b.leftover()
        coverage = b.coverage()
    finally:
        b.close()
    recs = {r["site"]: r for uc in {d["usecase"] or "" for d in day_sites} for r in store.by_usecase(uc)}
    for d in day_sites:
        if d["site"] not in recs:                            # placed by a fingerprint whose call-site row is gone
            recs[d["site"]] = {"site": d["site"], "usecase": d["usecase"], "versions": [], "markers": []}
        rec = recs[d["site"]]
        for v in rec["versions"]:
            v["last"] = max(v.get("last") or day, day) if v.get("first", day) <= day else v.get("last")
        if d["site"] in grams:
            rec["grams"] = grams[d["site"]]
        if d["site"] in layout:
            lay = layout[d["site"]]
            rec["tool_sets"] = lay["tool_sets"]
            if rec.get("static_chars") is None:              # no model view of the layout: the measured prefix stands in
                rec.update(static_chars=lay["lead_chars"], dynamic_chars=max((lay["chars"] or 0) - (lay["lead_chars"] or 0), 0), static_after=0)
    rows = {}                                                # (site, day, model) -> the day's numbers
    for r in ledger:
        merge(rows, r["site"], day, r["model"] or "", r)
    for g in gaps:
        merge(rows, g["site"], day, g["model"] or "", {}, {g["bucket"]: g})
    store.replace_coverage([day], coverage)
    store.replace_days([day], [r for r in rows.values() if r["day"] == day])
    store.replace_samples([day], [{**p, "day": day} for p in pool])
    store.replace_unplaced([day], leftover)
    _save(recs, {d["usecase"] or "" for d in day_sites} | {r["usecase"] or "" for r in leftover})
    store.mark_day(day, sum(d["n"] for d in day_sites), n_left)
    tasks.note("%s: cache findings" % day)
    _cache(recs)
    tasks.note("%s: done%s" % (day, " (" + b.timing + ")" if getattr(b, "timing", None) else ""))
    tasks.add("quality_day", {"day": day}, key="quality:" + day)
    return len(day_sites)


AVERAGES = ("avg_ms", "avg_messages")
GAPS = ("calls", "prefix_tokens", "prompt_tokens", "cached", "written")


def merge(rows, site, day, model, numbers, gaps=None):
    """Add numbers (and gap buckets) into rows[(site, day, model)]: sums add, averages are weighted by calls."""
    e = rows.setdefault((site, day, model), {"site": site, "day": day, "model": model, **{k: 0 for k in store.NUMBERS}, "gaps": {}})
    n0, n1 = e.get("calls") or 0, numbers.get("calls") or 0
    for k in AVERAGES:
        if numbers.get(k) is not None:
            e[k] = ((e.get(k) or 0) * n0 + numbers[k] * n1) / (n0 + n1) if n0 + n1 else numbers[k]
    for k in store.NUMBERS:
        if k not in AVERAGES:
            e[k] = (e.get(k) or 0) + (numbers.get(k) or 0)
    for bucket, g in (gaps or {}).items():
        b = e["gaps"].setdefault(bucket, {k: 0 for k in GAPS})
        for k in GAPS:
            b[k] += g.get(k) or 0
    return e


def _save(recs, usecases):
    """Trim the history to the window, then write every call-site's summary over it."""
    cutoff = str(_today() - datetime.timedelta(window()))
    store.drop_sites(store.trim(cutoff))
    summary = store.site_summary()
    for rec in recs.values():
        if rec["site"] not in summary:
            continue
        calls, first, last = summary[rec["site"]]
        rec.update(calls=calls, first_day=first, last_day=last)
        store.save_identity(rec, window())
    for uc in usecases:
        store.set_usecase_summary(uc, sum(1 for r in recs.values() if (r["usecase"] or "") == uc and r["site"] in summary))


def _cache(recs):
    """The cache findings of every call-site touched, from its history in the window."""
    from modules.cache import report
    built_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    sites = [{"site": r["site"], "usecase": r["usecase"], "static_chars": r.get("static_chars"), "dynamic_chars": r.get("dynamic_chars"),
              "static_after": r.get("static_after"), "tool_sets": r.get("tool_sets"), "versions": r["versions"]} for r in recs.values()]
    ledger, gaps = [], []
    for site, by_model in store.series([s["site"] for s in sites]).items():
        for model, by_day in by_model.items():
            for day, v in by_day.items():
                ledger.append({"site": site, "model": model, "day": day, **{k: v.get(k) for k in store.NUMBERS}})
                for bucket, g in (v.get("gaps") or {}).items():
                    gaps.append({"site": site, "model": model, "bucket": bucket, **g})
    for site, decision in report.cache_decisions(sites, ledger, gaps, window()).items():
        store.set_cache_findings(site, {**decision, "built_at": built_at})


# ------------------------------------------------------------------------------------------------------- queueing
def missing_days():
    """The window's days not processed yet, up to yesterday, oldest first."""
    done = store.days_done()
    today = _today()
    return [d for d in (str(today - datetime.timedelta(n)) for n in range(window(), 0, -1)) if d not in done]


def process_missing():
    """Process the missing days in order, one BigQuery read each. The scheduled job's task."""
    n = 0
    for day in missing_days():
        process_day(day)
        n += 1
    return n


def start():
    """Queue the missing days (one task, in order); False when nothing is missing or it is already queued."""
    if running() or not missing_days():
        return False
    tasks.add("process_days", {}, key=KEY)
    return True


def running():
    """["*"] while days are being processed, else []."""
    return ["*"] if tasks.pending(KEY) else []
