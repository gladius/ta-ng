"""The audit of the audit: after a day is processed, how far to trust it.

Automatic checks (our own numbers, no model):
  consistency   a usecase's calls = placed + not placed + other format + logged twice
  placement     the share of a usecase's chat calls placed in a call-site (flag under PLACED_MIN)
  fingerprint   call-sites with no phrases, or ambiguous ones (phrases also in a sibling)
  models        call-sites sent to several models that day (routing, fallback, or two call-sites merged)
  volume        call-sites whose calls jumped or fell by VOLUME_X against their previous day
  unpriced      the share of a usecase's calls with no price (flag over UNPRICED_MAX)
A model spot check on up to SITES call-sites a day (the busiest, then some at random): are its sampled calls one
prompt template, is a sibling call-site's call a different one, are its fingerprint phrases fixed instructions or
inserted data. It reads the calls the day script kept (no extra read of the log); only verdicts and reasons are
stored, never prompt text."""
import datetime
import json
import random

import credentials
from sqlalchemy import Boolean, Column, DateTime, String, Table, Text, delete, select

from app.services import db

PLACED_MIN = 0.9
UNPRICED_MAX = 0.05
VOLUME_X = 5
TEXT_CAP = 6000            # characters of each sampled call shown to the model

checks = Table(
    "quality_checks", db.metadata,
    Column("day", String(10), primary_key=True),
    Column("kind", String(16), primary_key=True),
    Column("usecase", Text, primary_key=True),
    Column("site", String(32), primary_key=True),          # "" for a usecase-level check
    Column("ok", Boolean, nullable=False),
    Column("detail", Text),                                # JSON
    Column("at", DateTime, nullable=False),
)


