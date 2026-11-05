"""Webhook key header + following an n8n execution to the end (no network)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mailroom.models import RunSpec
from mailroom.runners import n8n_runner as mod
from mailroom.runners import n8n_tracker as tr

WF_NODES = [
    {"name": "Webhook", "type": "n8n-nodes-base.webhook", "position": [-384, 560]},
    {"name": "Run Drafting", "type": "n8n-nodes-base.executeWorkflow", "position": [64, 688],
     "parameters": {"workflowId": {"value": "D"}}},
    {"name": "Run Events", "type": "n8n-nodes-base.executeWorkflow", "position": [288, 688]},
    {"name": "Run Recap", "type": "n8n-nodes-base.executeWorkflow", "position": [512, 688]},
]


def task(items, ms=400, sub=None, error=None):
    t = {"executionTime": ms, "data": {"main": [[{"json": j} for j in items]]}}
    if sub:
        t["metadata"] = {"subExecution": {"executionId": sub, "workflowId": "x"}}
    if error:
        t["error"] = {"message": error}
    return t


def execution(status, run_data, eid="77"):
    return {"id": eid, "status": status, "finished": status == "success",
            "startedAt": "2026-10-04T16:00:00.000Z",
            "stoppedAt": "2026-10-04T16:01:02.000Z" if status != "running" else None,
            "workflowData": {"nodes": WF_NODES},
            "data": {"resultData": {"runData": run_data}}}


# ------------------------------------------------------------- tracker
def test_render_marks_first_unfinished_step_as_running():
    ex = execution("running", {"Run Drafting": [task([{}], sub="78")]})
    lines = tr.render(ex)
    assert lines[0].startswith("n8n execution #77 · running")
    body = "\n".join(lines)
    assert "[done   ] Replies & meeting proposals" in body
    assert "[running] Events, webinars, courses" in body
    assert "[waiting] Recap document" in body


def test_step_that_continued_on_error_is_reported_failed():
    ex = execution("success", {"Run Drafting": [task([{}])],
                               "Run Events": [task([{"error": {"message": "Gmail quota"}}])],
                               "Run Recap": [task([{}])]})
    assert tr.failed_steps(ex) == ["Events, webinars, courses: Gmail quota"]
    assert "FAILED: Gmail quota" in "\n".join(tr.render(ex))


def test_summaries_count_items_across_runs_and_link_the_doc():
    drafting = execution("success", {
        "Search Unprocessed Emails": [task([{}, {}, {}])],
        "Create Gmail Draft": [task([{}, {}]), task([{}])],        # node ran twice
        "Create Approval Draft": [task([{}])]}, eid="78")
    assert tr.summarize_sub("Run Drafting", drafting) == \
        "3 emails from tracked senders, 3 reply drafts, 1 BOOK? notes"
    recap = execution("success", {"Get many messages": [task([{}] * 12)],
                                  "Create Recap Doc": [task([{"id": "DOC1"}])]}, eid="80")
    assert tr.summarize_sub("Run Recap", recap) == \
        "12 emails summarised, https://docs.google.com/document/d/DOC1/edit"


def test_events_summary_skips_nodes_that_never_ran():
    ev = execution("success", {"Search Event Emails": [task([{}, {}])],
                               "Skip Already Checked": [task([])]}, eid="79")
    assert tr.summarize_sub("Run Events", ev) == "2 keyword matches, 0 new"


# ------------------------------------------------------------- runner
@pytest.fixture
def harness(monkeypatch):
    calls = {"progress": [], "finish": None, "headers": None}
    monkeypatch.setattr(mod.db, "start_run", lambda *a, **k: 5)
    monkeypatch.setattr(mod.db, "update_run_progress",
                        lambda rid, stats, log: calls["progress"].append(list(log)))
    monkeypatch.setattr(mod.db, "finish_run",
                        lambda rid, status, stats, log, error=None: calls.__setitem__(
                            "finish", (status, stats, log, error)))
    secrets = {"n8n_webhook_key": "K", "n8n_api_key": "A"}
    monkeypatch.setattr(mod.store, "get", lambda k: secrets.get(k))

    def start(status=202, body=None):
        body = body if body is not None else {"status": "started", "executionId": "77",
                                              "workflowId": "W"}
        text = json.dumps(body)
        resp = SimpleNamespace(status_code=status, text=text, content=text.encode(),
                               json=lambda: body)

        class C:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def post(self, url, json=None, headers=None):
                calls["headers"] = headers
                return resp
        monkeypatch.setattr(mod.httpx, "Client", lambda **kw: C())
    calls["start"] = start
    calls["secrets"] = secrets
    return calls


def _spec(auth="key"):
    return RunSpec(job_id="n8n", name="Gmail Assistant (n8n)", runner="n8n", auth=auth,
                   webhook_url="https://n8n.example/webhook/abc")


def test_key_is_sent_and_run_is_followed_to_the_end(harness, monkeypatch):
    harness["start"]()
    timeline = [
        None,                                                     # not visible yet
        execution("running", {}),
        execution("running", {"Run Drafting": [task([{}], sub="78")]}),
        execution("success", {"Run Drafting": [task([{}], sub="78")],
                              "Run Events": [task([{}], sub="79")],
                              "Run Recap": [task([{}], sub="80")]}),
    ]
    final = timeline[-1]
    monkeypatch.setattr(tr, "fetch_execution",
                        lambda base, eid, full=False: final if full else timeline.pop(0))
    subs = {
        "Run Drafting": execution("success", {"Search Unprocessed Emails": [task([{}])],
                                              "Create Gmail Draft": [task([{}])]}, "78"),
        "Run Events": execution("success", {"Search Event Emails": [task([{}])],
                                            "Skip Already Checked": [task([{}])],
                                            "Create Decision Draft": [task([{}])]}, "79"),
        "Run Recap": execution("success", {"Get many messages": [task([{}] * 4)],
                                           "Create Recap Doc": [task([{"id": "D"}])]}, "80"),
    }
    monkeypatch.setattr(tr, "find_sub_execution", lambda base, ex, step: subs[step])

    res = mod.N8nRunner(sleep=lambda s: None).run(_spec())

    assert harness["headers"] == {"X-Inbox-Pilot-Key": "K"}
    assert res.status == "succeeded", res.error
    assert res.stats["reply drafts"] == 1 and res.stats["FREE/CONFLICT notes"] == 1
    text = "\n".join(res.log)
    assert "Started: n8n execution #77" in text
    assert "https://n8n.example/workflow/W/executions/77" in text
    assert "1 reply drafts" in text and "docs.google.com/document/d/D" in text
    # progress was written while the run was going, with a running step visible
    assert any("[running]" in "\n".join(p) for p in harness["progress"])


def test_missing_key_fails_before_calling_n8n(harness):
    harness["start"]()
    harness["secrets"].pop("n8n_webhook_key")
    res = mod.N8nRunner(sleep=lambda s: None).run(_spec())
    assert res.status == "failed" and "Webhook key" in res.error
    assert harness["headers"] is None


def test_wrong_key_is_explained(harness):
    harness["start"](403, {"message": "Authorization data is wrong!"})
    res = mod.N8nRunner(sleep=lambda s: None).run(_spec())
    assert res.status == "failed" and "MailRoom webhook key" in res.error


def test_failed_sub_workflow_fails_the_run(harness, monkeypatch):
    harness["start"]()
    done = execution("success", {"Run Drafting": [task([{"error": {"message": "boom"}}])],
                                 "Run Events": [task([{}])], "Run Recap": [task([{}])]})
    monkeypatch.setattr(tr, "fetch_execution", lambda base, eid, full=False: done)
    monkeypatch.setattr(tr, "find_sub_execution", lambda base, ex, step: None)
    res = mod.N8nRunner(sleep=lambda s: None).run(_spec())
    assert res.status == "failed" and "boom" in res.error


def test_stop_stops_n8n_and_marks_run_cancelled(harness, monkeypatch):
    harness["start"]()
    from mailroom.runners import control
    running = execution("running", {"Run Drafting": [task([{}], sub="78")]})
    calls = {"stop": 0}
    def fetch(base, eid, full=False):
        control.request_stop()          # the user presses Stop while the run is going
        return running
    monkeypatch.setattr(tr, "fetch_execution", fetch)
    def stop(base, ex, eid):
        calls["stop"] += 1
        return [f"stop execution #{eid}: HTTP 200"]
    monkeypatch.setattr(tr, "stop_execution", stop)
    res = mod.N8nRunner(sleep=lambda s: None).run(_spec())
    assert res.status == "cancelled" and calls["stop"] == 1
    assert any("STOPPED by you" in line for line in res.log)
    assert control.current() == {} and not control.stop_requested()


def test_stop_execution_targets_parent_and_sub_workflows(monkeypatch):
    posts = []
    class R:
        def __init__(self, code, body=None): self.status_code, self._b, self.text = code, body or {}, ""
        def json(self): return self._b
    class C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, headers=None, json=None):
            posts.append((url, json)); return R(200, {"stopped": 1})
    monkeypatch.setattr(tr.httpx, "Client", lambda **kw: C())
    ex = execution("running", {})
    ex["workflowData"]["nodes"][2]["parameters"] = {"workflowId": {"value": "EV"}}
    lines = tr.stop_execution("B", ex, "77")
    assert posts[0][0] == "B/api/v1/executions/77/stop"
    subs = [p[1]["workflowId"] for p in posts[1:]]
    assert subs == ["D", "EV"] and posts[1][1]["startedAfter"] == ex["startedAt"]
    assert len(lines) == 3
