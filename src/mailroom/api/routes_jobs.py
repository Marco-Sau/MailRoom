from __future__ import annotations

import threading

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from .. import db, run_lock, run_state
from ..jobs import get_job, load_jobs
from ..providers import engine_models
from ..runners import get_runner

router = APIRouter(prefix="/api", tags=["jobs"])

# One run at a time: the pipeline writes to SQLite and to your inbox, and
# concurrent runs on the same mailbox would duplicate drafts. `run_lock` extends this to
# other processes: the background automatic run and `mailroom run`.
_run_lock = threading.Lock()


def _external() -> dict | None:
    return None if _run_lock.locked() else run_lock.held_elsewhere()


def _reconcile() -> None:
    from ..runners import control
    try:
        run_state.reconcile(starting=_run_lock.locked() and not control.current())
    except Exception:  # noqa: BLE001 - never block the page over bookkeeping
        pass


@router.get("/jobs")
def list_jobs():
    _reconcile()
    out = []
    for j in load_jobs():
        d = j.model_dump()
        d["effective_model"] = engine_models.effective_for_job(j.runner, getattr(j, "provider", None),
                                                               getattr(j, "model", None))
        out.append(d)
    return out


class RunOptions(BaseModel):
    #: look back this many hours (1 h to 7 days); omitted = standard windows
    hours: int | None = Field(default=None, ge=1, le=168)


def _execute(job_id: str, hours: int | None = None) -> None:
    spec = get_job(job_id)
    spec.hours = hours
    if not _run_lock.acquire(blocking=False):
        db.audit("api", "run_rejected", job_id, "another run is already in flight")
        return
    try:
        if not run_lock.try_acquire("app", job=job_id, runner=spec.runner):
            db.audit("api", "run_rejected", job_id, "an automatic run is already in flight")
            return
        try:
            get_runner(spec.runner).run(spec)
        finally:
            run_lock.release()
    finally:
        _run_lock.release()


@router.post("/jobs/{job_id}/run")
def run_job(job_id: str, background: BackgroundTasks, options: RunOptions | None = None):
    try:
        spec = get_job(job_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    if _run_lock.locked():
        raise HTTPException(409, "a run is already in progress")
    ext = _external()
    if ext:
        who = "an automatic run" if ext.get("owner") == "automatic" else "a run started from the command line"
        raise HTTPException(409, f"{who} is in progress: wait for it, or press Stop")
    hours = options.hours if options else None
    background.add_task(_execute, job_id, hours)
    db.audit("user", "run_requested", job_id, spec.name + (f" ({hours} h)" if hours else ""))
    return {"queued": True, "job_id": job_id, "name": spec.name, "hours": hours}


@router.get("/jobs/status")
def run_status():
    from ..runners import control
    _reconcile()
    ext = _external()
    stopping = control.stop_requested() or bool(ext and control._stop_file().exists())
    return {"busy": _run_lock.locked() or bool(ext), "stopping": stopping,
            "automatic": bool(ext and ext.get("owner") == "automatic")}


@router.post("/jobs/stop")
def stop_run():
    """Ask the running job to stop. n8n runs are stopped in n8n (orchestrator and
    its sub-workflows); app-side pipelines stop before their next step."""
    from ..runners import control
    cur = control.request_stop()
    if not cur:
        raise HTTPException(409, "nothing is running")
    db.audit("user", "run_stop_requested", str(cur.get("run_id")), cur.get("runner"))
    return {"stopping": True, **cur}
