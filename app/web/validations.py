"""Validation API: submit a batch of requests with candidate models, follow it, read the results.

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
  GET  /api/validations/{id}/results.csv|json -> downloads (for the web UI; not in /openapi.json)

With "callback_url", the job's status and summary are POSTed there once it ends (done, stopped or failed).

Requests and responses are OpenAI-format bodies, as LiteLLM logs them. Instead of sending them, an item can name a
LiteLLM call by "request_id": the batch's ids are looked up in BigQuery (BQ_* settings) in one query at submit, and
the logged request and response are used, the original being the model that answered. Candidate labels belong to
the caller (a router, a release); each candidate is compared with the original model and no winner is picked.
"""
import dataclasses
import hmac
import json
import os
import threading
import time
import urllib.request
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

import credentials
from validation import jobs

MAX_ITEMS = 1000
MAX_CANDIDATES = 5


API_DOC = """Whether other models behave like the original, on your own logged requests: cheaper, newer, open-weight or
routers. Two ways in.

**Model audits** (`/api/audit/models`): name usecases (or one call-site) and a strategy; the server samples requests
per call-site from the gateway log, runs a validation and writes the verdicts on each call-site: which candidates
drift, and the cheapest and fastest that pass by measured cost and latency. Strategies: `cheaper`, `better_value`,
`release`, `revalidate`, `drift_watch`, `manual` (`GET /api/audit/models/strategies`). Verdicts are valid 30 days and
for the call-site's prompt version.

**Validations** (`/api/validations`): send the requests yourself.
1. `POST /api/validations` with up to 1000 items. Each item is a logged call, named by `request_id` (looked up in
   BigQuery) or sent as its OpenAI-format `request` and `response`, with up to 5 `candidates`: your `label` for each
   and the `model` to call. Returns `202` and the job `id`.
2. Wait. Either give a `callback_url` (the job status is POSTed there once, when the job ends), or poll
   `GET /api/validations/{id}`, sending its `ETag` back as `If-None-Match` (`304` = unchanged). `queued` and `running`
   mean still working; `done`, `stopped` (budget reached or the provider refused the account, see `stop_reason`) and
   `failed` (see `error`) mean it ended. A stopped or failed job goes on with `POST /api/validations/{id}/resume`.
3. Read `summary`: one row per label across the batch, plus `original`, the baseline.
4. Drill down: `GET /api/validations/{id}/results?label=...&verdict=outside` lists the requests a label drifted on,
   each with every answer and the judge's reasons.

**How a request is judged.** The original model is re-run 4 times; with its logged answer, these 5 answers show how
much the original itself varies. Each candidate answers 3 times, and 2 more when one of those isn't a pass. Each
answer gets one judge vote, and 2 more when that vote isn't a pass; an answer identical to one of the original's needs
no judge.
- Each answer: `match` (same behaviour), `acceptable` (differs only where the original itself varies), `drift`
  (changed a decision, value or action, left something out, or made something up) or `unclear`.
- Each request, per candidate: `within` (passes: its answers stay within the original's own consistency), `outside`
  (drifts) or `unverified` (could not be judged; `reason` says why).

No winner is picked in a validation: decide per label, e.g. from `within_rate`, `cost_vs_original_pct` and
`latency_vs_original_pct`. (A model audit does pick: the cheapest and fastest that pass.)

**Re-runs** send the logged request as it was (messages, tools, tool_choice, response_format, temperature ...); only
the model changes. They are not served from a cache and are not logged as production traffic.

**Spend.** Per request: 4 re-runs of the original, 3 to 5 answers per candidate, 1 to 3 judge calls per answer, and
one more judge call. `max_budget_usd` caps a job or an audit (no cap unless given); `spend_usd` is what it used.

**Auth.** Every call needs the `X-API-Key` header.
"""

_key = APIKeyHeader(name="X-API-Key", auto_error=False, description="The server's VALIDATION_API_KEY")
_IAP_KEYS = "https://www.gstatic.com/iap/verify/public_key"
_iap = {"at": 0.0, "certs": None}
_iap_lock = threading.Lock()


