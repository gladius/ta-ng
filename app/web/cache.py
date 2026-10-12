"""The cache module's JSON: the org, usecase and call-site levels from the call-site records, and the fleet
analysis (every usecase of the window) for the org view."""
from fastapi import APIRouter, Depends, HTTPException

from app.web.validations import require_key
from core.ledger import refresh
from modules.cache import query

api = APIRouter(prefix="/api/audit/cache", dependencies=[Depends(require_key)], include_in_schema=False)


def _status():
    return {"running": "*" in refresh.running(), "error": None, "analysing": refresh.running()}


@api.get("/overview")
def overview():
    o = query.overview()
    if not o:
        raise HTTPException(404, "no usecase analysed yet: open a usecase, or analyse the whole window")
    return {**o, "status": _status()}


@api.get("/usecases/{usecase}")
def usecase(usecase: str):
    u = query.usecase(None if usecase == "None" else usecase)
    if not u:
        raise HTTPException(404, "usecase not analysed yet")
    return u


@api.get("/sites/{site}")
def site(site: str):
    s = query.site(site)
    if not s:
        raise HTTPException(404, "unknown call-site")
    return s


@api.post("/refresh")
def start_refresh(days: int = 30):
    """Analyse the whole window (every usecase); the usecases list is reloaded first."""
    refresh.list_usecases(days)
    return {"started": refresh.start(None, days), **_status()}


@api.get("/refresh")
def refresh_status():
    return _status()
