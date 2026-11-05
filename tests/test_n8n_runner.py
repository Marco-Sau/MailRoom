"""n8n answers 404 for two very different problems; the runner must tell them apart
from the response body. Bodies below are verbatim from the live instance."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from mailroom.models import RunSpec
from mailroom.runners import n8n_runner as mod

WRONG_METHOD = ('{"code":404,"message":"This webhook is not registered for POST requests. '
                'Did you mean to make a GET request?"}')
NOT_PUBLISHED = ('{"code":404,"message":"The requested webhook \\"POST gmail-assistant\\" '
                 'is not registered.","hint":"The workflow must be active..."}')


def _spec():
    return RunSpec(job_id="j", name="J", runner="n8n",
                   webhook_url="https://n8n.example/webhook/abc", auth="none")


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setattr(mod.db, "start_run", lambda *a, **k: 1)
    monkeypatch.setattr(mod.db, "finish_run", lambda *a, **k: None)
    monkeypatch.setattr(mod.db, "update_run_progress", lambda *a, **k: None)

    def go(status, body):
        resp = SimpleNamespace(status_code=status, text=body, content=body.encode(),
                               json=lambda: {})
        client = SimpleNamespace(__enter__=None)
        class C:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def post(self, *a, **k): return resp
        monkeypatch.setattr(mod.httpx, "Client", lambda **kw: C())
        return mod.N8nRunner().run(_spec())
    return go


def test_wrong_method_is_reported_as_a_method_problem(run):
    res = run(404, WRONG_METHOD)
    assert res.status == "failed" and "different HTTP method" in res.error


def test_unregistered_path_points_at_publish_and_path(run):
    res = run(404, NOT_PUBLISHED)
    assert res.status == "failed"
    assert "published" in res.error and "path" in res.error


def test_200_without_execution_id_succeeds_but_says_it_cannot_follow(run):
    res = run(200, '{"message":"Workflow was started"}')
    assert res.status == "succeeded"
    assert any("cannot follow" in line for line in res.log)
