"""Validation API and pages: submit a batch of requests with candidate models, follow it, read the results.

  POST /api/validations                       -> 202 {id, status, items, url}   every /api route needs X-API-Key
  GET  /api/validations/{id}                  -> status, progress, spend, per-candidate summary, routing breakdown,
                                                 callback delivery. Light, for polling; sends an ETag, so
                                                 If-None-Match -> 304 if unchanged
  POST /api/validations/{id}/resume           -> a failed or stopped job runs its unfinished requests; optional
                                                 {"max_budget_usd": n} raises its budget. 409 if it is neither
  GET  /api/validations/{id}/results          -> finished requests' full results, paged: ?page=&per_page= (max 200)
                                                 &show=all|problems|split &label=&verdict= &q= (in the request id)
  GET  /api/validations/{id}/results/{req}    -> one request's full result
  GET  /api/validations                       -> jobs, newest first: ?page=&per_page=, &with_summary=1 adds each
                                                 job's per-candidate summary
  GET  /validations[/{id}[/requests/{req}]]   -> the same as pages

With "callback_url", the job's status and summary are POSTed there once it ends (done, stopped or failed).

Requests and responses are OpenAI-format bodies, as LiteLLM logs them. Instead of sending them, an item can name a
LiteLLM call by "request_id": the batch's ids are looked up in BigQuery (BQ_* settings) in one query at submit, and
the logged request and response are used, the original being the model that answered. Candidate labels belong to
the caller (a router, a release); each candidate is compared with the original model and no winner is picked.
"""
import dataclasses
import hmac
from collections import Counter
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field, HttpUrl

import credentials
from validation import jobs

MAX_ITEMS = 1000
MAX_CANDIDATES = 5
PER_PAGE = 50               # requests per page in the UI


def require_key(x_api_key: Optional[str] = Header(None)):
    expected = credentials.get_secret("VALIDATION_API_KEY")
    if not expected:
        raise HTTPException(503, "VALIDATION_API_KEY is not set on the server")
    if not x_api_key or not hmac.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(401, "missing or wrong X-API-Key")


api = APIRouter(prefix="/api", dependencies=[Depends(require_key)])
pages = APIRouter()


class CandidateIn(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=120)


class ItemIn(BaseModel):
    id: Optional[str] = Field(default=None, min_length=1)   # defaults to request_id
    request: Optional[dict] = None          # OpenAI chat request: model, messages, tools?, tool_choice?, max_tokens?
    response: Optional[dict] = None         # the original's OpenAI chat completion; omitted -> it is re-run once more
    request_id: Optional[str] = Field(default=None, min_length=1, max_length=200)  # or: a logged call, from BigQuery
    candidates: list[CandidateIn] = Field(min_length=1, max_length=MAX_CANDIDATES)


class JobIn(BaseModel):
    items: list[ItemIn] = Field(min_length=1, max_length=MAX_ITEMS)
    judge_model: Optional[str] = None
    max_budget_usd: Optional[float] = Field(default=None, gt=0)     # none: no budget, the job runs to the end
    callback_url: Optional[HttpUrl] = None


class ResumeIn(BaseModel):
    max_budget_usd: Optional[float] = Field(default=None, gt=0)


def _logged(items):
    """{request_id: (request, response)} for the items sent by request_id only: one BigQuery lookup per batch."""
    from connectors.bigquery import source
    wanted = {it.request_id for it in items if it.request is None and it.request_id}
    if not wanted:
        return {}
    if not source.SOURCE.configured():
        raise HTTPException(422, ["items sent by request_id need BigQuery: set BQ_TABLE, BQ_REQUEST_COLUMN and "
                                  "BQ_RESPONSE_COLUMN on the server"])
    try:
        return source.lookup(wanted)
    except Exception as e:
        raise HTTPException(503, "BigQuery lookup failed: %s" % str(e)[:300])


