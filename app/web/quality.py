"""The audit of the audit: the day's checks (modules/quality)."""
from fastapi import APIRouter, Depends, HTTPException

from app.web.public import require_key
from modules import quality

api = APIRouter(prefix="/api/suite/quality", dependencies=[Depends(require_key)], include_in_schema=False)


@api.get("")
def report(day: str | None = None):
    r = quality.report(day)
    if not r:
        raise HTTPException(404, "no day checked yet")
    from core.identity import store
    names = {}
    for item in r["failures"] + r["spot_checks"]:
        if item["site"] and item["site"] not in names:
            rec = store.get(item["site"])
            names[item["site"]] = ((rec or {}).get("profile") or {}).get("name") or item["site"]
        item["name"] = names.get(item["site"])
    return r
