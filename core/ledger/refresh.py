"""Days in, call-sites out. Read-only on BigQuery; everything lands in the app's database.

  list_usecases(days)   the cheap list (usecase, calls, days) -> the usecases table; reloaded when older than STALE
  process_day(day)      the day script over that day for every usecase (build.py): the day's groups are linked to the
                        known call-sites usecase by usecase (same template: the same call-site; changed: a new version;
                        unknown: a new call-site, profiled by its own task), the day's numbers and sample ids are
                        written, the calls no group placed are kept and grouped across days, the cache findings of the
                        call-sites touched are redone. A day is processed once; again replaces it.
  process_missing()     the window's days not processed yet, oldest first, one after another (two days at once
                        would each create the call-sites new that day); start() queues it as one task

A day is processed for all usecases at once: BigQuery bills the columns it reads for every day in range whatever
the usecase filter, so one usecase alone would cost the same. The window is ANALYSIS_WINDOW_DAYS; older days drop."""
import datetime
import hashlib
from collections import Counter, defaultdict

import credentials
from core import profiler, tasks
from core.identity import store
from core.ledger import build

STALE = datetime.timedelta(hours=12)
SAME_SITE = 0.7          # template overlap (Jaccard) for a day's group to be a known call-site
REVISION = (3, 0.1)      # a new version when the template gained or lost >= 3 windows and >= 10% of them
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


# ------------------------------------------------------------------------------------------------------- linking
def jaccard(a, b):
    return len(a & b) / len(a | b) if a or b else 0.0


def site_id(usecase, template):
    """A new call-site's id: from its usecase and its template's first phrases (the same text, the same id)."""
    key = (usecase or "") + "\x1f" + "|".join(sorted(template)[:build.TOP_K])
    return str(int.from_bytes(hashlib.sha1(key.encode()).digest()[:8], "big", signed=True))


def link(template, known):
    """The known call-site (site -> template) this template is, or None."""
    best = max(known.items(), key=lambda kv: jaccard(template, kv[1]), default=None)
    return best[0] if best and jaccard(template, best[1]) >= SAME_SITE else None


def group_leftovers(calls):
    """{id: window hashes} of one usecase's unplaced calls -> [(template, ids)] grouped the way the day script groups:
    windows in >= MIN_CALLS calls, the TOP_K most common as the key; a group's template = windows in >= CORE of its calls;
    a call is in when it holds >= CONTAIN of the template."""
    count = Counter(h for w in calls.values() for h in w)
    groups = defaultdict(list)
    for i, w in calls.items():
        sig = tuple(sorted((h for h in w if count[h] >= build.MIN_CALLS), key=lambda h: (-count[h], h))[:build.TOP_K])
        if sig:
            groups[sig].append(i)
    out = []
    for ids in groups.values():
        if len(ids) < build.MIN_CALLS:
            continue
        n = Counter(h for i in ids for h in calls[i])
        t = {h for h, k in n.items() if k >= build.CORE * len(ids)}
        markers = sorted(t, key=lambda h: (-n[h], h))[:build.MARKERS]
        need = min(build.HITS, len(markers))
        placed = [i for i in ids if need and len(set(markers) & calls[i]) >= need]
        if placed:
            out.append((t, markers, placed))
    return out


# ---------------------------------------------------------------------------------------------------- processing
def _version(rec, template, day):
    """Follow the call-site's template day by day: the same one extends the current version, a changed one starts
    a new one (a revision)."""
    versions, old = rec["versions"], set(rec["template"])
    changed = len(old ^ template)
    if versions and (changed < REVISION[0] or changed < REVISION[1] * max(len(old), len(template))):
        versions[-1]["last"] = max(versions[-1]["last"], day)
        return                                            # the same template: a window flipping in or out is noise
    versions.append({"version": site_id("v", template), "first": day, "last": day})
    rec["template"] = sorted(template)


def distinct_markers(markers, template, siblings):
    """-> (the fingerprint's phrases, ambiguous). Never empty: the phrases held by the fewest sibling call-sites,
    those the day script chose first among equals. Ambiguous when even those are in some sibling's template: such a
    call-site is then placed only on all its phrases and loses ties (worst case: its calls stay unplaced, never land
    in a sibling)."""
    shared = lambda p: sum(1 for s in siblings if p in s)
    first = {m: i for i, m in enumerate(markers)}
    ranked = sorted(set(template) | set(markers), key=lambda p: (shared(p), first.get(p, len(first)), p))
    keep = ranked[:build.MARKERS]
    return keep, any(shared(p) for p in keep)