def _items(body):
    """Validated items in the engine's shape; every problem in the batch is reported at once, nothing is started."""
    from connectors.bigquery.source import to_trace         # OpenAI request/response -> contract trace
    out, problems, ids = [], [], set()
    logged = _logged(body.items)
    for n, it in enumerate(body.items):
        it.id = it.id or it.request_id
        if it.request is None:
            if not it.request_id:
                problems.append("item %d: send request (and response) or request_id" % (n + 1))
                continue
            if it.request_id not in logged:
                problems.append("%s: request_id not found in BigQuery" % it.id)
                continue
            it.request, it.response = logged[it.request_id]
            if it.response is None:
                problems.append("%s: the logged call has no response (it failed)" % it.id)
                continue
        elif not it.id:
            problems.append("item %d: id is required" % (n + 1))
            continue
        labels = [c.label for c in it.candidates]
        if it.id in ids:
            problems.append("%s: duplicate item id" % it.id)
        if len(set(labels)) != len(labels):
            problems.append("%s: candidate labels must be unique" % it.id)
        if "original" in labels:
            problems.append("%s: candidate label 'original' is reserved for the original model" % it.id)
        model = it.request.get("model")
        if not model:
            problems.append("%s: request.model is required" % it.id)
        elif not isinstance(model, str) or len(model) > 120:
            problems.append("%s: request.model must be a model name of at most 120 characters" % it.id)
        ids.add(it.id)
        trace = to_trace(it.request, it.response or {}, "api")
        if not any(m["role"] == "user" for m in trace["input_messages"]):
            problems.append("%s: request.messages needs a user message" % it.id)
        out.append({"id": it.id, "request": trace, "model": it.request.get("model"),
                    "recorded": (trace["output"] or None) if it.response else None,
                    "candidates": [c.model_dump() for c in it.candidates]})
    if problems:
        raise HTTPException(422, problems)
    return out


@api.post("/validations", status_code=202)
def submit(body: JobIn):
    from app.services.downgrade_refset import spec as app_spec
    spec = app_spec()
    if body.judge_model:
        spec = dataclasses.replace(spec, judge_model=body.judge_model)
    job = jobs.create(_items(body), spec, body.max_budget_usd, str(body.callback_url) if body.callback_url else None)
    return {"id": job["id"], "status": job["status"], "items": job["items_total"],
            "url": "/api/validations/" + job["id"]}


def _known(job_id):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "no such validation")
    return job


@api.get("/validations/{job_id}")
def status(job_id: str, if_none_match: Optional[str] = Header(None)):
    job = _known(job_id)
    cb = jobs.callback(job_id)
    tag = 'W/"%s-%d-%d-%s-%s"' % (job["status"], job["items_done"], job["workers"], job["updated_at"],
                                  cb and "%s%s" % (cb["sent_at"], bool(cb["error"])))
    if if_none_match == tag:
        return Response(status_code=304, headers={"ETag": tag})
    return JSONResponse({**job, "summary": jobs.summary(job_id), "routing": jobs.routing(job_id), "callback": cb,
                         "results_url": "/api/validations/%s/results" % job_id}, headers={"ETag": tag})


@api.post("/validations/{job_id}/resume")
def resume(job_id: str, body: Optional[ResumeIn] = None):
    _known(job_id)
    job = jobs.resume(job_id, body.max_budget_usd if body else None)
    if job is None:
        raise HTTPException(409, "only a failed or stopped validation can be resumed; one stopped by its budget "
                                 "needs a higher max_budget_usd")
    return job


@api.get("/validations/{job_id}/results")
def results(job_id: str, page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=200),
            show: Literal["all", "problems", "split"] = "all", label: Optional[str] = None,
            verdict: Optional[Literal["within", "outside", "unverified"]] = None, q: str = ""):
    _known(job_id)
    total, res = jobs.page(job_id, page, per_page, show=show, label=label, verdict=verdict, q=q)
    return {"page": page, "per_page": per_page, "total": total, "results": res}


