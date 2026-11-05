"""Shared-secret protection for the webhook that starts a workflow.

The key is generated here, stored only in the macOS Keychain and in an n8n
"Header Auth" credential, and never shown, logged or returned by the API.
n8n then rejects any POST that does not carry `X-Inbox-Pilot-Key: <key>`.

Rotation re-uses the same n8n credential (PATCH), so the workflow does not
need to change after the first setup.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Any

import httpx

from ..rules import Rules
from ..secrets_store import store
from ..settings import settings
from . import n8n as n8n_conn

HEADER_NAME = "X-Inbox-Pilot-Key"
CREDENTIAL_NAME = "Inbox Pilot webhook key"
CREDENTIAL_TYPE = "httpHeaderAuth"
SECRET_KEY = "n8n_webhook_key"


def _state_path():
    return settings.data_dir / "n8n-webhook-key.json"


def load_state() -> dict[str, Any]:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    _state_path().parent.mkdir(parents=True, exist_ok=True)
    _state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")


def status() -> dict[str, Any]:
    st = load_state()
    return {"key_stored": bool(store.get(SECRET_KEY)), "header": HEADER_NAME,
            "credential_id": st.get("credentialId"), "workflow_id": st.get("workflowId"),
            "path": st.get("path"), "rotated_at": st.get("rotatedAt")}


def _call(method: str, url: str, body: dict[str, Any] | None = None,
          params: dict[str, str] | None = None) -> httpx.Response:
    with httpx.Client(timeout=n8n_conn.TIMEOUT, follow_redirects=True) as c:
        return c.request(method, url, headers=n8n_conn._headers(), json=body, params=params)


def _fail(what: str, r: httpx.Response) -> RuntimeError:
    return RuntimeError(f"{what} failed: n8n answered {r.status_code} {r.text[:300]}")


def _upsert_credential(base: str, cred_id: str | None, key: str) -> tuple[str, str]:
    data = {"name": HEADER_NAME, "value": key}
    if cred_id:
        r = _call("PATCH", f"{base}/api/v1/credentials/{cred_id}",
                  {"name": CREDENTIAL_NAME, "type": CREDENTIAL_TYPE, "data": data})
        if r.status_code < 400:
            return cred_id, CREDENTIAL_NAME
        if r.status_code != 404:
            raise _fail("Updating the n8n credential", r)
    r = _call("POST", f"{base}/api/v1/credentials",
              {"name": CREDENTIAL_NAME, "type": CREDENTIAL_TYPE, "data": data})
    if r.status_code >= 400:
        raise _fail("Creating the n8n credential", r)
    body = r.json()
    return str(body["id"]), str(body.get("name") or CREDENTIAL_NAME)


def _node_is_protected(node: dict[str, Any], cred_id: str) -> bool:
    p = node.get("parameters", {}) or {}
    c = (node.get("credentials", {}) or {}).get(CREDENTIAL_TYPE, {}) or {}
    return p.get("authentication") == "headerAuth" and str(c.get("id")) == cred_id


class UnpublishedChanges(RuntimeError):
    """The workflow has edits saved in the editor that are not live yet."""


def secure_webhook(workflow_ref: str, path: str, rules: Rules | None = None,
                   allow_unpublished: bool = False) -> dict[str, Any]:
    """Create (or rotate) the key and require it on the webhook at `path`.

    Switching the webhook to header auth saves and re-publishes the workflow,
    which would also put live any edit that was saved in the n8n editor but
    never published. Unless `allow_unpublished`, that case is refused first.
    """
    rules = rules or Rules.load()
    base, _ = n8n_conn.reach(n8n_conn.base_url(rules))
    wf = n8n_conn._find_workflow(base, workflow_ref)
    wid = str(wf["id"])
    node = n8n_conn._existing_webhook(wf, path)
    if node is None:
        raise KeyError(f"workflow {wf.get('name')!r} has no webhook with path {path!r}")
    active_vid = (wf.get("activeVersion") or {}).get("versionId") or wf.get("activeVersionId")
    already = (node.get("parameters", {}) or {}).get("authentication") == "headerAuth"
    if (not allow_unpublished and not already and wf.get("active") and active_vid and wf.get("versionId")
            and active_vid != wf.get("versionId")):
        raise UnpublishedChanges(
            f"{wf.get('name')!r} has changes saved in the n8n editor that are not published. "
            f"Setting the key re-publishes the workflow and would put those changes live too. "
            f"Publish or discard them in n8n first, or confirm to publish them along with the key.")

    backup_dir = settings.data_dir / "n8n-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{wid}-{datetime.now():%Y%m%d-%H%M%S}-before-key.json"
    backup.write_text(json.dumps(wf, indent=2, ensure_ascii=False), encoding="utf-8")

    current = ((node.get("credentials", {}) or {}).get(CREDENTIAL_TYPE, {}) or {}).get("id")
    cred_id = str(current or load_state().get("credentialId") or "") or None
    key = secrets.token_urlsafe(32)                       # 256 bits
    cred_id, cred_name = _upsert_credential(base, cred_id, key)
    # n8n now holds the new key; keep the Keychain in step before anything else.
    store.set(SECRET_KEY, key)
    _save_state({"credentialId": cred_id, "workflowId": wid, "path": path,
                 "rotatedAt": datetime.now().isoformat(timespec="seconds")})

    changed = False
    if not _node_is_protected(node, cred_id):
        node["parameters"] = {**(node.get("parameters") or {}), "authentication": "headerAuth"}
        node["credentials"] = {**(node.get("credentials") or {}),
                               CREDENTIAL_TYPE: {"id": cred_id, "name": cred_name}}
        payload = {k: wf[k] for k in n8n_conn._PUT_FIELDS if k in wf}
        payload["settings"] = wf.get("settings") or {}
        r = _call("PUT", f"{base}/api/v1/workflows/{wid}", payload,
                  params={"publishIfActive": "true"})
        if r.status_code >= 400:
            raise _fail(f"Saving the workflow (original kept at {backup})", r)
        changed = True

    # Verify from n8n's side that the PUBLISHED version requires the key.
    live = n8n_conn._get(base, f"/api/v1/workflows/{wid}")
    active_nodes = ((live.get("activeVersion") or {}).get("nodes")) or live.get("nodes", [])
    live_node = next((n for n in active_nodes
                      if (n.get("parameters", {}) or {}).get("path") == path), None)
    published = bool(live_node and _node_is_protected(live_node, cred_id))

    # Then from the outside: a request with a wrong key must be refused. This
    # never starts a run when protection is in place.
    probe = None
    if published:
        with httpx.Client(timeout=n8n_conn.TIMEOUT, follow_redirects=True) as c:
            pr = c.post(f"{base}/webhook/{path}", headers={HEADER_NAME: "wrong-key"}, json={})
        probe = pr.status_code
    return {
        "ok": published and probe in (401, 403),
        "workflow": {"id": wid, "name": wf.get("name")},
        "credential_id": cred_id,
        "workflow_changed": changed,
        "published_requires_key": published,
        "wrong_key_probe_status": probe,
        "header": HEADER_NAME,
        "backup": str(backup),
    }


def find_workflow_by_path(path: str, rules: Rules | None = None) -> str:
    """Id of the workflow whose Webhook node serves `path`."""
    listing = n8n_conn.list_workflows(rules)
    hits = [w for w in listing["workflows"]
            if any(h.get("path") == path for h in w.get("webhooks", []))]
    if not hits:
        raise KeyError(f"no n8n workflow has a webhook with path {path!r}")
    if len(hits) > 1:
        raise KeyError(f"several workflows use webhook path {path!r}: "
                       + ", ".join(w["name"] for w in hits))
    return str(hits[0]["id"])