def _iap_user(assertion):
    """The signed-in user's email when the request came through Google IAP, else None. IAP signs every request it
    lets through (x-goog-iap-jwt-assertion); IAP_AUDIENCE is this service's audience in IAP, and without it the
    header is never trusted. IAP's public keys are fetched at most once an hour."""
    audience = credentials.get_config("IAP_AUDIENCE")
    if not (assertion and audience):
        return None
    from google.auth import jwt
    try:
        with _iap_lock:
            if time.time() - _iap["at"] > 3600:
                with urllib.request.urlopen(_IAP_KEYS, timeout=10) as r:
                    _iap.update(certs=json.loads(r.read()), at=time.time())
        claims = jwt.decode(assertion, certs=_iap["certs"], audience=audience)
    except Exception:
        return None
    return claims.get("email") if claims.get("iss") == "https://cloud.google.com/iap" else None


def _ui_open():
    """VALIDATION_UI_OPEN=1: the web UI may call the API without a key, on a developer's machine only (there is no
    IAP there). Ignored on Cloud Run."""
    return credentials.get_config("VALIDATION_UI_OPEN") == "1" and not os.environ.get("K_SERVICE")


def require_key(x_api_key: Optional[str] = Depends(_key),
                iap: Optional[str] = Header(None, alias="x-goog-iap-jwt-assertion", include_in_schema=False)):
    """Code calls the API with X-API-Key; the web UI calls it as the person signed in through IAP."""
    if _iap_user(iap) or _ui_open():
        return
    expected = credentials.get_secret("VALIDATION_API_KEY")
    if not expected:
        raise HTTPException(503, "VALIDATION_API_KEY is not set on the server")
    if not x_api_key or not hmac.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(401, "missing or wrong X-API-Key")


api = APIRouter(prefix="/api", dependencies=[Depends(require_key)], tags=["validations"],
                responses={401: {"description": "Missing or wrong X-API-Key"},
                           503: {"description": "The server has no VALIDATION_API_KEY, or a BigQuery lookup failed"}})


class CandidateIn(BaseModel):
    label: str = Field(min_length=1, max_length=120, description="Your name for it, e.g. a router or a release. "
                                                                 "Unique within the item; 'original' is reserved")
    model: str = Field(min_length=1, max_length=120, description="The model or router to call, as the gateway names it")


class ItemIn(BaseModel):
    id: Optional[str] = Field(default=None, min_length=1,
                              description="Your id for the request, unique in the batch; defaults to request_id")
    request: Optional[dict] = Field(default=None, description="The logged OpenAI chat request: model (the original), "
                                    "messages, and tools, tool_choice, response_format, temperature, max_tokens when used")
    response: Optional[dict] = Field(default=None, description="The original's logged OpenAI chat completion; "
                                     "without it the original is re-run once more")
    request_id: Optional[str] = Field(default=None, min_length=1, max_length=200,
                                      description="Instead of request and response: a logged LiteLLM call, looked up "
                                                  "in BigQuery")
    candidates: list[CandidateIn] = Field(min_length=1, max_length=MAX_CANDIDATES)


class JobIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "items": [
            {"request_id": "chatcmpl-ET2KW1d347oy8pFVy3NbMzm3liZzC",
             "candidates": [{"label": "mini", "model": "gpt-5.4-mini"}, {"label": "router", "model": "jev-router"}]},
            {"id": "refund-17",
             "request": {"model": "gpt-5.4", "max_tokens": 1024, "messages": [
                 {"role": "system", "content": "You are a billing agent."},
                 {"role": "user", "content": "Refund order 17, it arrived broken."}]},
             "response": {"model": "gpt-5.4", "choices": [{"message": {"role": "assistant",
                                                                        "content": "I've started a refund for order 17."}}],
                          "usage": {"prompt_tokens": 31, "completion_tokens": 12}},
             "candidates": [{"label": "mini", "model": "gpt-5.4-mini"}]}],
        "max_budget_usd": 25,
        "callback_url": "https://example.internal/hooks/validation"}]})
    items: list[ItemIn] = Field(min_length=1, max_length=MAX_ITEMS)
    judge_model: Optional[str] = Field(default=None, description="The judge; defaults to the server's (claude-sonnet-5)")
    max_budget_usd: Optional[float] = Field(default=None, gt=0, description="Stop once the job has spent this much; "
                                            "no cap when left out")
    callback_url: Optional[HttpUrl] = Field(default=None, description="POSTed the job status once, when the job ends")


