"""Usecases and call-sites: the cheap list (reloaded when stale), one usecase analysed on demand, one call-site's
identity, profile and decisions."""
import threading

from fastapi import APIRouter, Depends, HTTPException

from app.web.public import require_key
from core.identity import store
from core.ledger import refresh

api = APIRouter(prefix="/api/suite/callsites", dependencies=[Depends(require_key)], include_in_schema=False)
_list = {"running": False}


def _cache_decision(site):
    from modules.cache import query
    return query.decision(site)


def _with_profile(s):
    p = s.get("profile")
    return {**s, "name": (p or {}).get("name") or (s["signature"][0][:60] if s["signature"] else s["site"])}


def _reload_list():
    """Reload the usecases list in the background, once at a time."""
    if _list["running"]:
        return True

    def go():
        try:
            refresh.list_usecases()
        finally:
            _list["running"] = False
    _list["running"] = True
    threading.Thread(target=go, daemon=True).start()
    return True


@api.get("/usecases")
def usecases(q: str | None = None, page: int = 1, per_page: int = 50, reload: bool = False):
    """The usecases of the log. The list reloads in the background when older than 12 h (or on reload=true)."""
    reloading = _list["running"] or ((reload or refresh.list_is_stale()) and _reload_list())
    out = store.usecase_list(q=q, page=max(1, page), per_page=min(200, max(1, per_page)))
    at = store.listed_at()
    return {**out, "listed_at": at.isoformat(timespec="seconds") if at else None, "reloading": bool(reloading),
            "running": refresh.running()}


@api.get("/usecases/{usecase}")
def usecase(usecase: str):
    """One usecase with its call-sites; analysed in the background on first open, the answer saying `analysing`."""
    uc = None if usecase == "None" else usecase
    u = store.get_usecase(uc)
    if not u:
        raise HTTPException(404, "unknown usecase")
    if u["analysed_at"] is None and not u["analysing"] and not u["error"]:
        refresh.start(uc or "")
        u = store.get_usecase(uc)
    sites = store.by_usecase(uc)
    from modules.models import audits
    for s in sites:
        s["decisions"] = {"models": audits.knowledge(s["site"], s), "router": s["choice"]}
    u = {**u, "decisions": {"models": audits.usecase_rollup(uc)}}
    return {**u, "sites": [{**_with_profile(s), "cache": _cache_decision(s["site"])} for s in sites],
            "analysing": bool(u["analysing"]) or (uc or "") in refresh.running()}


@api.post("/usecases/{usecase}/analyse")
def analyse_usecase(usecase: str):
    uc = None if usecase == "None" else usecase
    if not store.get_usecase(uc):
        raise HTTPException(404, "unknown usecase")
    return {"started": refresh.start(uc or ""), "running": refresh.running()}


@api.get("/{site}")
def one(site: str):
    s = store.get(site)
    if not s:
        raise HTTPException(404, "unknown call-site")
    from modules.models import audits
    return {**_with_profile(s), "decisions": {"cache": _cache_decision(site), "models": audits.knowledge(site, s),
                                              "router": s["choice"]}}
