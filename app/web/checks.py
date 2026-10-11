"""Model checks: start one (a usecase or a call-site, a preset or named candidates, a budget), list them, follow one."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.web.validations import require_key
from core.identity import store
from modules.models import checks

api = APIRouter(prefix="/api/suite/checks", dependencies=[Depends(require_key)], include_in_schema=False)


class CheckIn(BaseModel):
    usecase: Optional[str] = None                   # None / "None" for the untagged usecase
    site: Optional[str] = None
    preset: str = "cheaper"
    candidates: list[str] = Field(default_factory=list)
    samples: int = Field(default=checks.DEFAULT_SAMPLES, ge=1, le=50)
    max_budget_usd: float = Field(gt=0)
    trigger: str = "user"
    judge_model: Optional[str] = None


def _uc(u):
    return None if u in (None, "None") else u


@api.get("/estimate")
def estimate(usecase: Optional[str] = None, site: Optional[str] = None, samples: int = checks.DEFAULT_SAMPLES, candidates: int = 1):
    return {"usd": checks.estimate(_uc(usecase), site, samples, max(1, candidates))}


@api.post("", status_code=202)
def start(body: CheckIn):
    if body.preset not in checks.PRESETS:
        raise HTTPException(422, "preset must be one of %s" % ", ".join(checks.PRESETS))
    if body.preset in ("release", "custom") and not body.candidates:
        raise HTTPException(422, "%s needs candidate models" % body.preset)
    if body.trigger not in checks.TRIGGERS:
        raise HTTPException(422, "trigger must be one of %s" % ", ".join(checks.TRIGGERS))
    if body.site and not store.get(body.site):
        raise HTTPException(404, "unknown call-site")
    if not body.site and not store.get_usecase(_uc(body.usecase)):
        raise HTTPException(404, "unknown usecase")
    c = checks.start(usecase=_uc(body.usecase), site=body.site, preset=body.preset, asked=body.candidates,
                     n=body.samples, max_budget_usd=body.max_budget_usd, trigger=body.trigger, judge_model=body.judge_model)
    return c


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
