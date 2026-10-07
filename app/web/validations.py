"""Validation API and pages: submit a batch of requests with candidate models, follow it, read the results.

  POST /api/validations         -> 202 {id, status, items, url}            all /api routes need the X-API-Key header
  GET  /api/validations/{id}    -> status, progress, spend, per-label summary, per-request results (partial while running)
  GET  /api/validations         -> recent jobs
  GET  /validations[/{id}]      -> the same as pages, refreshing themselves while a job runs

Requests and responses are OpenAI-format bodies, as LiteLLM logs them. Candidate labels belong to the caller (a
router, a release); each candidate is compared with the original model and no winner is picked.
"""
import dataclasses
import hmac
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import credentials
from validation import jobs

MAX_ITEMS = 1000
MAX_CANDIDATES = 5


def require_key(x_api_key: Optional[str] = Header(None)):
    expected = credentials.get_secret("VALIDATION_API_KEY")
    if not expected:
        raise HTTPException(503, "VALIDATION_API_KEY is not set on the server")
    if not x_api_key or not hmac.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(401, "missing or wrong X-API-Key")


api = APIRouter(prefix="/api", dependencies=[Depends(require_key)])
pages = APIRouter()


class CandidateIn(BaseModel):
    label: str = Field(min_length=1)
    model: str = Field(min_length=1)


class ItemIn(BaseModel):
    id: str = Field(min_length=1)
    request: dict                           # OpenAI chat request: model, messages, tools?, tool_choice?, max_tokens?
    response: Optional[dict] = None         # the original's OpenAI chat completion; omitted -> it is re-run once more
    candidates: list[CandidateIn] = Field(min_length=1, max_length=MAX_CANDIDATES)


class JobIn(BaseModel):
    items: list[ItemIn] = Field(min_length=1, max_length=MAX_ITEMS)
    judge_model: Optional[str] = None
    max_budget_usd: Optional[float] = Field(default=None, gt=0)


def _items(body):
    """Validated items in the engine's shape; every problem in the batch is reported at once, nothing is started."""
    from connectors.bigquery.source import to_trace         # OpenAI request/response -> contract trace
    out, problems, ids = [], [], set()
    for it in body.items:
        labels = [c.label for c in it.candidates]
        if it.id in ids:
            problems.append("%s: duplicate item id" % it.id)
        if len(set(labels)) != len(labels):
            problems.append("%s: candidate labels must be unique" % it.id)
        if not it.request.get("model"):
            problems.append("%s: request.model is required" % it.id)
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
    budget = body.max_budget_usd or float(credentials.get_config("VALIDATION_DEFAULT_BUDGET_USD", "25"))
    job = jobs.create(_items(body), spec, budget)
    return {"id": job["id"], "status": job["status"], "items": job["items_total"],
            "url": "/api/validations/" + job["id"]}


@api.get("/validations/{job_id}")
def status(job_id: str):
    return _job(job_id)


@api.get("/validations")
def recent():
    return jobs.recent()


@pages.get("/validations", response_class=HTMLResponse)
def list_page(request: Request):
    from app.web.server import _page
    return _page(request, "validations.html", jobs=jobs.recent())


def _job(job_id):
    """The job with its summary and results so far, or 404."""
    view = jobs.view(job_id)
    if view is None:
        raise HTTPException(404, "no such validation")
    return view


_FILTERS = {"all": lambda row: True, "problems": lambda row: row["problem"], "split": lambda row: row["split"]}


@pages.get("/validations/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str, item: str = "", cand: str = "", show: str = "all"):
    """Per-candidate headline, a request x candidate grid, and the full evidence for the picked cell."""
    from app.web.server import _page
    from app.services.downgrade_refset import evidence_row
    view = _job(job_id)
    rows = []
    for r in view["results"]:
        verdicts = {c["verdict"] for c in r["candidates"]}
        rows.append({"r": r, "cells": {c["label"]: c for c in r["candidates"]},
                     "problem": verdicts - {"within"} != set(), "split": len(verdicts) > 1})
    show = show if show in _FILTERS else "all"
    counts = {k: sum(1 for x in rows if f(x)) for k, f in _FILTERS.items()}
    picked = None
    r = next((x["r"] for x in rows if x["r"]["id"] == item), None)
    c = r and next((c for c in r["candidates"] if c["label"] == cand), None)
    submitted = c and jobs.item(job_id, item)
    if submitted:
        lat, cost = jobs.original_averages(r)
        picked = {"item": item, "c": c, "orig_latency_ms": lat, "orig_cost_usd": cost,
                  "row": evidence_row(submitted["request"], r, view["spec"]["cap_chars"], c)}
    return _page(request, "validation.html", job=view, rows=[x for x in rows if _FILTERS[show](x)],
                 labels=[s["label"] for s in view["summary"] if s["label"] != "original"], counts=counts,
                 show=show, picked=picked, live=view["status"] in jobs.LIVE)


@pages.get("/validations/{job_id}/results.json")
def download_json(job_id: str):
    import json
    from fastapi.responses import Response
    return Response(json.dumps(_job(job_id), indent=2), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="%s.json"' % job_id})


@pages.get("/validations/{job_id}/results.csv")
def download_csv(job_id: str):
    """One row per request x candidate: what a spreadsheet needs to compare candidates."""
    import csv
    import io
    from fastapi.responses import Response
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["request_id", "original_model", "original_consistency", "original_avg_latency_ms",
                "original_avg_cost_usd", "label", "candidate_model", "verdict", "fit", "avg_latency_ms",
                "avg_cost_usd", "reason", "judge_reason"])
    for r in _job(job_id)["results"]:
        lat, cost = jobs.original_averages(r)
        ref = r["reference"]
        for c in r["candidates"]:
            w.writerow([r["id"], r["model"], "%s/%s" % ("?" if ref["agree"] is None else ref["agree"], ref["of"]),
                        lat, cost, c["label"], c["model"], c["verdict"], "%d/%d" % (c["kept"], c["of"]),
                        c["latency_ms"], c["cost_usd"], c["reason"], c.get("judge_reason", "")])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="%s.csv"' % job_id})
