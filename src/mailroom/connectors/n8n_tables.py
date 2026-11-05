"""n8n data tables through the public API (`/api/v1/data-tables`).

The drafting workflow reads its configuration from two tables, so the app edits
those tables rather than the workflow:

* **Tracked Senders** - one row per sender: matchPatterns (comma-separated
  addresses or @domains), senderName (who they are), notes (reply
  instructions), active.
* **Inbox Pilot Settings** - key/value rows: draftScope ("senders" | "inbox"),
  signatureName, defaultNotes, maxEmails.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from ..rules import Rules
from . import n8n as n8n_conn

TABLE_NAMES = {"senders": "Tracked Senders", "settings": "Inbox Pilot Settings",
               "processed": "Processed Sender Emails", "decisions": "Inbox Pilot Decisions",
               "recaps": "Inbox Pilot Recaps"}
_ids: dict[str, str] = {}


def _base(rules: Rules | None = None) -> str:
    base, _ = n8n_conn.reach(n8n_conn.base_url(rules or Rules.load()))
    return base


def _req(method: str, url: str, **kw: Any) -> Any:
    with httpx.Client(timeout=n8n_conn.TIMEOUT, follow_redirects=True) as c:
        r = c.request(method, url, headers=n8n_conn._headers(), **kw)
    if r.status_code in (401, 403):
        raise n8n_conn.key_rejected(r, f"using the data tables ({method} {url.split('/api/', 1)[-1]})")
    if r.status_code >= 400:
        raise RuntimeError(f"n8n answered {r.status_code} on {method} {url}: {r.text[:300]}")
    return r.json() if r.content else None


def table_id(kind: str, base: str) -> str:
    """Resolve a table by name (overridable in rules.yaml: n8n.tables.<kind>)."""
    if kind in _ids:
        return _ids[kind]
    override = ((Rules.load().raw.get("n8n") or {}).get("tables") or {}).get(kind)
    if override:
        _ids[kind] = str(override)
        return _ids[kind]
    name = TABLE_NAMES[kind]
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 250}
        if cursor:
            params["cursor"] = cursor
        page = _req("GET", f"{base}/api/v1/data-tables", params=params) or {}
        for t in page.get("data", []):
            if (t.get("name") or "").strip().lower() == name.lower():
                _ids[kind] = str(t["id"])
                return _ids[kind]
        cursor = page.get("nextCursor")
        if not cursor:
            raise KeyError(f"n8n has no data table named {name!r}")


def rows(kind: str, base: str) -> list[dict[str, Any]]:
    tid, out, cursor = table_id(kind, base), [], None
    while True:
        params: dict[str, Any] = {"limit": 250, "sortBy": "id:asc"}
        if cursor:
            params["cursor"] = cursor
        page = _req("GET", f"{base}/api/v1/data-tables/{tid}/rows", params=params) or {}
        out.extend(page.get("data", []))
        cursor = page.get("nextCursor")
        if not cursor:
            return out


def _by_id(row_id: int) -> dict[str, Any]:
    return {"type": "and", "filters": [{"columnName": "id", "condition": "eq", "value": int(row_id)}]}


def insert_many(kind: str, base: str, data: list[dict[str, Any]]) -> Any:
    if not data:
        return None
    return _req("POST", f"{base}/api/v1/data-tables/{table_id(kind, base)}/rows",
                json={"data": data, "returnType": "id"})


def insert(kind: str, base: str, data: dict[str, Any]) -> Any:
    return _req("POST", f"{base}/api/v1/data-tables/{table_id(kind, base)}/rows",
                json={"data": [data], "returnType": "all"})


def update(kind: str, base: str, row_id: int, data: dict[str, Any]) -> Any:
    return _req("PATCH", f"{base}/api/v1/data-tables/{table_id(kind, base)}/rows/update",
                json={"filter": _by_id(row_id), "data": data, "returnData": True})


def delete(kind: str, base: str, row_id: int) -> Any:
    return _req("DELETE", f"{base}/api/v1/data-tables/{table_id(kind, base)}/rows/delete",
                params={"filter": json.dumps(_by_id(row_id)), "returnData": "true"})
