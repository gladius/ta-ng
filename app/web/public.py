"""The public API's shared parts: sign-in (X-API-Key or IAP), the API description, the request models a caller
sends and the result models it reads, and turning sent requests into the engine's items. The routes live in
app/web/model_audits.py (/api/audit/models).

Requests and responses are OpenAI-format bodies, as LiteLLM logs them. Instead of sending them, a request can name a
LiteLLM call by "request_id": the ids are looked up in BigQuery (BQ_* settings) in one query, and the logged request
and response are used, the original being the model that answered."""
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


API_DOC = """Whether other models behave like the original, on your own traffic: cheaper, newer, open-weight or
routers. One call, `POST /api/audit/models`, with one scope:

- `usecases`: one audit per usecase; the server samples requests per call-site from the gateway log (smallest and
  largest prompts, the quartiles, the heaviest answers, a few random) and writes the verdicts on each call-site.
- `site`: the same for one call-site.
- `requests`: yours, up to 1000: logged calls named by `request_id` (looked up in BigQuery) or OpenAI-format
  `request` and `response` bodies. A request may name its own `candidates` (`label` and `model`).

Candidates: the `strategy`'s picks for each request's model plus the `models` you name. Strategies (`GET
/api/audit/models/strategies`): `cheaper`, `better_value`, `release`, `revalidate`, `drift_watch`, `manual`.

**Flow**
1. `POST /api/audit/models` returns `202` and `{audits: [{id, url, ...}]}`.
2. Wait: give a `callback_url` (the audit's status is POSTed there once, when it ends), or poll
   `GET /api/audit/models/{id}` with its `ETag` as `If-None-Match` (`304` = unchanged). `sampling`, `queued` and
   `running` mean still working; `done`, `stopped` (budget reached or the provider refused the account, see
   `stop_reason`) and `failed` (see `error`) mean it ended. `POST /api/audit/models/{id}/resume` continues a stopped or
   failed one.
3. Read `summary` (one row per candidate, plus `original`, the baseline) and `verdicts` (per call-site and candidate:
   pass rate, cost and latency against the original; on the call-sites the cheapest and fastest that pass are kept,
   valid 30 days and for the call-site's prompt version).
4. Drill down: `GET /api/audit/models/{id}/results?label=...&verdict=outside` lists the requests a candidate drifted
   on, each with every answer and the judge's reasons.

**How a request is judged.** The original model is re-run 4 times; with its logged answer, these 5 answers show how
much the original itself varies. Each candidate answers 3 times, and 2 more when one of those isn't a pass. Each
answer gets one judge vote, and 2 more when that vote isn't a pass; an answer identical to one of the original's needs
no judge.
- Each answer: `match` (same behaviour), `acceptable` (differs only where the original itself varies), `drift`
  (changed a decision, value or action, left something out, or made something up) or `unclear`.
- Each request, per candidate: `within` (passes: its answers stay within the original's own consistency), `outside`
  (drifts) or `unverified` (could not be judged; `reason` says why).

**Re-runs** send the logged request as it was (messages, tools, tool_choice, response_format, temperature ...); only
the model changes. They are not served from a cache and are not logged as production traffic.

**Spend.** Per request: 4 re-runs of the original, 3 to 5 answers per candidate, 1 to 3 judge calls per answer, and
one more judge call. `max_budget_usd` caps an audit (no cap unless given); `spend_usd` is what it used.

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


class CandidateIn(BaseModel):
    label: str = Field(min_length=1, max_length=120, description="Your name for it, e.g. a router or a release. "
                                                                 "Unique within the item; 'original' is reserved")
    model: str = Field(min_length=1, max_length=120, description="The model or router to call, as the gateway names it")


class RequestIn(BaseModel):
    id: Optional[str] = Field(default=None, min_length=1,
                              description="Your id for the request, unique in the batch; defaults to request_id")
    request: Optional[dict] = Field(default=None, description="The logged OpenAI chat request: model (the original), "
                                    "messages, and tools, tool_choice, response_format, temperature, max_tokens when used")
    response: Optional[dict] = Field(default=None, description="The original's logged OpenAI chat completion; "
                                     "without it the original is re-run once more")
    request_id: Optional[str] = Field(default=None, min_length=1, max_length=200,
                                      description="Instead of request and response: a logged LiteLLM call, looked up "
                                                  "in BigQuery")
    candidates: Optional[list[CandidateIn]] = Field(default=None, max_length=MAX_CANDIDATES, description=
        "This request's own candidates; without them, the audit's models (and its strategy's picks) are used")


class ResumeIn(BaseModel):
    max_budget_usd: Optional[float] = Field(default=None, gt=0, description="A new budget for the audit")


# ── what the API returns: documentation for /docs and /openapi.json. The routes return plain dicts; the tests check
# real responses against these models (extra fields refused), so the documentation can't drift from the code ──────
class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


Verdict = Literal["within", "outside", "unverified"]


class Started(_Out):
    id: str
    status: str = Field(description="sampling (scope from the log) or queued (requests you sent)")
    items: int = Field(description="Requests in the audit; 0 while sampling")
    url: str = Field(description="Where to follow it")


class Submitted(_Out):
    audits: list[Started] = Field(description="One audit per usecase, or one for a call-site or for your requests")


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


class Verdict_(_Out):
    requests: int
    within: int
    outside: int
    unverified: int
    pass_rate: float
    cost_vs_original_pct: Optional[int]
    latency_vs_original_pct: Optional[int]
    fit_rate: Optional[float]
    verdict: Literal["passes", "drifts", "not verified"]


class Sample(_Out):
    id: str
    why: str = Field(description="Why it was picked: smallest prompt, largest prompt, median, quartile, heaviest "
                                 "answer, random, or sent (your requests)")
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None


class Audit(_Out):
    id: str
    status: Literal["sampling", "queued", "running", "done", "stopped", "failed"]
    created_at: str
    updated_at: Optional[str]
    trigger: str = Field(description="Who started it: user, release, router, api, schedule")
    scope: Literal["usecase", "site", "requests"]
    usecase: Optional[str]
    site: Optional[str] = Field(description="The call-site id, for an audit of one call-site")
    strategy: str
    models: list[str] = Field(description="The models named when it was started (the strategy adds its own picks)")
    items_total: int
    items_done: int
    spend_usd: float
    max_budget_usd: Optional[float]
    judge_model: Optional[str]
    stop_reason: Optional[str] = Field(description="Why a stopped audit stopped")
    error: Optional[str] = Field(description="Why a failed audit failed")
    workers: int = Field(description="Workers on it right now")
    unpriced_calls: int = Field(description="Calls to models without a catalog price: spend is understated")
    spec: Optional[JobSpec] = Field(description="How hard it looks; null while sampling")
    samples: dict[str, list[Sample]] = Field(description="Per call-site id the requests audited ('' for your requests)")
    verdicts: Optional[dict[str, dict[str, Verdict_]]] = Field(
        description="Once it ended: per call-site id ('' for your requests), per candidate model")
    site_names: dict[str, str] = Field(description="Call-site id -> its profiled name")


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


class Status(Audit):
    summary: list[LabelSummary]
    routing: list[Routing] = Field(description="For labels answered by several models (routers): results per model")
    callback: Optional[Callback] = Field(description="null without a callback_url")
    results_url: str


class Listed(Audit):
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


def _logged_bodies(ids, oldest_day=None):
    """{request_id: (request, response)} from BigQuery for an audit's samples: the log's days back to `oldest_day`."""
    import datetime
    from connectors.bigquery import source
    if not ids:
        return {}
    days = source.LOOKUP_DAYS
    if oldest_day:
        days = (datetime.date.today() - datetime.date.fromisoformat(str(oldest_day))).days + 2
    return source.lookup(set(ids), days)


def _logged(items):
    """{request_id: (request, response)} for the requests sent by request_id only: one BigQuery lookup."""
    from connectors.bigquery import source
    from core.ledger import bq
    wanted = {it.request_id for it in items if it.request is None and it.request_id}
    if not wanted:
        return {}
    if not bq.configured():
        raise HTTPException(422, ["items sent by request_id need BigQuery: set %s on the server" % ", ".join(bq.SETTINGS.values())])
    try:
        return source.lookup(wanted)
    except Exception as e:
        raise HTTPException(503, "BigQuery lookup failed: %s" % str(e)[:300])


def parse_requests(requests, default_candidates):
    """Validated items in the engine's shape; every problem is reported at once, nothing is started.
    `default_candidates(model)` gives [{label, model}] for a request that names no candidates of its own."""
    from connectors.bigquery import source
    from connectors.bigquery.source import to_trace         # OpenAI request/response -> contract trace
    out, problems, ids = [], [], set()
    logged = _logged(requests)
    for n, it in enumerate(requests):
        it.id = it.id or it.request_id
        if it.request is None:
            if not it.request_id:
                problems.append("item %d: send request (and response) or request_id" % (n + 1))
                continue
            if it.request_id not in logged:
                problems.append("%s: request_id not found in the log's last %d days" % (it.id, source.LOOKUP_DAYS))
                continue
            it.request, it.response = logged[it.request_id]
            if it.response is None:
                problems.append("%s: the logged call has no response (it failed)" % it.id)
                continue
        elif not it.id:
            problems.append("item %d: id is required" % (n + 1))
            continue
        cands = [c.model_dump() for c in it.candidates] if it.candidates else default_candidates(it.request.get("model"))
        if not cands:
            problems.append("%s: no candidate: name models (or this request's candidates) or pick a strategy with "
                            "picks for its model" % it.id)
        labels = [c["label"] for c in cands]
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
                    "candidates": cands})
    if problems:
        raise HTTPException(422, problems)
    return out