def _new_site(site, usecase, template, markers, req):
    return {"site": site, "usecase": usecase, "template": sorted(template), "markers": list(markers), "versions": [],
            "signature": [], "request": req, "new": True}


def process_day(day):
    """Process one day (ISO date) for every usecase. Blocking: the task start() queues runs it."""
    day = str(day)
    tasks.note("%s: starting" % day)
    b = build.Build(day, day, store.fingerprints(), session_id=store.day_session(day))
    store.day_session(day, b.s.id)
    if b.resumed:
        tasks.note("%s: resumed from the last run's BigQuery session (the day was not read again)" % day)
    tasks.note("%s: linking the day's call-sites to the known ones" % day)
    day_sites, ledger, gaps, layout, pool = b.day_sites(), b.ledger(), b.gaps(), b.layout(), b.pool()
    leftover, n_left = b.leftover()
    by_uc = defaultdict(list)
    for d in day_sites:
        by_uc[d["usecase"] or ""].append(d)
    for r in leftover:
        by_uc.setdefault(r["usecase"] or "", [])
    mapping, recs = {}, {}                             # day site -> site; site -> identity rec
    for uc, ds in by_uc.items():                       # one usecase at a time: its templates only
        known = store.templates(uc)
        recs.update({r["site"]: r for r in store.by_usecase(uc)})
        for d in sorted(ds, key=lambda d: d["site"]):
            t = set(d["template"] or [])
            site = d["site"] if d["site"] in known else link(t, known)   # placed by a known fingerprint, or learned
            if site is None:
                site = site_id(uc, t)
                recs[site] = _new_site(site, uc, t, d["markers"], b.sample_request(site=d["site"]))
            mapping[d["site"]] = site
            rec = recs[site]
            if site != d["site"]:                      # learned this day: follow its template
                known[site] = t
                _version(rec, t, day)
                if not rec.get("new"):                 # a known call-site its fingerprint missed: new phrases (healing)
                    rec["relearned"], rec["day_markers"] = True, list(d["markers"] or [])
            elif not rec["versions"]:
                rec["versions"].append({"version": site_id("v", t), "first": day, "last": day})
            else:
                rec["versions"][-1]["last"] = max(rec["versions"][-1]["last"], day)
            if d["site"] in layout:
                rec.update({k: layout[d["site"]][k] for k in ("static_chars", "dynamic_chars", "static_after", "tool_sets")})
    # the day's numbers, by call-site
    rows = {}                                              # (site, day, model) -> the day's numbers
    for r in ledger:                                       # two of the day's groups can be one call-site: added up
        merge(rows, mapping[r["site"]], day, r["model"] or "", r)
    for g in gaps:
        merge(rows, mapping[g["site"]], day, g["model"] or "", {}, {g["bucket"]: g})
    samples = [{**p, "site": mapping[p["site"]], "day": day} for p in pool]
    placed = _place_leftovers(leftover, recs, rows, day, b)
    for site, rec in recs.items():                      # new or relearned call-sites: phrases their siblings lack
        if rec.get("new") or rec.get("relearned"):
            sibs = [set(r["template"]) for s, r in recs.items() if s != site and (r["usecase"] or "") == (rec["usecase"] or "")]
            rec["markers"], rec["ambiguous"] = distinct_markers(rec.get("day_markers") or rec["markers"], set(rec["template"]), sibs)
    store.replace_coverage([day], b.coverage())
    store.replace_days([day], [r for r in rows.values() if r["day"] == day])
    store.add_to_days([r for r in rows.values() if r["day"] != day])   # calls of earlier days placed only now
    store.replace_samples([day], samples)
    store.replace_unplaced([day], [r for r in leftover if r["id"] not in placed])
    _save(recs, by_uc.keys())
    store.mark_day(day, sum(d["n"] for d in day_sites) + len(placed), n_left - len(placed))
    tasks.note("%s: cache findings" % day)
    _cache(recs)
    tasks.note("%s: done%s" % (day, " (" + b.timing + ")" if getattr(b, "timing", None) else ""))
    _queue_quality(day, day_sites, mapping, recs, b)   # the audit of the audit; it closes the session when done
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


