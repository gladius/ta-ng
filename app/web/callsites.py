"""Usecases and call-sites: the cheap list (reloaded when stale), one usecase analysed on demand, one call-site's
identity, profile and decisions."""
from fastapi import APIRouter, Depends, HTTPException

from app.web.public import require_key
from core import tasks
from core.identity import store
from core.ledger import bq, refresh

api = APIRouter(prefix="/api/suite/callsites", dependencies=[Depends(require_key)], include_in_schema=False)


def _cache_decision(site):
    from modules.cache import query
    return query.decision(site)


def _with_profile(s):
    p = s.get("profile")
    return {**s, "name": (p or {}).get("name") or (s["signature"][0][:60] if s["signature"] else s["site"])}


def _reloading():
    return "reload_usecases" in tasks.pending("reload_usecases")


@api.get("/usecases")
def usecases(q: str | None = None, page: int = 1, per_page: int = 50, reload: bool = False):
    """The usecases of the log. The list reloads in the background when older than 12 h (or on reload=true)."""
    if (reload or refresh.list_is_stale()) and not _reloading():
        tasks.add("reload_usecases", {"days": 30}, key="reload_usecases")
    reloading = _reloading()
    out = store.usecase_list(q=q, page=max(1, page), per_page=min(200, max(1, per_page)))
    at = store.listed_at()
    return {**out, "listed_at": at.isoformat(timespec="seconds") if at else None, "reloading": bool(reloading),
            "running": refresh.running(), "skipped": bq.skipped()}


@api.get("/usecases/{usecase}")
def usecase(usecase: str):
    """One usecase with its call-sites, from the log's days processed so far."""
    uc = None if usecase == "None" else usecase
    u = store.get_usecase(uc)
    if not u:
        raise HTTPException(404, "unknown usecase")
    sites = store.by_usecase(uc)
    from modules.models import audits
    for s in sites:
        s["decisions"] = {"models": audits.knowledge(s["site"], s), "router": s["choice"]}
    u = {**u, "decisions": {"models": audits.usecase_rollup(uc)}}
    return {**u, "sites": [{**_with_profile(s), "cache": _cache_decision(s["site"])} for s in sites],
            "analysing": bool(refresh.running()), "days": days_status()}


def days_status():
    """Which days of the window are processed, queued, or missing."""
    done, missing = store.days_done(), refresh.missing_days()
    return {"window": refresh.window(), "done": sorted(done), "queued": missing if refresh.running() else [],
            "missing": [] if refresh.running() else missing}


@api.post("/usecases/{usecase}/analyse")
def analyse_usecase(usecase: str):
    """Process the log's days not processed yet (every usecase: one read per day serves them all)."""
    return {"queued": refresh.start(), "days": days_status()}


@api.get("/days")
def days():
    return days_status()


@api.get("/{site}")
def one(site: str):
    s = store.get(site)
    if not s:
        raise HTTPException(404, "unknown call-site")
    from modules.models import audits
    return {**_with_profile(s), "decisions": {"cache": _cache_decision(site), "models": audits.knowledge(site, s),
                                              "router": s["choice"]}}