def sites_per_day():
    """QUALITY_SITES (default 20; 0 turns the model spot check off)."""
    return int(credentials.get_config("QUALITY_SITES") or 20)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def pick(recs, day_calls, n):
    """The call-sites to spot-check: the busiest half, then the rest at random (the same pick for the same day)."""
    ranked = sorted(day_calls, key=lambda s: -day_calls[s])
    busy = ranked[:n // 2]
    rest = [s for s in ranked[n // 2:] if s in recs]
    random.Random(str(sorted(day_calls))[:64]).shuffle(rest)
    return busy + rest[:n - len(busy)]


# ----------------------------------------------------------------------------------------------- automatic checks
def automatic(day):
    """The checks on our own numbers for the day -> rows for quality_checks."""
    from core.identity import store
    out = []
    eng = db.ready()
    with eng.connect() as c:
        cov = c.execute(select(store.usecase_days).where(store.usecase_days.c.day == day)).all()
        sites = {r.site: r for r in c.execute(select(store.callsites.c.site, store.callsites.c.usecase, store.callsites.c.markers,
                                                      store.callsites.c.ambiguous))}
        rows = c.execute(select(store.site_days.c.site, store.site_days.c.day, store.site_days.c.model, store.site_days.c.calls)
                         .where(store.site_days.c.day <= day)).all()
    for r in cov:
        chat = r.calls - r.other_format - r.repeated
        out.append(("consistency", r.usecase, "", r.calls == r.placed + r.unplaced + r.other_format + r.repeated,
                    {"calls": r.calls, "placed": r.placed, "unplaced": r.unplaced, "other_format": r.other_format, "repeated": r.repeated}))
        if chat > 0:
            share = r.placed / chat
            out.append(("placement", r.usecase, "", share >= PLACED_MIN, {"placed": r.placed, "chat_calls": chat, "share": round(share, 3)}))
            out.append(("unpriced", r.usecase, "", r.unpriced <= UNPRICED_MAX * chat, {"unpriced": r.unpriced, "share": round(r.unpriced / chat, 3)}))
    today, before = {}, {}
    models = {}
    prev_day = max((d for _, d, _, _ in rows if d < day), default=None)
    for site, d, model, calls in rows:
        if d == day:
            today[site] = today.get(site, 0) + (calls or 0)
            models.setdefault(site, {})[model] = models.setdefault(site, {}).get(model, 0) + (calls or 0)
        elif d == prev_day:
            before[site] = before.get(site, 0) + (calls or 0)
    for site, n in today.items():
        rec = sites.get(site)
        uc = rec.usecase if rec else ""
        phrases = json.loads(rec.markers or "[]") if rec else []
        if not phrases or (rec and rec.ambiguous):
            out.append(("fingerprint", uc, site, False, {"phrases": len(phrases), "ambiguous": bool(rec and rec.ambiguous)}))
        mix = {m: k for m, k in models.get(site, {}).items() if k >= 0.1 * n}
        if len(mix) > 1:
            out.append(("models", uc, site, True, {"calls": {m: int(k) for m, k in sorted(mix.items(), key=lambda kv: -kv[1])}}))
        b = before.get(site)
        if prev_day and b and (n > VOLUME_X * b or n * VOLUME_X < b):
            out.append(("volume", uc, site, False, {"today": int(n), "previous_day": int(b)}))
    return out


# ----------------------------------------------------------------------------------------------- model spot check
_PROMPT = """You are checking how an auditing system grouped the LLM calls of one application. It groups calls into
"call-sites": one prompt template sent from one place in the code, whose per-call parts (names, ids, dates, account
details, retrieved documents, the user's data) vary from call to call. Below is the head of some requests (the tool
names, then the system prompt, possibly cut).

A. Calls the system put in ONE call-site{models}:
{same}

B. One call from a DIFFERENT call-site of the same application (or "none"):
{sibling}

C. The 5 phrases the system uses to recognise call-site A (lower case, numbers shown as #):
{phrases}

Answer with one json object:
- "same": true if every call in A is the same prompt template with different per-call data; false if A mixes
  different templates.
- "same_why": one sentence.
- "different": true if B is a different template from A; false if B is really the same template as A; null if B is "none".
- "different_why": one sentence, or "".
- "phrases": an object mapping each phrase in C to "fixed" (part of the template's fixed instructions) or "data"
  (comes from per-call or inserted data, e.g. a customer's details, a date, a retrieved document).
Only the json object."""


def _model():
    from core import profiler
    return profiler.model()


def ask(same_calls, sibling_call, phrases):
    """One model call for one call-site -> the parsed verdict."""
    from app.services import llm_client
    models = sorted({c["model"] for c in same_calls if c.get("model")})
    same = "\n\n".join("--- call %d%s ---\n%s" % (i + 1, " (model %s)" % c["model"] if len(models) > 1 else "", (c["sys"] or "")[:TEXT_CAP])
                       for i, c in enumerate(same_calls))
    prompt = _PROMPT.format(models=" (sent to several models: %s)" % ", ".join(models) if len(models) > 1 else "",
                            same=same, sibling=(sibling_call["sys"] or "")[:TEXT_CAP] if sibling_call else "none",
                            phrases="\n".join("- %s" % p for p in phrases) or "(none)")
    r = llm_client.complete(model=_model(), max_tokens=800, response_format={"type": "json_object"},
                            messages=[{"role": "user", "content": prompt}])
    txt = llm_client.text(r).strip()
    return json.loads(txt[txt.find("{"):txt.rfind("}") + 1], strict=False)


def run(day, sample, session_id=None):
    """The day's checks. `sample`: {site: {"usecase", "day_sites": [...], "sibling_day_site": ... or None}}: the call-
    sites picked for the model spot check and where their calls are in the day's BigQuery session (`session_id`)."""
    from core.identity import store
    from core.ledger import build
    rows = automatic(day)
    if sample and session_id:
        b = build.Build(day, day, session_id=session_id)
        try:
            if b.resumed:
                wanted = {ds for v in sample.values() for ds in v["day_sites"] + ([v["sibling_day_site"]] if v.get("sibling_day_site") else [])}
                calls = {}
                for c in b.site_calls(sorted(wanted)):
                    calls.setdefault(c["site"], []).append(c)
                for site, v in sample.items():
                    own = [c for ds in v["day_sites"] for c in calls.get(ds, [])]
                    sib = (calls.get(v.get("sibling_day_site")) or [None])[0]
                    rec = store.get(site) or {}
                    if not own:
                        continue
                    try:
                        verdict = ask(own[:4], sib, rec.get("markers") or [])
                    except Exception as e:
                        rows.append(("model_check", v["usecase"], site, False, {"error": "%s: %s" % (type(e).__name__, str(e)[:200])}))
                        continue
                    data = sorted(p for p, kind in (verdict.get("phrases") or {}).items() if kind == "data")
                    ok = verdict.get("same") is not False and verdict.get("different") is not False and len(data) <= 2
                    rows.append(("model_check", v["usecase"], site, ok,
                                 {"same": verdict.get("same"), "same_why": verdict.get("same_why"), "different": verdict.get("different"),
                                  "different_why": verdict.get("different_why"), "data_phrases": data, "calls_shown": len(own[:4]),
                                  "models": sorted({c["model"] for c in own if c.get("model")})}))
        finally:
            b.close()
    save(day, rows)
    return len(rows)


def save(day, rows):
    eng = db.ready()
    now = _now()
    with eng.begin() as c:
        c.execute(delete(checks).where(checks.c.day == day))
        for kind, usecase, site, ok, detail in rows:
            c.execute(checks.insert().values(day=day, kind=kind, usecase=usecase or "", site=site or "", ok=bool(ok),
                                             detail=json.dumps(detail), at=now))


def report(day=None):
    """The latest (or given) day's checks: totals per kind and every failure, plus the model spot checks."""
    eng = db.ready()
    with eng.connect() as c:
        day = day or c.execute(select(checks.c.day).order_by(checks.c.day.desc()).limit(1)).scalar()
        if not day:
            return None
        rows = c.execute(select(checks).where(checks.c.day == day)).all()
    out = {"day": day, "kinds": {}, "failures": [], "spot_checks": []}
    for r in rows:
        k = out["kinds"].setdefault(r.kind, {"checked": 0, "failed": 0})
        k["checked"] += 1
        k["failed"] += 0 if r.ok else 1
        item = {"kind": r.kind, "usecase": r.usecase or None, "site": r.site or None, "ok": r.ok, "detail": json.loads(r.detail or "{}")}
        if r.kind == "model_check":
            out["spot_checks"].append(item)
        elif not r.ok:
            out["failures"].append(item)
    return out
