"""Job catalog: config/jobs.yaml -> RunSpec. Each job is one button in the UI."""
from __future__ import annotations

import yaml

from .models import RunSpec
from .settings import settings


def load_jobs(path=None) -> list[RunSpec]:
    p = path or settings.jobs_path
    with open(p, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    out = []
    for j in data.get("jobs", []):
        j = dict(j)
        j["job_id"] = j.pop("id", j.get("job_id"))
        if j.get("runner") == "n8n":
            try:
                from . import app_settings
                hook = app_settings.local_explicit("n8nWebhookUrl")   # Settings → Connections wins
                if hook:
                    j["webhook_url"] = hook
            except Exception:  # noqa: BLE001
                pass
        out.append(RunSpec(**j))
    return out


def get_job(job_id: str) -> RunSpec:
    for j in load_jobs():
        if j.job_id == job_id:
            return j
    raise KeyError(f"unknown job: {job_id}")
