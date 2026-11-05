"""Close out runs left as 'running' by a process that no longer exists.

A run's row is marked 'running' when it starts and gets its final status when it ends. If the
process dies in between (the app quit mid-run, macOS stopped the background job), the row would
say 'running' forever. Whenever nothing is running, such rows become 'interrupted'.
"""
from __future__ import annotations

from typing import Any

from . import db, run_lock

REASON = "interrupted: the app or the background job closed before the run finished (drafts already created stay in Gmail Drafts; nothing was sent)"


def reconcile(starting: bool = False) -> list[dict[str, Any]]:
    """`starting`: this process is about to start a run (lock taken, no run id yet): change nothing."""
    from .runners import control
    if starting:
        return []
    keep: set[int] = set()
    cur = control.current()
    if run_lock.holding() and not cur.get("run_id"):
        return []                                  # this process is between start_run and begin
    if cur.get("run_id"):
        keep.add(int(cur["run_id"]))
    other = run_lock.held_elsewhere()
    if other is not None:
        if not other.get("run_id"):
            return []                              # another process is starting its run
        keep.add(int(other["run_id"]))
    rows = db.mark_interrupted(keep, REASON)
    for r in rows:
        db.audit("app", "run_interrupted", str(r["id"]), r["job_name"])
        if str(r.get("job_name", "")).endswith("(automatic)"):
            from . import schedule
            schedule.note_interrupted(r)
    return rows