def _queue_quality(day, day_sites, mapping, recs, b):
    """Queue the day's audit of the audit: the call-sites to spot-check and where their calls sit in this session.
    The session stays open for it (it closes it); with the spot check off, it is closed here."""
    from modules import quality
    n = quality.sites_per_day()
    by_site, calls = {}, {}
    for d in day_sites:
        by_site.setdefault(mapping[d["site"]], []).append(d["site"])
        calls[mapping[d["site"]]] = calls.get(mapping[d["site"]], 0) + d["n"]
    sample = {}
    for site in quality.pick(recs, calls, n) if n else []:
        uc = recs[site]["usecase"] or ""
        sib = max((s for s in calls if s != site and (recs.get(s, {}).get("usecase") or "") == uc), key=lambda s: calls[s], default=None)
        sample[site] = {"usecase": uc, "day_sites": by_site[site], "sibling_day_site": by_site[sib][0] if sib else None}
    if not sample:
        b.close()
    tasks.add("quality_day", {"day": day, "sample": sample, "session_id": b.s.id if sample else None}, key="quality:" + day)


def _place_leftovers(leftover, recs, rows, day, b):
    """The day's unplaced calls plus the ones kept from earlier days, by usecase: a call holding a known call-site's
    template joins it; the rest are grouped (group_leftovers); a group matching no call-site is a new one. The calls
    placed add their numbers to the day rows (of their own day). -> {id: site} of the calls placed."""
    placed, by_uc = {}, defaultdict(dict)
    for r in leftover:
        by_uc[r["usecase"] or ""][r["id"]] = r
    for uc in list(by_uc):
        for r in store.unplaced_rows(uc):
            by_uc[uc].setdefault(r["id"], r)
    for uc, calls in by_uc.items():
        known = {r["site"]: set(r["template"]) for r in recs.values() if (r["usecase"] or "") == uc}
        markers_of = {r["site"]: set(r.get("markers") or []) for r in recs.values() if (r["usecase"] or "") == uc}
        rest = {}
        for i, r in calls.items():
            w = set(r["template"])
            site = next((s for s, m in markers_of.items() if m and len(w & m) >= min(build.HITS, len(m))), None)
            if site:
                placed[i] = site
            elif w:
                rest[i] = w
        for t, markers, ids in group_leftovers(rest):
            site = link(t, known)
            if site is None:
                site = site_id(uc, t)
                recs[site] = _new_site(site, uc, t, markers, b.sample_request(call_id=ids[0]))
                recs[site]["versions"] = [{"version": site_id("v", t), "first": min(str(calls[i]["day"]) for i in ids), "last": day}]
                known[site], markers_of[site] = t, set(markers)
            for i in ids:
                placed[i] = site
        for i, r in calls.items():
            if i in placed:                                # one call, on its own day
                merge(rows, placed[i], str(r["day"]), r["model"] or "",
                      {**{k: r.get(k) for k in ("prompt_tokens", "cached_tokens", "written_tokens", "completion_tokens", "cost",
                                                 "cost_nocache")}, "calls": 1, "calls_cached": int((r.get("cached_tokens") or 0) > 0)})
    today_ids = {r["id"] for r in leftover}
    store.drop_unplaced([i for i in placed if i not in today_ids])
    return placed


def _save(recs, usecases):
    """Trim the history to the window, then write every call-site's identity and queue profiles for the new ones."""
    cutoff = str(_today() - datetime.timedelta(window()))
    store.drop_sites(store.trim(cutoff))
    summary = store.site_summary()
    for rec in recs.values():
        if rec["site"] not in summary:
            continue
        calls, first, last = summary[rec["site"]]
        rec.update(calls=calls, first_day=first, last_day=last)
        for v in rec["versions"]:
            v["first"], v["last"] = max(v["first"], first) if v["first"] else first, v["last"] or last
        store.save_identity(rec, window())
        if rec.get("request") and not profiler.get(rec["site"]):
            system, tools, first_user = _texts(rec["request"])
            tasks.add("profile_site", {"site": rec["site"], "system_prompt": system, "tools": tools, "first_user": first_user},
                      key="profile:" + rec["site"])
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
