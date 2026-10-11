"""The cache module's JSON: the org, usecase and call-site levels from the app's tables, and the refresh."""
from fastapi import APIRouter, Depends, HTTPException

from app.web.validations import require_key
from core.ledger import refresh
from modules.cache import query

api = APIRouter(prefix="/api/suite/cache", dependencies=[Depends(require_key)], include_in_schema=False)


@api.get("/overview")
def overview():
    o = query.overview()
    if not o:
        raise HTTPException(404, "no cache analysis yet: refresh first")
    return {**o, "status": refresh.status()}


@api.get("/usecases/{usecase}")
def usecase(usecase: str):
    u = query.usecase(None if usecase == "None" else usecase)
    if not u:
        raise HTTPException(404, "unknown usecase")
    return u


@api.get("/sites/{site}")
def site(site: str):
    s = query.site(site)
    if not s:
        raise HTTPException(404, "unknown call-site")
    return s


@api.post("/refresh")
def start_refresh(days: int = 30):
    return {"started": refresh.start(days), **refresh.status()}


@api.get("/refresh")
def refresh_status():
    return refresh.status()
