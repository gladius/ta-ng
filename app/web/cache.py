"""The cache module's JSON: the stored report at three levels (org, usecase, call-site) and the refresh."""
from fastapi import APIRouter, Depends, HTTPException

from app.web.validations import require_key
from core.ledger import refresh

api = APIRouter(prefix="/api/suite/cache", dependencies=[Depends(require_key)], include_in_schema=False)


def _report():
    rep = refresh.latest()
    if not rep:
        raise HTTPException(404, "no cache report yet: refresh first")
    return rep


def _brief(u, s):
    p = s.get("profile") or {}
    return {"usecase": u["usecase"], "site": s["site"], "name": p.get("name") or (s["anchors"][0][:60] if s["anchors"] else s["site"]),
            "does": p.get("does", ""), "calls": s["total"]["calls"], "share": s["story"]["share"], "cause": s["story"]["cause"],
            "action": s["story"]["action"], "worth": s["saving"], "proof": s["story"]["proof"]}


@api.get("/overview")
def overview():
    """The org level: the reuse share and its trend, the biggest opportunities, what dropped, and every usecase."""
    rep = _report()
    sites = [(u, s) for u in rep["usecases"].values() for s in u["sites"]]
    opportunities = [_brief(u, s) for u, s in sorted(sites, key=lambda x: -x[1]["saving"]) if s["saving"] > 0][:10]
    dropped = [_brief(u, s) | {"trend": s["trend"]} for u, s in sites if s.get("trend") and s["trend"]["delta"] <= -0.05]
    return {"built_at": rep.get("built_at"), "window": rep["days"], "daily": rep.get("daily", {}), "total": rep["total"],
            "saving": rep["saving"], "trend": rep.get("trend"), "opportunities": opportunities,
            "dropped": sorted(dropped, key=lambda b: b["trend"]["delta"]),
            "usecases": [{"usecase": u["usecase"], "total": u["total"], "saving": u["saving"], "trend": u.get("trend"),
                          "sites": len(u["sites"]), "unsure": any(s["unsure"] for s in u["sites"]),
                          "causes": sorted({s["story"]["cause"] for s in u["sites"] if s["story"]["cause"]})}
                         for u in sorted(rep["usecases"].values(), key=lambda u: (-u["saving"], -u["total"]["cost"]))],
            "status": refresh.status()}


@api.get("/usecases/{usecase}")
def usecase(usecase: str):
    rep = _report()
    u = rep["usecases"].get(usecase)
    if not u:
        raise HTTPException(404, "unknown usecase")
    return {"built_at": rep.get("built_at"), "window": rep["days"], **u}


@api.get("/sites/{site}")
def site(site: str):
    rep = _report()
    for u in rep["usecases"].values():
        for s in u["sites"]:
            if s["site"] == site:
                return {"built_at": rep.get("built_at"), "window": rep["days"], "usecase": u["usecase"], **s}
    raise HTTPException(404, "unknown call-site")


@api.post("/refresh")
def start_refresh(days: int = 30):
    return {"started": refresh.start(days), **refresh.status()}


@api.get("/refresh")
def refresh_status():
    return refresh.status()
