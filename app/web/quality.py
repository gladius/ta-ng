"""The audit of the audit: the day's checks (modules/quality)."""
from fastapi import APIRouter, Depends, HTTPException

from app.web.public import require_key
from modules import quality

api = APIRouter(prefix="/api/suite/quality", dependencies=[Depends(require_key)], include_in_schema=False)


@api.post("/run")
def run(day: str):
    """Check a processed day again (our own numbers; no read of the log)."""
    from core import tasks
    from core.identity import store
    if day not in store.days_done():
        raise HTTPException(404, "that day is not processed")
    return {"task_id": tasks.add("quality_day", {"day": day}, key="quality:" + day)}


@api.get("")
def report(day: str | None = None):
    from core import tasks
    r = quality.report(day)
    r["running"] = [k[len("quality:"):] for k in tasks.pending("quality:")]
    from core.identity import store
    names = {}
    for item in r["failures"]:
        if item["site"] and item["site"] not in names:
            rec = store.get(item["site"])
            names[item["site"]] = ((rec or {}).get("profile") or {}).get("name") or item["site"]
        item["name"] = names.get(item["site"])
    return r
