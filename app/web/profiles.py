"""Call-site profiles: what each call-site is, as the profiler named it. Shared by the cache, models and router pages."""
from fastapi import APIRouter, Depends, HTTPException

from app.web.validations import require_key
from core import profiler

api = APIRouter(prefix="/api/suite/profiles", dependencies=[Depends(require_key)], include_in_schema=False)


@api.get("")
def listing(usecase: str | None = None, q: str | None = None, page: int = 1, per_page: int = 50):
    return profiler.listing(usecase=usecase, q=q, page=max(1, page), per_page=min(200, max(1, per_page)))


@api.get("/{site}")
def one(site: str):
    p = profiler.get(site)
    if not p:
        raise HTTPException(404, "no profile for this call-site")
    return p
