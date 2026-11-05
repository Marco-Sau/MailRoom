"""n8n runner: start a workflow through its webhook, then follow it to the end.

The orchestrator answers the webhook at once with `{"executionId": ...}` (a
Respond to Webhook node right after the trigger). From then on the run is
followed through the n8n public API with the n8n API key, and the run log in
the app is rewritten every few seconds, so the Run tab shows which step is
going and what each one produced.

The app never edits workflows here - n8n stays the source of truth for them.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from .. import app_settings, db
from ..connectors.n8n_webhook_key import HEADER_NAME, SECRET_KEY
from ..models import RunResult, RunSpec
from ..secrets_store import store
from . import control
from . import n8n_tracker as tracker
from .base import Runner

TIMEOUT = httpx.Timeout(60.0, connect=10.0)
POLL_SECONDS = 2.5
MAX_FOLLOW_SECONDS = 20 * 60
NOT_FOUND_GRACE_SECONDS = 20


class N8nRunner(Runner):
    name = "n8n"

    def __init__(self, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._sleep, self._clock = sleep, clock

    def _wait(self, seconds: float) -> None:
        """Sleep, but wake up early when Stop is pressed."""
        step = 0.25
        waited = 0.0
        while waited < seconds and not control.stop_requested():
            self._sleep(step)
            waited += step

    def _stopped(self, run_id: int, base: str, execution_id: str,
                 execution: dict[str, Any] | None, head: list[str]):
        lines = tracker.stop_execution(base, execution, execution_id)
        final = tracker.fetch_execution(base, execution_id) or execution
        body = head + (tracker.render(final) if final else []) + ["", "STOPPED by you."] + lines + [
            "Drafts already created stay in Gmail Drafts; nothing was sent."]
        return "cancelled", {"execution_id": execution_id, "n8n_status": "stopped"}, body, "stopped by you"

    # ------------------------------------------------------------- start
    def _headers(self, spec: RunSpec, log: list[str]) -> dict[str, str]:
        if spec.auth == "key":
            key = store.get(SECRET_KEY)
            if not key:
                raise RuntimeError(
                    "This job needs the webhook key, but none is stored. Create it in "
                    "Settings → Advanced → Webhook key.")
            return {HEADER_NAME: key}
        if spec.auth == "header":
            token = store.get("n8n_api_key")
            if token:
                return {"X-N8N-API-KEY": token}
            log.append("warning: auth=header but n8n_api_key is not set")
        return {}

    def _start(self, spec: RunSpec, log: list[str]) -> httpx.Response:
        if not spec.webhook_url:
            raise ValueError("job has no webhook_url")
        try:
            with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as c:
                body = {**(spec.params or {}), **({"hours": spec.hours} if spec.hours else {})}
                r = c.post(spec.webhook_url, json=body, headers=self._headers(spec, log))
        except httpx.ConnectError as exc:
            raise ConnectionError(
                f"Could not open a connection to {spec.webhook_url} ({exc}). Nothing is "
                f"listening on that host:port — confirm where n8n runs (Settings → Advanced → "
                f"n8n workflows) and set it in Settings → Advanced → Connections → n8n run webhook.") from exc
        log.append(f"HTTP {r.status_code}")
        if r.status_code == 404:
            if "not registered for" in r.text.lower():
                raise RuntimeError(
                    f"n8n has a webhook at {spec.webhook_url}, but for a different HTTP "
                    f"method. This app sends POST — set the Webhook node's method to POST "
                    f"and publish the workflow.")
            raise RuntimeError(
                f"n8n has no production webhook at {spec.webhook_url}. Either the path is "
                f"wrong, or the workflow was saved but not published (n8n 2.x serves "
                f"/webhook/ only from the published version).")
        if r.status_code in (401, 403):
            raise RuntimeError(
                f"n8n refused the request ({r.status_code}): the webhook requires the "
                f"MailRoom webhook key and the one sent was missing or wrong. Re-create it in "
                f"Settings → Advanced → Webhook key." if spec.auth == "key" else
                f"n8n refused the request ({r.status_code}): the webhook requires "
                f"authentication. Set auth: key on this job and create the key in "
                f"Settings → Advanced → Webhook key.")
        if r.status_code >= 400:
            raise RuntimeError(f"n8n returned {r.status_code}: {r.text[:300]}")
        return r

    # ------------------------------------------------------------- follow
    def _follow(self, run_id: int, base: str, execution_id: str,
                head: list[str]) -> tuple[str, dict[str, Any], list[str], str | None]:
        started = self._clock()
        execution: dict[str, Any] | None = None
        while True:
            if control.stop_requested():
                return self._stopped(run_id, base, execution_id, execution, head)
            try:
                execution = tracker.fetch_execution(base, execution_id)
            except PermissionError as exc:
                # The webhook was accepted: n8n runs the job on its own; only the follow-up failed.
                return ("failed", {"execution_id": execution_id}, head + [
                    f"n8n accepted the run as execution #{execution_id} and runs it on its own,",
                    "but MailRoom cannot read its progress or result:", f"  {exc}",
                    f"Check the result in n8n → Executions (#{execution_id})."],
                    f"could not read n8n execution {execution_id}: API key rejected")
            elapsed = self._clock() - started
            if execution is None:
                if elapsed > NOT_FOUND_GRACE_SECONDS:
                    raise RuntimeError(f"n8n has no execution #{execution_id}")
            else:
                db.update_run_progress(run_id, {"execution_id": execution_id},
                                       head + tracker.render(execution))
                if str(execution.get("status")) in tracker.FINISHED or execution.get("finished"):
                    break
            max_follow = int(app_settings.local("n8nFollowMinutes")) * 60
            if elapsed > max_follow:
                body = head + (tracker.render(execution) if execution else [])
                return ("failed", {"execution_id": execution_id}, body + [
                    "", f"Stopped following after {max_follow // 60} min; the run "
                        f"may still be going in n8n."], "still running after 20 min")
            self._wait(float(app_settings.local("n8nPollSeconds")))

        # Final pass with full data, plus what each sub-workflow produced.
        execution = tracker.fetch_execution(base, execution_id, full=True) or execution
        summaries: dict[str, str] = {}
        stats: dict[str, Any] = {"execution_id": execution_id,
                                 "n8n_status": execution.get("status")}
        for step in tracker.steps(execution):
            if tracker.step_state(execution, step)[0] != "done":
                continue
            try:
                sub = tracker.find_sub_execution(base, execution, step)
            except (httpx.HTTPError, PermissionError):
                sub = None
            if sub:
                summaries[step] = tracker.summarize_sub(step, sub)
                for node, label in tracker.SUB_COUNTS.get(step, []):
                    stats[label] = tracker.item_count(sub, node)
        body = head + tracker.render(execution, summaries)
        failures = tracker.failed_steps(execution)
        ok = str(execution.get("status")) == "success" and not failures
        body.append("")
        body.append("Finished. Review the drafts in Gmail → Drafts; nothing was sent."
                    if ok else "Finished with errors — open the execution in n8n for details.")
        return ("succeeded" if ok else "failed", stats, body,
                None if ok else ("; ".join(failures) or f"n8n status {execution.get('status')}"))

    # ------------------------------------------------------------- run
    def run(self, spec: RunSpec) -> RunResult:
        run_id = db.start_run(spec.job_id, spec.name, "n8n", None, None)
        control.begin(run_id, "n8n")
        started = datetime.now(timezone.utc)
        log = [f"POST {spec.webhook_url}" + (" (with the webhook key)" if spec.auth == "key" else "")
               + (f" · look back {spec.hours} h" if spec.hours else "")]
        stats: dict[str, Any] = {}
        try:
            r = self._start(spec, log)
            try:
                payload = r.json()
            except ValueError:
                payload = {}
            execution_id = str(payload.get("executionId") or "") if isinstance(payload, dict) else ""
            if not execution_id:
                log += [r.text[:500], "",
                        "n8n accepted the request but did not return an execution id, so the "
                        "app cannot follow the run. The workflow should answer with a Respond "
                        "to Webhook node returning {executionId}."]
                db.finish_run(run_id, "succeeded", {"http_status": r.status_code}, log)
                return RunResult(run_id=run_id, job_id=spec.job_id, status="succeeded",
                                 started_at=started, finished_at=datetime.now(timezone.utc),
                                 stats={"http_status": r.status_code}, log=log)

            base = spec.webhook_url.split("/webhook/")[0].rstrip("/")
            exec_url = f"{base}/workflow/{payload.get('workflowId', '')}/executions/{execution_id}"
            head = log + [f"Started: n8n execution #{execution_id}", exec_url, ""]
            control.update(execution_id=execution_id)
            db.update_run_progress(run_id, {"execution_id": execution_id}, head)
            if not store.get("n8n_api_key"):
                head.append("No n8n API key in Settings, so progress cannot be shown. "
                            "The run continues in n8n.")
                db.finish_run(run_id, "succeeded", {"execution_id": execution_id}, head)
                return RunResult(run_id=run_id, job_id=spec.job_id, status="succeeded",
                                 started_at=started, finished_at=datetime.now(timezone.utc),
                                 stats={"execution_id": execution_id}, log=head)

            status, stats, log, error = self._follow(run_id, base, execution_id, head)
            db.finish_run(run_id, status, stats, log, error)
            return RunResult(run_id=run_id, job_id=spec.job_id, status=status,
                             started_at=started, finished_at=datetime.now(timezone.utc),
                             stats=stats, error=error, log=log)
        except Exception as exc:  # noqa: BLE001
            log.append(f"FAILED: {exc}")
            db.finish_run(run_id, "failed", stats, log, str(exc))
            return RunResult(run_id=run_id, job_id=spec.job_id, status="failed",
                             started_at=started, finished_at=datetime.now(timezone.utc),
                             error=str(exc), log=log)
        finally:
            control.end()