@api.get("/validations/{job_id}/results/{request_id:path}")
def one_result(job_id: str, request_id: str):
    got = jobs.request(job_id, request_id)
    if not got or got[1] is None:
        raise HTTPException(404, "no finished request %r in this validation" % request_id)
    return got[1]


@api.get("/validations")
def recent(page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=200), with_summary: bool = False):
    found = jobs.recent(per_page, (page - 1) * per_page)
    return _with_summaries(found) if with_summary else found


def _with_summaries(found):
    by = jobs.summaries([j["id"] for j in found])
    return [{**j, "summary": by.get(j["id"], [])} for j in found]


@pages.get("/validations", response_class=HTMLResponse)
def list_page(request: Request, page: int = Query(1, ge=1)):
    from app.web.server import _page
    found = jobs.recent(PER_PAGE + 1, (page - 1) * PER_PAGE)
    return _page(request, "validations.html", jobs=_with_summaries(found[:PER_PAGE]), page=page,
                 more=len(found) > PER_PAGE)


@pages.get("/validations/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str, page: int = Query(1, ge=1), show: str = "all", q: str = ""):
    """Per-candidate headline, routing breakdown, and one page of the request x candidate grid."""
    from app.web.server import _page
    job = _known(job_id)
    show = show if show in jobs.SHOW else "all"
    summary = jobs.summary(job_id)
    total, res = jobs.page(job_id, page, PER_PAGE, show=show, q=q)
    return _page(request, "validation.html", job=job, summary=summary, routing=jobs.routing(job_id),
                 callback=jobs.callback(job_id),
                 rows=[{"r": r, "cells": {c["label"]: c for c in r["candidates"]}} for r in res],
                 labels=[s["label"] for s in summary if s["label"] != "original"], show=show, q=q,
                 counts={k: jobs.count(job_id, show=k, q=q) for k in jobs.SHOW}, page=page, total=total,
                 pages=max(1, -(-total // PER_PAGE)), live=job["status"] in jobs.LIVE)


@pages.get("/validations/{job_id}/requests/{item_id:path}", response_class=HTMLResponse)
def request_page(request: Request, job_id: str, item_id: str, cand: str = ""):
    """One request: what was sent, the original's own answers, and every candidate's runs with the judge's reasons."""
    from app.web.server import _page
    from app.services.downgrade_refset import evidence_row
    job = _known(job_id)
    got = jobs.request(job_id, item_id)
    if not got or got[1] is None:
        raise HTTPException(404, "no finished request %r in this validation" % item_id)
    submitted, r = got
    lat, cost = jobs.original_averages(r)
    cands = [{"c": c, "row": evidence_row(submitted["request"], r, job["spec"]["cap_chars"], c),
              "served": [m for m, _ in Counter(x["model"] for x in c["runs"]
                                                  if x.get("model") and not x.get("error")).most_common()]}
             for c in r["candidates"]]
    return _page(request, "validation_request.html", job=job, r=r, cands=cands,
                 cand=cand or r["candidates"][0]["label"], orig_latency_ms=lat, orig_cost_usd=cost)


@pages.get("/validations/{job_id}/results.json")
def download_json(job_id: str):
    import json
    _known(job_id)
    return Response(json.dumps(jobs.view(job_id), indent=2), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="%s.json"' % job_id})


@pages.get("/validations/{job_id}/results.csv")
def download_csv(job_id: str):
    """One row per request x candidate: what a spreadsheet needs to compare candidates."""
    import csv
    import io
    _known(job_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["request_id", "original_model", "original_consistency", "original_avg_latency_ms",
                "original_avg_cost_usd", "label", "candidate_model", "served_model", "verdict", "fit", "fit_rate",
                "avg_latency_ms", "avg_cost_usd", "reason", "judge_reason"])
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
                    row["cost_usd"], row["reason"], row["judge_reason"] or ""])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="%s.csv"' % job_id})
