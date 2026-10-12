"""Model audits: the public API (/api/audit/models). Which models hold where, and which is cheapest and fastest.

  POST /api/audit/models                     -> 202 {audits: [{id, status, items, url}]}  every route needs X-API-Key
       scope: usecases (one audit each, sampled from the log), site (one call-site), or requests (yours: logged
       ids or OpenAI-format bodies); strategy and models choose the candidates
  GET  /api/audit/models/{id}                -> status, progress, spend, the per-candidate summary, routing, verdicts
                                                per call-site. Light, for polling; ETag + If-None-Match -> 304
  POST /api/audit/models/{id}/resume         -> a failed or stopped audit runs its unfinished requests; 409 otherwise
  GET  /api/audit/models/{id}/results        -> finished requests' full results, paged and filtered
  GET  /api/audit/models/{id}/results/{req}  -> one request's full result
  GET  /api/audit/models                     -> audits, newest first; &with_summary=1 adds each one's summary
  GET  /api/audit/models/strategies|estimate -> the strategies and engine settings; a rough cost before starting
  GET  /api/audit/models/{id}/results.csv|json -> downloads (for the web UI; not in /openapi.json)

With "callback_url", the audit's status is POSTed there once it ends. The UI, the router and release pipelines all
use these routes; the measurement underneath (validation/) is internal."""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.web import public
from app.web.public import MAX_ITEMS, RequestIn, ResumeIn, require_key
from core.identity import store
from modules.models import audits
from validation import jobs

api = APIRouter(prefix="/api/audit/models", dependencies=[Depends(require_key)], tags=["model audits"],
                responses={401: {"description": "Missing or wrong X-API-Key"},
                           503: {"description": "The server has no VALIDATION_API_KEY, or a BigQuery lookup failed"}})


class AuditIn(BaseModel):
    """One of: `usecases` (one audit each, requests sampled per call-site from the log), `site` (one call-site), or
    `requests` (yours). Candidates: the `strategy`'s picks per request plus the `models` you name."""
    model_config = ConfigDict(json_schema_extra={"examples": [
        {"usecases": ["uc-ticket-summary"], "strategy": "cheaper"},
        {"usecases": ["uc-ticket-summary", "uc-order-triage"], "strategy": "release", "models": ["gpt-5.5"],
         "max_budget_usd": 20},
        {"requests": [{"request_id": "chatcmpl-ET2KW1d347oy8pFVy3NbMzm3liZzC"},
                      {"id": "refund-17", "request": {"model": "gpt-5.4", "max_tokens": 1024, "messages": [
                          {"role": "system", "content": "You are a billing agent."},
                          {"role": "user", "content": "Refund order 17, it arrived broken."}]},
                       "response": {"model": "gpt-5.4", "choices": [{"message": {
                           "role": "assistant", "content": "I've started a refund for order 17."}}],
                           "usage": {"prompt_tokens": 31, "completion_tokens": 12}}}],
         "models": ["gpt-5.4-mini", "jev-router"], "callback_url": "https://example.internal/hooks/audit"}]})
    usecases: list[str] = Field(default_factory=list, description="Usecase ids; 'None' names the untagged usecase")
    site: Optional[str] = Field(default=None, description="One call-site id")
    requests: list[RequestIn] = Field(default_factory=list, max_length=MAX_ITEMS,
                                      description="Your requests, up to %d" % MAX_ITEMS)
    strategy: Optional[str] = Field(default=None, description="See GET /api/audit/models/strategies; defaults to "
                                    "'cheaper', or 'manual' for your requests")
    models: list[str] = Field(default_factory=list, description="Models to try besides the strategy's picks; "
                                                                "required for 'manual' and 'release'")
    samples: int = Field(default=audits.DEFAULT_SAMPLES, ge=1, le=50, description="Requests per call-site (sampled scopes)")
    max_budget_usd: Optional[float] = Field(default=None, gt=0, description="Per audit; no cap when left out")
    callback_url: Optional[HttpUrl] = Field(default=None, description="POSTed the audit's status once, when it ends")
    trigger: str = Field(default="api", description="Who starts it: user, release, router, api, schedule")
    judge_model: Optional[str] = Field(default=None, description="The judge; defaults to the server's")


