"""Webhook key setup: credential upsert, node switch, verification (no network)."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from mailroom.connectors import n8n_webhook_key as wk

WF = {"id": "W", "name": "Gmail Assistant", "settings": {"timezone": "Europe/Rome"},
      "connections": {}, "nodes": [
          {"name": "Webhook", "type": "n8n-nodes-base.webhook",
           "parameters": {"httpMethod": "POST", "path": "abc", "responseMode": "responseNode"}},
          {"name": "Run Drafting", "type": "n8n-nodes-base.executeWorkflow", "parameters": {}}]}


@pytest.fixture
def env(monkeypatch, tmp_path):
    state = {"calls": [], "stored": {}, "wf": copy.deepcopy(WF), "probe": 403}
    monkeypatch.setattr(wk.settings.__class__, "data_dir", property(lambda self: tmp_path))
    monkeypatch.setattr(wk.n8n_conn, "reach", lambda url: ("https://n8n.example", ""))
    monkeypatch.setattr(wk.n8n_conn, "base_url", lambda rules=None: "https://n8n.example")
    monkeypatch.setattr(wk.n8n_conn, "_find_workflow", lambda base, ref: copy.deepcopy(state["wf"]))
    monkeypatch.setattr(wk.n8n_conn, "_get",
                        lambda base, path, params=None: {**state["wf"], "activeVersion": {"nodes": state["wf"]["nodes"]}})
    monkeypatch.setattr(wk.store, "set", lambda k, v: state["stored"].__setitem__(k, v))

    def call(method, url, body=None, params=None):
        state["calls"].append((method, url, body, params))
        if method == "POST" and url.endswith("/credentials"):
            return SimpleNamespace(status_code=200, text="", json=lambda: {"id": "C1", "name": body["name"]})
        if method == "PATCH":
            return SimpleNamespace(status_code=200, text="", json=lambda: {})
        if method == "PUT":
            state["wf"]["nodes"] = body["nodes"]
            return SimpleNamespace(status_code=200, text="", json=lambda: {})
        raise AssertionError((method, url))
    monkeypatch.setattr(wk, "_call", call)

    class C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, headers=None, json=None):
            state["probe_headers"] = headers
            return SimpleNamespace(status_code=state["probe"])
    monkeypatch.setattr(wk.httpx, "Client", lambda **kw: C())
    return state


def test_first_setup_creates_credential_protects_node_and_verifies(env):
    out = wk.secure_webhook("W", "abc")
    methods = [c[0] for c in env["calls"]]
    assert methods == ["POST", "PUT"]
    cred_body = env["calls"][0][2]
    assert cred_body["type"] == "httpHeaderAuth" and cred_body["data"]["name"] == "X-Inbox-Pilot-Key"
    key = env["stored"]["n8n_webhook_key"]
    assert cred_body["data"]["value"] == key and len(key) >= 40
    node = env["calls"][1][2]["nodes"][0]
    assert node["parameters"]["authentication"] == "headerAuth"
    assert node["credentials"] == {"httpHeaderAuth": {"id": "C1", "name": "Inbox Pilot webhook key"}}
    assert env["calls"][1][3] == {"publishIfActive": "true"}
    assert env["probe_headers"] == {"X-Inbox-Pilot-Key": "wrong-key"}
    assert out["ok"] and out["published_requires_key"] and out["wrong_key_probe_status"] == 403
    assert key not in str(out)                       # the key is never returned


def test_rotation_patches_same_credential_without_touching_workflow(env):
    wk.secure_webhook("W", "abc")
    first = env["stored"]["n8n_webhook_key"]
    env["calls"].clear()
    out = wk.secure_webhook("W", "abc")
    assert [c[0] for c in env["calls"]] == ["PATCH"]
    assert env["calls"][0][1].endswith("/credentials/C1")
    assert env["stored"]["n8n_webhook_key"] != first
    assert out["ok"] and not out["workflow_changed"]


def test_unprotected_probe_is_not_ok(env):
    env["probe"] = 202
    assert wk.secure_webhook("W", "abc")["ok"] is False


def test_unknown_path_is_refused_before_any_change(env):
    with pytest.raises(KeyError):
        wk.secure_webhook("W", "nope")
    assert env["calls"] == []


def test_unpublished_editor_changes_block_first_setup(env):
    env["wf"]["active"] = True
    env["wf"]["versionId"] = "draft-2"
    env["wf"]["activeVersion"] = {"versionId": "live-1"}
    with pytest.raises(wk.UnpublishedChanges):
        wk.secure_webhook("W", "abc")
    assert env["calls"] == [] and "n8n_webhook_key" not in env["stored"]
    assert wk.secure_webhook("W", "abc", allow_unpublished=True)["workflow_changed"]