class ResumeIn(BaseModel):
    max_budget_usd: Optional[float] = Field(default=None, gt=0, description="A new budget for the job")


# ── what the API returns: documentation for /docs and /openapi.json. The routes return plain dicts; the tests check
# real responses against these models (extra fields refused), so the documentation can't drift from the code ──────
class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


Verdict = Literal["within", "outside", "unverified"]


class Submitted(_Out):
    id: str
    status: str
    items: int = Field(description="Requests in the job")
    url: str = Field(description="Where to follow it")


class JobSpec(_Out):
    reference_runs: int = Field(description="Fresh re-runs of the original per request")
    candidate_runs: int = Field(description="Answers per candidate per request at most")
    first_runs: int = Field(description="Answers per candidate first; the rest only when one of those isn't a pass")
    votes: int = Field(description="Judge calls per candidate answer when the first isn't a pass; an answer "
                                   "identical to one of the original's needs none")
    coherence_floor: float = Field(description="Below this self-agreement the original has no stable behaviour, so "
                                               "the request is unverified")
    margin: float = Field(description="How far a candidate may trail the original's self-agreement and still pass")
    judge_model: str
    judge_max_tokens: int
    max_parallel: int


class Job(_Out):
    id: str
    status: Literal["queued", "running", "done", "stopped", "failed"]
    created_at: str
    updated_at: str
    items_total: int
    items_done: int
    spend_usd: float
    max_budget_usd: Optional[float]
    judge_model: str
    stop_reason: Optional[str] = Field(description="Why a stopped job stopped")
    error: Optional[str] = Field(description="Why a failed job failed")
    workers: int = Field(description="Workers on it right now")
    unpriced_calls: int = Field(description="Calls to models without a catalog price: spend is understated")
    spec: JobSpec


class Answers(_Out):
    match: int = Field(description="Same behaviour as the original")
    acceptable: int = Field(description="Differs only where the original itself varies")
    drift: int = Field(description="Changed a decision, value or action, or made something up")
    unclear: int = Field(description="Not judged: the call failed or the judges were unsure")


class LabelSummary(_Out):
    label: str = Field(description="A candidate label, or 'original' for the baseline row")
    models: list[str] = Field(description="Models this label was asked to call")
    requests: int
    answers: Optional[Answers] = Field(description="Every answer of this label, by category; null on the original")
    within: int = Field(description="Requests that pass")
    outside: int = Field(description="Requests that drift")
    unverified: int = Field(description="Requests that could not be judged")
    avg_fit_rate: Optional[float] = Field(default=None, description="Candidates: share of answers that fit, averaged")
    avg_consistency: Optional[float] = Field(default=None, description="Original: how often its own answers agree")
    avg_latency_ms: Optional[int]
    avg_cost_usd: Optional[float] = Field(description="Per call, over the same requests as the original's")
    within_rate: Optional[float] = Field(default=None, description="Candidates: within / requests")
    cost_vs_original_pct: Optional[int] = Field(default=None, description="Candidates: -72 means 72% cheaper")
    latency_vs_original_pct: Optional[int] = Field(default=None, description="Candidates: -31 means 31% faster")


class Routing(_Out):
    label: str
    served_model: Optional[str] = Field(description="The model that answered, as named in its responses")
    requests: int
    within: int
    outside: int
    unverified: int


class Callback(_Out):
    sent_at: Optional[str] = Field(description="When it was delivered; null until then")
    error: Optional[str] = Field(description="Why the last delivery failed")


