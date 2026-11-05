"""Run tab → Automatic runs: read and save the schedule; Save installs or removes the LaunchAgent."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from .. import db, schedule

router = APIRouter(prefix="/api/schedule", tags=["schedule"])


@router.get("")
def get_schedule():
    return schedule.state()


@router.put("")
def put_schedule(body: dict[str, Any]):
    try:
        st = schedule.save(body)
    except schedule.ScheduleError as exc:
        raise HTTPException(400, str(exc)) from exc
    c = st["config"]
    db.audit("user", "schedule_saved", c.get("job_id") or "-",
             ("automatic: " + ",".join(schedule.DAY_NAMES[d - 1] for d in c["days"]) + " " + ",".join(c["times"]))
             if c["enabled"] else "manual (background job removed)")
    return st


@router.post("/kick")
def kick():
    if not schedule.load()["enabled"] or not schedule.installed()["file"]:
        raise HTTPException(409, "turn on Automatic and save first")
    try:
        schedule.kick()
    except schedule.ScheduleError as exc:
        raise HTTPException(500, str(exc)) from exc
    db.audit("user", "autorun_kicked", schedule.load()["job_id"], "started from the Run tab")
    return {"started": True}


@router.get("/news")
def news():
    """Automatic runs you have not seen yet (the in-app notification). No launchctl call: cheap to poll."""
    return schedule.unseen()


@router.post("/seen")
def seen():
    return schedule.mark_seen()
