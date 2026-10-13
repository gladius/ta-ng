"""The scheduled jobs: when each last ran and how it went, when it runs next; turn one on or off, or run it now."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.web.public import require_key
from core import schedule

api = APIRouter(prefix="/api/suite/schedules", dependencies=[Depends(require_key)], include_in_schema=False)


class EnabledIn(BaseModel):
    enabled: bool


def _known(name):
    if name not in schedule.JOBS:
        raise HTTPException(404, "unknown job")


@api.get("")
def listing():
    return {"jobs": schedule.status()}


@api.post("/{name}/run")
def run(name: str):
    _known(name)
    return {"task_id": schedule.run_now(name), "jobs": schedule.status()}


@api.post("/{name}")
def enable(name: str, body: EnabledIn):
    _known(name)
    schedule.set_enabled(name, body.enabled)
    return {"jobs": schedule.status()}