class Status(Job):
    summary: list[LabelSummary]
    routing: list[Routing] = Field(description="For labels answered by several models (routers): results per model")
    callback: Optional[Callback] = Field(description="null without a callback_url")
    results_url: str


class Listed(Job):
    summary: Optional[list[LabelSummary]] = Field(default=None, description="With ?with_summary=1")


class Usage(_Out):
    input: int = Field(description="Input tokens not read from a cache")
    output: int
    cache_read: int
    cache_write: int


class Call(_Out):
    output: str = Field(description="The answer: its text, then any tool call as [calls name({args})]")
    model: Optional[str] = Field(description="The model that answered")
    usage: Optional[Usage]
    latency_ms: Optional[int]
    cost_usd: Optional[float]
    error: Optional[str] = Field(description="Why the call failed")
    stop_reason: Optional[str]


class OriginalAnswer(Call):
    recorded: Optional[bool] = Field(description="true: the logged answer; otherwise a fresh re-run")
    agrees: bool = Field(description="Agrees with the rest of the original's answers")
    reason: str = Field(description="Why it doesn't, when it doesn't")


class Reference(_Out):
    agree: Optional[int] = Field(description="How many of the original's answers agree; null when unknown")
    of: int
    consistency: Optional[float] = Field(description="agree / of")
    runs: list[OriginalAnswer]


class Vote(_Out):
    kept: Optional[bool] = Field(description="true: fits the original's answers; null: the judge was unsure")
    reason: str


class CandidateAnswer(Call):
    degraded: Optional[bool] = Field(description="The model refused a setting of the request (e.g. reasoning), so "
                                                 "it ran without it")
    kept: bool = Field(description="The judges' majority: it fits")
    category: Optional[Literal["match", "acceptable", "drift", "unclear"]]
    votes: str = Field(description="e.g. '3/3': the judges that agree with the outcome")
    reason: str
    judges: list[Vote]


class CandidateResult(_Out):
    label: str
    model: str
    verdict: Verdict
    reason: str = Field(description="Why this verdict, e.g. '(4/5 vs original 5/5)' or why it is unverified")
    judge_reason: str = Field(description="The judges' most common reason behind the verdict")
    kept: int = Field(description="Answers that fit")
    of: int
    fit_rate: Optional[float]
    answers: Optional[Answers]
    latency_ms: Optional[int] = Field(description="Average over its answers")
    cost_usd: Optional[float] = Field(description="Average per answer")
    runs: list[CandidateAnswer]


class Message(_Out):
    role: str
    content: str


class Sent(_Out):
    messages: list[Message] = Field(description="The conversation as text, system prompt first")
    tools: list[str] = Field(description="Names of the tools the request offered")


class Result(_Out):
    id: str
    model: str = Field(description="The original model")
    spend_usd: float = Field(description="What judging this request cost")
    reference: Reference = Field(description="The original's own answers")
    candidates: list[CandidateResult]
    request: Optional[Sent] = Field(default=None, description="What was sent; on the one-request call only")


class ResultsPage(_Out):
    page: int
    per_page: int
    total: int
    results: list[Result]


def _logged_bodies(ids):
    """{request_id: (request, response)} from BigQuery, for the model checks (same lookup as the API's items)."""
    from connectors.bigquery import source
    return source.lookup(set(ids)) if ids else {}


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


@api.post("/validations", status_code=202, summary="Submit a batch", responses={
    202: {"model": Submitted}, 422: {"description": "The batch was refused whole; `detail` lists every problem"}})
def submit(body: JobIn):
    """Queue a batch. Items named by `request_id` are looked up in BigQuery first; nothing starts unless every item
    is usable."""
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


@api.get("/validations/{job_id}", summary="Status and summary", responses={
    200: {"model": Status}, 304: {"description": "Unchanged since the ETag sent as If-None-Match"},
    404: {"description": "No such validation"}})
