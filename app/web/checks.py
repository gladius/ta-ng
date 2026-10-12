"""Model checks: one way to start them (usecases or a call-site, the models to try, Auto), list them, follow one."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.web.validations import require_key
from core.identity import store
from modules.models import checks

api = APIRouter(prefix="/api/suite/checks", dependencies=[Depends(require_key)], include_in_schema=False)


class CheckIn(BaseModel):
    """Scope: `usecases` (one check each), or one `site`. Candidates: `models` (any served models) and/or `auto`
    (per call-site, the catalog's cheaper tier and open-weight swap of its current model)."""
    usecases: list[str] = Field(default_factory=list)       # "None" names the untagged usecase
    site: Optional[str] = None
    models: list[str] = Field(default_factory=list)
    auto: bool = False
    samples: int = Field(default=checks.DEFAULT_SAMPLES, ge=1, le=50)
    max_budget_usd: Optional[float] = Field(default=None, gt=0, description="per check; none when left out")
    trigger: str = "user"
    judge_model: Optional[str] = None


def _uc(u):
    return None if u in (None, "None") else u


@api.get("/estimate")
def estimate(usecase: Optional[str] = None, site: Optional[str] = None, samples: int = checks.DEFAULT_SAMPLES, candidates: int = 1):
    return {"usd": checks.estimate(_uc(usecase), site, samples, max(1, candidates))}


@api.post("", status_code=202)
def start(body: CheckIn):
    if not body.models and not body.auto:
        raise HTTPException(422, "name at least one model, or choose Auto")
    if body.trigger not in checks.TRIGGERS:
        raise HTTPException(422, "trigger must be one of %s" % ", ".join(checks.TRIGGERS))
    preset = "auto" if body.auto and not body.models else "custom"
    kw = dict(preset=preset, asked=body.models, auto=body.auto, n=body.samples, max_budget_usd=body.max_budget_usd,
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