def _uc(u):
    return None if u in (None, "None") else u


def _audit(audit_id):
    a = audits.get(audit_id)
    if not a:
        raise HTTPException(404, "no such audit")
    return audits.refresh_status(a)


def _view(a):
    """An audit with its job's progress, in the public Audit shape."""
    job = jobs.get(a["job_id"]) if a["job_id"] else None
    names = {s["site"]: (s["profile"] or {}).get("name") or s["site"] for s in store.all_sites()} if a["samples"] else {}
    return {"id": a["id"], "status": a["status"], "created_at": a["created_at"], "updated_at": job and job["updated_at"],
            "trigger": a["trigger"], "scope": a["scope"], "usecase": a["usecase"], "site": a["site"],
            "strategy": a["strategy"], "models": a["candidates"],
            "items_total": job["items_total"] if job else 0, "items_done": job["items_done"] if job else 0,
            "spend_usd": job["spend_usd"] if job else 0.0, "max_budget_usd": a["max_budget_usd"],
            "judge_model": job and job["judge_model"], "stop_reason": job and job.get("stop_reason"),
            "error": a["error"] or (job and job.get("error")), "workers": job["workers"] if job else 0,
            "unpriced_calls": job["unpriced_calls"] if job else 0, "spec": job and job["spec"],
            "samples": a["samples"], "verdicts": a["verdicts"],
            "site_names": {st: names.get(st, st) for st in a["samples"] if st}}


def _status_body(a):
    v = _view(a)
    jid = a["job_id"]
    return {**v, "summary": jobs.summary(jid) if jid else [], "routing": jobs.routing(jid) if jid else [],
            "callback": jobs.callback(jid) if jid else None, "results_url": "/api/audit/models/%s/results" % a["id"]}


def _callback_body(job_id):
    a = audits.for_job(job_id)
    return _status_body(audits.refresh_status(a)) if a else None


jobs.callback_body = _callback_body


@api.get("/strategies", summary="The strategies and the engine's settings")
def strategies():
    from app.services.downgrade_refset import spec as app_spec
    sp = app_spec()
    return {"strategies": [{"id": k, "does": v} for k, v in audits.STRATEGIES.items()],
            "spec": {"reference_runs": sp.reference_runs, "first_runs": sp.first_runs, "candidate_runs": sp.candidate_runs,
                     "votes": sp.votes, "margin": sp.margin, "coherence_floor": sp.coherence_floor, "judge_model": sp.judge_model},
            "samples": audits.DEFAULT_SAMPLES, "valid_days": audits.VALID_DAYS}


@api.get("/estimate", summary="A rough cost before starting a sampled audit")
def estimate(usecase: Optional[str] = None, site: Optional[str] = None, samples: int = audits.DEFAULT_SAMPLES,
             candidates: int = 1):
    """null when the scope's call-sites aren't known yet (never analysed or audited): the audit finds them."""
    known = bool(store.get(site)) if site else bool(store.by_usecase(_uc(usecase)))
    return {"usd": audits.estimate(_uc(usecase), site, samples, max(1, candidates)) if known else None}


@api.get("/preview", summary="What a strategy will try, per known call-site")
def preview(strategy: str = "cheaper", usecases: str = "", site: Optional[str] = None, models: str = ""):
    """For each call-site already known in the scope, its current models and the candidates the strategy picks for
    them (served models only). Call-sites not known yet are found by the audit itself."""
    if strategy not in audits.STRATEGIES:
        raise HTTPException(422, "strategy must be one of %s" % ", ".join(audits.STRATEGIES))
    asked = [m for m in models.split(",") if m]
    recs = [store.get(site)] if site else [r for u in usecases.split(",") if u for r in store.by_usecase(_uc(u))]
    out = []
    for r in recs:
        if not r:
            continue
        for m in sorted(r["series"]):
            out.append({"usecase": r["usecase"], "site": r["site"], "name": (r["profile"] or {}).get("name") or r["site"],
                        "model": m, "candidates": audits.candidates_for(m, strategy, asked, rec=r)})
    unknown = [u for u in usecases.split(",") if u and not store.by_usecase(_uc(u))]
    return {"call_sites": out, "not_known_yet": unknown}