def status(job_id: str, if_none_match: Optional[str] = Header(None)):
    """Progress, spend, and the per-label summary. Small enough to poll; the callback sends the same body."""
    job = _known(job_id)
    cb = jobs.callback(job_id)
    tag = 'W/"%s-%d-%d-%s-%s"' % (job["status"], job["items_done"], job["workers"], job["updated_at"],
                                  cb and "%s%s" % (cb["sent_at"], bool(cb["error"])))
    if if_none_match == tag:
        return Response(status_code=304, headers={"ETag": tag})
    return JSONResponse({**job, "summary": jobs.summary(job_id), "routing": jobs.routing(job_id), "callback": cb,
                         "results_url": "/api/validations/%s/results" % job_id}, headers={"ETag": tag})


@api.post("/validations/{job_id}/resume", summary="Resume a stopped or failed job", responses={
    200: {"model": Job}, 404: {"description": "No such validation"},
    409: {"description": "Not stopped or failed, or its budget is spent: send a higher max_budget_usd"}})
def resume(job_id: str, body: Optional[ResumeIn] = None):
    """Its unfinished requests run; finished ones are kept. The callback is sent again when it ends."""
    _known(job_id)
    job = jobs.resume(job_id, body.max_budget_usd if body else None)
    if job is None:
        raise HTTPException(409, "only a failed or stopped validation can be resumed; one stopped by its budget "
                                 "needs a higher max_budget_usd")
    return job


@api.get("/validations/{job_id}/results", summary="Results per request", responses={
    200: {"model": ResultsPage}, 404: {"description": "No such validation"}})
def results(job_id: str, page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=200),
            show: Literal["all", "problems", "split"] = Query(
                "all", description="problems: a candidate is outside or unverified; split: candidates disagree"),
            label: Optional[str] = Query(None, description="With verdict: requests where this label has it"),
            verdict: Optional[Verdict] = None, q: str = Query("", description="Part of a request id")):
    """Finished requests in submission order, each with every answer and the judges' reasons."""
    _known(job_id)
    total, res = jobs.page(job_id, page, per_page, show=show, label=label, verdict=verdict, q=q)
    return {"page": page, "per_page": per_page, "total": total, "results": res}


@api.get("/validations/{job_id}/results/{request_id:path}", summary="One request's result", responses={
    200: {"model": Result}, 404: {"description": "No such validation, or the request has not finished"}})
def one_result(job_id: str, request_id: str):
    """Everything about one request, including what was sent."""
    got = jobs.request(job_id, request_id)
    if not got or got[1] is None:
        raise HTTPException(404, "no finished request %r in this validation" % request_id)
    trace = got[0]["request"]
    return {**got[1], "request": {"messages": [{"role": m["role"], "content": m["content"]}
                                               for m in trace.get("input_messages", [])],
                                  "tools": [t[0] for t in trace.get("tools_defined", [])]}}


@api.get("/validations", summary="List jobs", responses={200: {"model": list[Listed]}})
def recent(page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=200), with_summary: bool = False):
    """Newest first."""
    found = jobs.recent(per_page, (page - 1) * per_page)
    return _with_summaries(found) if with_summary else found


def _with_summaries(found):
    by = jobs.summaries([j["id"] for j in found])
    return [{**j, "summary": by.get(j["id"], [])} for j in found]


@api.get("/validations/{job_id}/results.json", include_in_schema=False)
def download_json(job_id: str):
    import json
    _known(job_id)
    return Response(json.dumps(jobs.view(job_id), indent=2), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="%s.json"' % job_id})


@api.get("/validations/{job_id}/results.csv", include_in_schema=False)
def download_csv(job_id: str):
    """One row per request x candidate: what a spreadsheet needs to compare candidates."""
    import csv
    import io
    _known(job_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["request_id", "original_model", "original_consistency", "original_avg_latency_ms",
                "original_avg_cost_usd", "label", "candidate_model", "served_model", "verdict", "fit", "fit_rate",
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
                    headers={"Content-Disposition": 'attachment; filename="%s.csv"' % job_id})
