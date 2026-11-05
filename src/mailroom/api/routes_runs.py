from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from .. import db

router = APIRouter(prefix="/api", tags=["runs"])


@router.get("/runs")
def runs(limit: int = 30):
    out = []
    for r in db.list_runs(limit):
        r["stats"] = json.loads(r.pop("stats_json", "{}"))
        r.pop("log_json", None)
        out.append(r)
    return out


@router.get("/runs/{run_id}")
def run_detail(run_id: int):
    r = db.get_run(run_id)
    if not r:
        raise HTTPException(404, "no such run")
    r["stats"] = json.loads(r.pop("stats_json", "{}"))
    r["log"] = json.loads(r.pop("log_json", "[]"))
    r["emails"] = db.run_emails(run_id)
    return r
