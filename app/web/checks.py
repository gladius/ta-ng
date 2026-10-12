"""Model checks: one way to start them (usecases or a call-site, the models to try, Auto), list them, follow one."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.web.validations import require_key
from core.identity import store
from modules.models import checks

api = APIRouter(prefix="/api/suite/checks", dependencies=[Depends(require_key)], include_in_schema=False)


class CheckIn(BaseModel):
    """Scope: `usecases` (one check each), or one `site`. Strategy: how candidates are chosen per call-site (see
    GET /api/suite/checks/strategies); `models` are added to the strategy's picks (required for release and manual)."""
    usecases: list[str] = Field(default_factory=list)       # "None" names the untagged usecase
    site: Optional[str] = None
    strategy: str = "cheaper"
    models: list[str] = Field(default_factory=list)
    auto: bool = False                                      # older clients: same as strategy "cheaper"
    samples: int = Field(default=checks.DEFAULT_SAMPLES, ge=1, le=50)
    max_budget_usd: Optional[float] = Field(default=None, gt=0, description="per check; none when left out")
    trigger: str = "user"
    judge_model: Optional[str] = None


def _uc(u):
    return None if u in (None, "None") else u


@api.get("/strategies")
def strategies():
    """The strategies and the engine's settings, for the dialog and the explainer."""
    from app.services.downgrade_refset import spec as app_spec
    sp = app_spec()
    return {"strategies": [{"id": k, "does": v} for k, v in checks.STRATEGIES.items()],
            "spec": {"reference_runs": sp.reference_runs, "first_runs": sp.first_runs, "candidate_runs": sp.candidate_runs,
                     "votes": sp.votes, "margin": sp.margin, "coherence_floor": sp.coherence_floor, "judge_model": sp.judge_model},
            "samples": checks.DEFAULT_SAMPLES, "valid_days": checks.VALID_DAYS}


@api.get("/estimate")
def estimate(usecase: Optional[str] = None, site: Optional[str] = None, samples: int = checks.DEFAULT_SAMPLES, candidates: int = 1):
    return {"usd": checks.estimate(_uc(usecase), site, samples, max(1, candidates))}


@api.post("", status_code=202)
def start(body: CheckIn):
    strategy = "cheaper" if body.auto and body.strategy == "manual" and not body.models else body.strategy
    if strategy not in checks.STRATEGIES:
        raise HTTPException(422, "strategy must be one of %s" % ", ".join(checks.STRATEGIES))
    if strategy in ("release", "manual") and not body.models:
        raise HTTPException(422, "%s needs model names" % strategy)
    if body.trigger not in checks.TRIGGERS:
        raise HTTPException(422, "trigger must be one of %s" % ", ".join(checks.TRIGGERS))
    kw = dict(preset=strategy, asked=body.models, auto=False, n=body.samples, max_budget_usd=body.max_budget_usd,
              trigger=body.trigger, judge_model=body.judge_model)
    if body.site:
        rec = store.get(body.site)
        if not rec:
            raise HTTPException(404, "unknown call-site")
        c = checks.start(usecase=rec["usecase"], site=body.site, **kw)
        return {"checks": [c["id"]]}
    if not body.usecases:
        raise HTTPException(422, "choose at least one usecase, or a call-site")
    started = []
    for u in body.usecases:
        if not store.get_usecase(_uc(u)):
            raise HTTPException(404, "unknown usecase %s" % u)
    for u in body.usecases:
        started.append(checks.start(usecase=_uc(u), site=None, **kw)["id"])
    return {"checks": started}


@api.get("")
def listing(usecase: Optional[str] = None, site: Optional[str] = None, limit: int = 50):
    return [checks.refresh_status(c) for c in checks.recent(min(200, limit), _uc(usecase) if usecase else None, site)]


@api.get("/{check_id}")
def one(check_id: str):
    c = checks.get(check_id)
    if not c:
        raise HTTPException(404, "unknown check")
    c = checks.refresh_status(c)
    from validation import jobs
    job = jobs.get(c["job_id"]) if c["job_id"] else None
    names = {s["site"]: (s["profile"] or {}).get("name") or s["site"] for s in store.all_sites()}
    return {**c, "job": job and {"id": job["id"], "status": job["status"], "items_total": job["items_total"], "items_done": job["items_done"],
                                 "spend_usd": job["spend_usd"], "stop_reason": job.get("stop_reason")},
            "site_names": {st: names.get(st, st) for st in c["samples"]}}
