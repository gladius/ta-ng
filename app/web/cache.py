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


@api.get("/overview")
def overview():
    rep = _report()
    return {"built_at": rep.get("built_at"), "window": rep["days"], "daily": rep.get("daily", {}), "total": rep["total"],
            "saving": rep["saving"],
            "usecases": [{"usecase": u["usecase"], "total": u["total"], "saving": u["saving"], "days": u["days"],
                          "sites": len(u["sites"]),
                          "causes": sorted({m["diagnosis"]["cause"] for s in u["sites"] for m in s["models"].values()
                                            if m.get("diagnosis")})}
                         for u in sorted(rep["usecases"].values(), key=lambda u: -u["total"]["cost"])],
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
def start_refresh(days: int = 14):
    return {"started": refresh.start(days), **refresh.status()}


@api.get("/refresh")
def refresh_status():
    return refresh.status()
