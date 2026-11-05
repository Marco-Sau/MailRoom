"""Run-tab look-back window: one value for replies, events and recap; standard when not chosen."""
from datetime import timedelta

from fastapi.testclient import TestClient

from mailroom.assistant import logic
from mailroom.models import RunSpec
from mailroom.runners import assistant_runner as ar

from tests.test_assistant_runner import NOW, FakeModel, world  # noqa: F401 - pytest fixture


def _windows(world, monkeypatch, hours):
    seen = []
    orig = ar.google_io.search

    def spy(q, after, limit, full=True):
        seen.append(round((NOW - after).total_seconds() / 3600))
        return orig(q, after, limit, full)
    monkeypatch.setattr(ar.google_io, "search", spy)
    spec = RunSpec(job_id="x", name="x", runner="assistant", provider="claude_cli",
                   steps=["drafting", "events", "recap"], hours=hours)
    res = ar.AssistantRunner(lambda n, m: FakeModel()).run(spec)
    return res, seen


def test_standard_windows(world, monkeypatch):
    res, seen = _windows(world, monkeypatch, None)
    assert seen[:3] == [48, 24, 24] and res.status == "succeeded"


def test_chosen_window_applies_to_all_steps(world, monkeypatch):
    res, seen = _windows(world, monkeypatch, 72)
    assert seen[:3] == [72, 72, 72]
    assert any("look-back window: the last 3 days" in line for line in res.log)


def test_span_labels():
    assert [logic.span(h) for h in (24, 48, 72, 168, 30)] == ["24 h", "48 h", "3 days", "7 days", "30 h"]


def test_run_api_accepts_hours_and_bounds(monkeypatch):
    from mailroom.api import routes_jobs as rj
    from mailroom.main import app
    got = []
    monkeypatch.setattr(rj, "_execute", lambda job_id, hours=None: got.append((job_id, hours)))
    c = TestClient(app)
    job = c.get("/api/jobs").json()[0]["job_id"]
    assert c.post(f"/api/jobs/{job}/run", json={"hours": 120}).json()["hours"] == 120
    assert c.post(f"/api/jobs/{job}/run", json={"hours": 500}).status_code == 422
    assert c.post(f"/api/jobs/{job}/run").json()["hours"] is None
    assert got == [(job, 120), (job, None)]


def test_n8n_webhook_body_carries_hours(monkeypatch):
    from mailroom.runners import n8n_runner as nr
    sent = {}

    class C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None, headers=None):
            sent.update(json)
            import httpx
            return httpx.Response(202, json={}, request=httpx.Request("POST", url))
    monkeypatch.setattr(nr.httpx, "Client", C)
    monkeypatch.setattr(nr.N8nRunner, "_headers", lambda self, spec, log: {})
    spec = RunSpec(job_id="n", name="n8n", runner="n8n", webhook_url="https://n8n/webhook/x", params={"mode": "daily"}, hours=72)
    nr.N8nRunner()._start(spec, [])
    assert sent == {"mode": "daily", "hours": 72}
