"""Follow an n8n execution through the public API and describe it in plain lines.

The orchestrator workflow answers its webhook with `{"executionId": ...}` and
saves progress after every node, so polling `GET /executions/{id}` shows which
sub-workflow has finished. When the run ends, each sub-execution is read once
to report what it produced (drafts, decision notes, recap document).

Everything here except `fetch_execution` is a pure function of the JSON n8n
returns, so it is unit-tested without a server.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from ..connectors import n8n as n8n_conn

FINISHED = {"success", "error", "crashed", "canceled", "cancelled"}
SUBWORKFLOW_TYPES = {"n8n-nodes-base.executeWorkflow"}

# Friendly names for the orchestrator's steps; unknown steps show their node name.
STEP_LABELS = {
    "Run Drafting": "Replies & meeting proposals",
    "Run Events": "Events, webinars, courses",
    "Run Recap": "Recap document",
}

# What to count inside each sub-workflow once it has finished: (node, label).
SUB_COUNTS = {
    "Run Drafting": [("Search Unprocessed Emails", "emails from tracked senders"),
                     ("Create Gmail Draft", "reply drafts"),
                     ("Create Approval Draft", "BOOK? notes")],
    "Run Events": [("Search Event Emails", "keyword matches"),
                   ("Skip Already Checked", "new"),
                   ("Create Decision Draft", "FREE/CONFLICT notes")],
    "Run Recap": [("Get many messages", "emails summarised")],
}


# ------------------------------------------------------------------ parsing
def _run_data(execution: dict[str, Any]) -> dict[str, Any]:
    data = execution.get("data")
    if not isinstance(data, dict):
        return {}
    return ((data.get("resultData") or {}).get("runData")) or {}


def _items(task: dict[str, Any], output: int = 0) -> list[dict[str, Any]]:
    main = ((task.get("data") or {}).get("main")) or []
    if len(main) <= output or not main[output]:
        return []
    return main[output]


def item_count(execution: dict[str, Any], node: str) -> int:
    return sum(len(_items(t)) for t in _run_data(execution).get(node, []) or [])


def steps(execution: dict[str, Any]) -> list[str]:
    """Sub-workflow calls in canvas order (left to right)."""
    nodes = ((execution.get("workflowData") or {}).get("nodes")) or []
    subs = [n for n in nodes if n.get("type") in SUBWORKFLOW_TYPES and not n.get("disabled")]
    if subs:
        return [n["name"] for n in sorted(subs, key=lambda n: (n.get("position") or [0, 0])[0])]
    return [s for s in STEP_LABELS]


def step_state(execution: dict[str, Any], node: str) -> tuple[str, str, float | None]:
    """('done'|'failed'|'pending', error message, seconds)."""
    tasks = _run_data(execution).get(node) or []
    if not tasks:
        return "pending", "", None
    t = tasks[-1]
    secs = (t.get("executionTime") or 0) / 1000.0
    if t.get("error"):
        return "failed", str((t["error"] or {}).get("message") or "error"), secs
    for it in _items(t):
        err = (it.get("json") or {}).get("error")
        if err:   # the call continued on error: the sub-workflow failed
            msg = err.get("message") if isinstance(err, dict) else str(err)
            return "failed", str(msg), secs
    return "done", "", secs


def sub_execution_ref(execution: dict[str, Any], node: str) -> dict[str, str] | None:
    for t in _run_data(execution).get(node) or []:
        sub = ((t.get("metadata") or {}).get("subExecution")) or None
        if sub and sub.get("executionId"):
            return {"executionId": str(sub["executionId"]), "workflowId": str(sub.get("workflowId", ""))}
    return None


def summarize_sub(step: str, sub: dict[str, Any]) -> str:
    parts = [f"{item_count(sub, node)} {label}" for node, label in SUB_COUNTS.get(step, [])
             if node in _run_data(sub)]
    doc = _run_data(sub).get("Create Recap Doc")
    if doc:
        its = _items(doc[-1])
        did = (its[0].get("json") or {}).get("id") if its else None
        if did:
            parts.append(f"https://docs.google.com/document/d/{did}/edit")
    return ", ".join(parts)


def _elapsed(execution: dict[str, Any]) -> str:
    try:
        start = datetime.fromisoformat(str(execution.get("startedAt")).replace("Z", "+00:00"))
        stop_raw = execution.get("stoppedAt")
        stop = (datetime.fromisoformat(str(stop_raw).replace("Z", "+00:00")) if stop_raw
                else datetime.now(start.tzinfo))
        s = int((stop - start).total_seconds())
        return f"{s // 60:02d}:{s % 60:02d}"
    except (TypeError, ValueError):
        return "--:--"


def render(execution: dict[str, Any], summaries: dict[str, str] | None = None) -> list[str]:
    status = str(execution.get("status") or ("success" if execution.get("finished") else "running"))
    lines = [f"n8n execution #{execution.get('id')} · {status} · {_elapsed(execution)}", ""]
    running_marked = False
    for node in steps(execution):
        state, err, secs = step_state(execution, node)
        label = STEP_LABELS.get(node, node)
        if state == "pending":
            if status not in FINISHED and not running_marked:
                tag, running_marked = "running", True
            else:
                tag = "skipped" if status in FINISHED else "waiting"
            lines.append(f"  [{tag:<7}] {label}")
            continue
        detail = f"{secs:.1f}s" if secs is not None else ""
        extra = (summaries or {}).get(node, "")
        if state == "failed":
            extra = f"FAILED: {err}"
        lines.append(f"  [{state:<7}] {label:<30} {detail:>7}" + (f"  {extra}" if extra else ""))
    top_err = ((((execution.get("data") or {}) if isinstance(execution.get("data"), dict) else {})
               .get("resultData") or {}).get("error") or {})
    if top_err.get("message"):
        lines += ["", f"n8n error: {top_err['message']}"]
    return lines


def failed_steps(execution: dict[str, Any]) -> list[str]:
    out = []
    for node in steps(execution):
        state, err, _ = step_state(execution, node)
        if state == "failed":
            out.append(f"{STEP_LABELS.get(node, node)}: {err}")
    return out


# ------------------------------------------------------------------ network
def fetch_execution(base: str, execution_id: str, full: bool = False) -> dict[str, Any] | None:
    params = {"includeData": "true"}
    if full:
        params["ignoreDataSizeLimit"] = "true"
    with httpx.Client(timeout=n8n_conn.TIMEOUT, follow_redirects=True) as c:
        r = c.get(f"{base}/api/v1/executions/{execution_id}", headers=n8n_conn._headers(),
                  params=params)
    if r.status_code == 404:
        return None
    if r.status_code in (401, 403):
        raise n8n_conn.key_rejected(r, f"reading execution {execution_id}")
    r.raise_for_status()
    return r.json()


def find_sub_execution(base: str, execution: dict[str, Any], node: str) -> dict[str, Any] | None:
    ref = sub_execution_ref(execution, node)
    if ref:
        return fetch_execution(base, ref["executionId"], full=True)
    # Fallback: the first execution of the called workflow after the parent started.
    wf_nodes = ((execution.get("workflowData") or {}).get("nodes")) or []
    target = next((n for n in wf_nodes if n.get("name") == node), None)
    wid = (((target or {}).get("parameters") or {}).get("workflowId") or {})
    wid = wid.get("value") if isinstance(wid, dict) else wid
    if not wid:
        return None
    with httpx.Client(timeout=n8n_conn.TIMEOUT, follow_redirects=True) as c:
        r = c.get(f"{base}/api/v1/executions", headers=n8n_conn._headers(),
                  params={"workflowId": str(wid), "limit": "5"})
    if r.status_code >= 400:
        return None
    started = str(execution.get("startedAt") or "")
    cands = [e for e in (r.json().get("data") or []) if str(e.get("startedAt") or "") >= started]
    if not cands:
        return None
    first = min(cands, key=lambda e: str(e.get("startedAt")))
    return fetch_execution(base, str(first["id"]), full=True)


def stop_execution(base: str, execution: dict[str, Any] | None, execution_id: str) -> list[str]:
    """Stop the orchestrator run and any sub-workflow run it started. Returns log lines."""
    lines: list[str] = []
    with httpx.Client(timeout=n8n_conn.TIMEOUT, follow_redirects=True) as c:
        r = c.post(f"{base}/api/v1/executions/{execution_id}/stop", headers=n8n_conn._headers())
        lines.append(f"stop execution #{execution_id}: HTTP {r.status_code}"
                     + ("" if r.status_code < 400 else f" {r.text[:160]}"))
        started = str((execution or {}).get("startedAt") or "")
        nodes = (((execution or {}).get("workflowData") or {}).get("nodes")) or []
        for n in nodes:
            if n.get("type") not in SUBWORKFLOW_TYPES:
                continue
            wid = ((n.get("parameters") or {}).get("workflowId") or {})
            wid = wid.get("value") if isinstance(wid, dict) else wid
            if not wid:
                continue
            body: dict[str, Any] = {"status": ["running", "waiting", "queued"], "workflowId": str(wid)}
            if started:
                body["startedAfter"] = started
            rr = c.post(f"{base}/api/v1/executions/stop", headers=n8n_conn._headers(), json=body)
            stopped = ""
            try:
                stopped = str((rr.json() or {}).get("stopped", ""))
            except ValueError:
                pass
            lines.append(f"stop {STEP_LABELS.get(n.get('name'), n.get('name'))}: HTTP {rr.status_code}"
                         + (f", {stopped} stopped" if stopped else ""))
    return lines