@api.post("", status_code=202, summary="Start model audits", responses={
    202: {"model": public.Submitted}, 422: {"description": "Refused whole; `detail` lists every problem"}})
def start(body: AuditIn):
    """Usecases or a call-site: sampled from the log in the background (status `sampling`, then `queued`). Your
    requests: checked at once (named request_ids are looked up in BigQuery) and queued; nothing starts unless every
    request is usable."""
    scopes = sum(bool(x) for x in (body.usecases, body.site, body.requests))
    if scopes != 1:
        raise HTTPException(422, "give exactly one scope: usecases, site or requests")
    strategy = body.strategy or ("manual" if body.requests else "cheaper")
    if strategy not in audits.STRATEGIES:
        raise HTTPException(422, "strategy must be one of %s" % ", ".join(audits.STRATEGIES))
    if body.trigger not in audits.TRIGGERS:
        raise HTTPException(422, "trigger must be one of %s" % ", ".join(audits.TRIGGERS))
    own = body.requests and all(r.candidates for r in body.requests)
    if strategy in ("release", "manual") and not body.models and not own:
        raise HTTPException(422, "%s needs model names" % strategy)
    cb = str(body.callback_url) if body.callback_url else None
    started = []
    if body.requests:
        items = public.parse_requests(body.requests, lambda m: [{"label": c, "model": c} for c in
                                                                 audits.candidates_for(m, strategy, body.models)])
        started.append(audits.start_requests(items, strategy, body.models, body.max_budget_usd, cb, body.trigger,
                                             body.judge_model))
    else:
        kw = dict(strategy=strategy, asked=body.models, auto=False, n=body.samples, max_budget_usd=body.max_budget_usd,
                  trigger=body.trigger, judge_model=body.judge_model, callback_url=cb)
        if body.site:
            rec = store.get(body.site)
            if not rec:
                raise HTTPException(404, "unknown call-site")
            started.append(audits.start(usecase=rec["usecase"], site=body.site, **kw))
        else:
            for u in body.usecases:
                if not store.get_usecase(_uc(u)):
                    raise HTTPException(404, "unknown usecase %s" % u)
            started += [audits.start(usecase=_uc(u), site=None, **kw) for u in body.usecases]
    return {"audits": [{"id": a["id"], "status": a["status"],
                        "items": sum(len(v) for v in a["samples"].values()) if a["scope"] == "requests" else 0,
                        "url": "/api/audit/models/" + a["id"]} for a in started]}


@api.get("", summary="Audits, newest first", responses={200: {"model": list[public.Listed]}})
def listing(page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=200), with_summary: bool = False,
            usecase: Optional[str] = None, site: Optional[str] = None):
    found = [audits.refresh_status(a) for a in audits.recent(per_page, _uc(usecase) if usecase else None, site,
                                                            offset=(page - 1) * per_page)]
    out = [_view(a) for a in found]
    if with_summary:
        by = jobs.summaries([a["job_id"] for a in found if a["job_id"]])
        out = [{**v, "summary": by.get(a["job_id"], [])} for v, a in zip(out, found)]
    return out


@api.get("/{audit_id}", summary="Status, summary and verdicts", responses={
    200: {"model": public.Status}, 304: {"description": "Unchanged since the ETag sent as If-None-Match"},
    404: {"description": "No such audit"}})
def status(audit_id: str, if_none_match: Optional[str] = Header(None)):
    """Progress, spend, the per-candidate summary and, once it ended, the verdicts per call-site. Small enough to
    poll; the callback sends the same body."""
    a = _audit(audit_id)
    job = jobs.get(a["job_id"]) if a["job_id"] else None
    cb = jobs.callback(a["job_id"]) if a["job_id"] else None
    tag = 'W/"%s-%d-%d-%s-%s"' % (a["status"], job["items_done"] if job else 0, job["workers"] if job else 0,
                                  job and job["updated_at"], cb and "%s%s" % (cb["sent_at"], bool(cb["error"])))
    if if_none_match == tag:
        return Response(status_code=304, headers={"ETag": tag})
    return JSONResponse(_status_body(a), headers={"ETag": tag})


