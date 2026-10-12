"""Call-site profiles as a flat list (the records live in core.identity.store; this is a convenience listing)."""
from fastapi import APIRouter, Depends, HTTPException

from app.web.public import require_key
from core.identity import store

api = APIRouter(prefix="/api/suite/profiles", dependencies=[Depends(require_key)], include_in_schema=False)


@api.get("")
def listing(usecase: str | None = None, q: str | None = None):
    recs = [r for r in store.all_sites() if r["profile"]]
    if usecase:
        recs = [r for r in recs if (r["usecase"] or "None") == usecase]
    if q:
        ql = q.lower()
        recs = [r for r in recs if ql in (r["name"] or "").lower() or ql in (r["does"] or "").lower() or ql in (r["usecase"] or "").lower()]
    return {"total": len(recs), "profiles": [{**r["profile"], "site": r["site"], "usecase": r["usecase"]} for r in recs]}


@api.get("/{site}")
def one(site: str):
    r = store.get(site)
    if not r or not r["profile"]:
        raise HTTPException(404, "no profile for this call-site")
    return {**r["profile"], "site": r["site"], "usecase": r["usecase"]}
