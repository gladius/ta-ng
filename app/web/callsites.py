"""Call-sites: usecase -> call-site -> identity (the fingerprint), profile, and what each module decided."""
from fastapi import APIRouter, Depends, HTTPException

from app.web.validations import require_key
from core import profiler
from core.identity import store
from core.ledger import refresh

api = APIRouter(prefix="/api/suite/callsites", dependencies=[Depends(require_key)], include_in_schema=False)


def _cache_decision(site):
    """The cache module's story for the site, from its stored report, or None."""
    rep = refresh.latest() or {}
    for u in rep.get("usecases", {}).values():
        for s in u["sites"]:
            if s["site"] == site:
                return {"cause": s["story"]["cause"], "action": s["story"]["action"], "worth": s["story"]["worth"],
                        "proof": s["story"]["proof"], "share": s["story"]["share"], "models": sorted(s["models"]),
                        "built_at": rep.get("built_at")}
    return None


def _with_profile(s):
    p = profiler.get(s["site"])
    return {**s, "profile": p, "name": (p or {}).get("name") or (s["signature"][0][:60] if s["signature"] else s["site"])}


@api.get("/usecases")
def usecases(q: str | None = None, page: int = 1, per_page: int = 50):
    out = store.usecases(q=q, page=max(1, page), per_page=min(200, max(1, per_page)))
    profiled = {p["usecase"]: 0 for p in profiler.listing(per_page=200)["profiles"]}
    for p in profiler.listing(per_page=200)["profiles"]:
        profiled[p["usecase"]] = profiled.get(p["usecase"], 0) + 1
    for u in out["usecases"]:
        u["profiled"] = profiled.get(u["usecase"], 0)
    return out


@api.get("/usecases/{usecase}")
def usecase(usecase: str):
    uc = None if usecase == "None" else usecase
    sites = store.by_usecase(uc)
    if not sites:
        raise HTTPException(404, "unknown usecase")
    return {"usecase": uc, "sites": [{**_with_profile(s), "cache": _cache_decision(s["site"])} for s in sites]}


@api.get("/{site}")
def one(site: str):
    s = store.get(site)
    if not s:
        raise HTTPException(404, "unknown call-site")
    return {**_with_profile(s), "decisions": {"cache": _cache_decision(site), "models": None, "router": None}}