def _job_of(audit_id):
    a = _audit(audit_id)
    if not a["job_id"]:
        raise HTTPException(409, "still sampling: no requests yet")
    return a["job_id"]


@api.post("/{audit_id}/resume", summary="Resume a stopped or failed audit", responses={
    200: {"model": public.Audit}, 404: {"description": "No such audit"},
    409: {"description": "Not stopped or failed, or its budget is spent: send a higher max_budget_usd"}})
def resume(audit_id: str, body: Optional[ResumeIn] = None):
    """Its unfinished requests run; finished ones are kept. The callback is sent again when it ends."""
    job = jobs.resume(_job_of(audit_id), body.max_budget_usd if body else None)
    if job is None:
        raise HTTPException(409, "only a failed or stopped audit can be resumed; one stopped by its budget needs a "
                                 "higher max_budget_usd")
    if body and body.max_budget_usd:
        audits._set(audit_id, max_budget_usd=body.max_budget_usd)
    return _view(audits.refresh_status(audits.get(audit_id)))


@api.get("/{audit_id}/results", summary="Results per request", responses={
    200: {"model": public.ResultsPage}, 404: {"description": "No such audit"}})
def results(audit_id: str, page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=200),
            show: Literal["all", "problems", "split"] = Query(
                "all", description="problems: a candidate is outside or unverified; split: candidates disagree"),
            label: Optional[str] = Query(None, description="With verdict: requests where this candidate has it"),
            verdict: Optional[public.Verdict] = None, q: str = Query("", description="Part of a request id")):
    """Finished requests in order, each with every answer and the judges' reasons."""
    total, res = jobs.page(_job_of(audit_id), page, per_page, show=show, label=label, verdict=verdict, q=q)
    return {"page": page, "per_page": per_page, "total": total, "results": res}


@api.get("/{audit_id}/results/{request_id:path}", summary="One request's result", responses={
    200: {"model": public.Result}, 404: {"description": "No such audit, or the request has not finished"}})
def one_result(audit_id: str, request_id: str):
    """Everything about one request, including what was sent."""
    if request_id in ("results.csv", "results.json"):
        raise HTTPException(404)
    got = jobs.request(_job_of(audit_id), request_id)
    if not got or got[1] is None:
        raise HTTPException(404, "no finished request %r in this audit" % request_id)
    trace = got[0]["request"]
    return {**got[1], "request": {"messages": [{"role": m["role"], "content": m["content"]}
                                               for m in trace.get("input_messages", [])],
                                  "tools": [t[0] for t in trace.get("tools_defined", [])]}}


@api.get("/{audit_id}/results.json", include_in_schema=False)
def download_json(audit_id: str):
    import json
    a = _audit(audit_id)
    body = {**_status_body(a), "results": jobs.results(a["job_id"]) if a["job_id"] else []}
    return Response(json.dumps(body, indent=2), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="%s.json"' % audit_id})


@api.get("/{audit_id}/results.csv", include_in_schema=False)
def download_csv(audit_id: str):
    """One row per request x candidate: what a spreadsheet needs to compare candidates."""
    import csv
    import io
    job_id = _job_of(audit_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["request_id", "original_model", "original_consistency", "original_avg_latency_ms",
                "original_avg_cost_usd", "candidate", "candidate_model", "served_model", "verdict", "fit", "fit_rate",
                "avg_latency_ms", "avg_cost_usd", "reason", "judge_reason", "match", "acceptable", "drift", "unclear"])
    ms = lambda v: None if v is None else round(v)
    orig = {}
    for row in jobs.table(job_id):
        if row["label"] == "original":
            orig = row
            continue
        w.writerow([row["item_id"], orig["model"], "%s/%s" % ("?" if orig["kept"] is None else orig["kept"], orig["runs"]),
                    ms(orig["latency_ms"]), orig["cost_usd"], row["label"], row["model"], row["served_model"],
                    row["verdict"], "%d/%d" % (row["kept"], row["runs"]),
                    round(row["kept"] / row["runs"], 3) if row["runs"] else None, ms(row["latency_ms"]),
                    row["cost_usd"], row["reason"], row["judge_reason"] or "",
                    *(row.get(k) for k in ("match", "acceptable", "drift", "unclear"))])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="%s.csv"' % audit_id})
