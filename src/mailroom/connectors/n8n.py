"""n8n connector.

Two separate concerns that are easy to confuse:

* **Reachability** - a TCP connection to the n8n host. This fails with
  ECONNREFUSED before any header is sent, so an API key cannot fix it.
* **Authorization** - the `X-N8N-API-KEY` header on the REST API. Webhooks do
  NOT use this key unless the workflow itself declares header auth.

`localhost` is deliberately probed as both `localhost` and `127.0.0.1`: on macOS
`localhost` resolves to `::1` first, and a Node process bound to IPv4 only will
refuse the IPv6 connection.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..rules import Rules
from ..secrets_store import store

TIMEOUT = httpx.Timeout(12.0, connect=4.0)
WEBHOOK_NODE_TYPES = {
    "n8n-nodes-base.webhook",
    "n8n-nodes-base.formTrigger",
    "@n8n/n8n-nodes-langchain.chatTrigger",
}


def short_type(node_type: str) -> str:
    """'n8n-nodes-base.gmailTrigger' -> 'gmailTrigger'."""
    return node_type.rsplit(".", 1)[-1]


def base_url(rules: Rules | None = None) -> str:
    try:
        from .. import app_settings
        chosen = app_settings.local_explicit("n8nBaseUrl")     # Settings → Connections wins
        if chosen:
            return str(chosen).rstrip("/")
    except Exception:  # noqa: BLE001
        pass
    rules = rules or Rules.load()
    cfg = rules.raw.get("n8n") or {}
    return str(cfg.get("base_url") or "http://127.0.0.1:5678").rstrip("/")


def candidates(url: str) -> list[str]:
    """The URL as given, plus the IPv4 literal when it names localhost."""
    out = [url]
    parts = urlsplit(url)
    if parts.hostname == "localhost":
        port = f":{parts.port}" if parts.port else ""
        out.append(urlunsplit((parts.scheme, f"127.0.0.1{port}", parts.path, "", "")))
    return out


def _headers() -> dict[str, str]:
    key = store.get("n8n_api_key")
    return {"X-N8N-API-KEY": key} if key else {}


def key_rejected(r: httpx.Response, what: str) -> PermissionError:
    """A 401/403 from the n8n REST API, explained: no key, invalid/expired key, or missing scope."""
    try:
        said = str((r.json() or {}).get("message") or "").strip()
    except ValueError:
        said = (r.text or "").strip()[:120]
    said = f": {said}" if said else ""
    if not store.get("n8n_api_key"):
        why = "no n8n API key is saved in MailRoom. Add it in Settings → Advanced → Secrets."
    elif r.status_code == 401:
        why = ("n8n does not accept the saved API key. It has expired (n8n API keys can carry an expiry "
               "date), or it was deleted or replaced in n8n. Create a new key in n8n → Settings → n8n API "
               "and paste it in MailRoom → Settings → Advanced → Secrets.")
    else:
        why = ("the API key is valid but not allowed to do this. Give the key the execution, workflow and "
               "data table scopes in n8n → Settings → n8n API, or create a new key with them.")
    return PermissionError(f"n8n rejected the API key while {what} (HTTP {r.status_code}{said}): {why}")


def reach(url: str) -> tuple[str, str]:
    """Return (working_base, detail). Raises if nothing answers."""
    errors = []
    for cand in candidates(url):
        try:
            with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as c:
                r = c.get(cand + "/healthz")
            return cand, f"{cand} → HTTP {r.status_code}"
        except httpx.ConnectError as exc:
            errors.append(f"{cand}: {exc}")
        except httpx.HTTPError as exc:
            errors.append(f"{cand}: {type(exc).__name__}: {exc}")
    raise ConnectionError(
        "n8n did not answer on " + " or ".join(candidates(url)) + ". "
        "Nothing is listening there — check whether n8n runs on a different "
        "port, in Docker with another mapping, or on n8n Cloud "
        "(https://<instance>.app.n8n.cloud). Details: " + " | ".join(errors)
    )


def _get(base: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as c:
        r = c.get(base + path, headers=_headers(), params=params)
    if r.status_code in (401, 403):
        raise key_rejected(r, f"reading {path}")
    r.raise_for_status()
    return r.json()


def list_workflows(rules: Rules | None = None, limit: int = 100) -> dict[str, Any]:
    """Every workflow, with the production webhook URL of each trigger found."""
    rules = rules or Rules.load()
    base, detail = reach(base_url(rules))

    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while len(items) < limit:
        params: dict[str, Any] = {"limit": min(100, limit - len(items))}
        if cursor:
            params["cursor"] = cursor
        page = _get(base, "/api/v1/workflows", params)
        items.extend(page.get("data", []))
        cursor = page.get("nextCursor")
        if not cursor:
            break

    out = []
    for wf in items:
        nodes = wf.get("nodes")
        if nodes is None:                       # some versions omit nodes in the list
            nodes = _get(base, f"/api/v1/workflows/{wf['id']}").get("nodes", [])
        hooks, triggers = [], []
        for n in nodes:
            ntype = n.get("type", "")
            params = n.get("parameters", {}) or {}
            if ntype in WEBHOOK_NODE_TYPES:
                path = params.get("path") or n.get("webhookId") or ""
                wired = bool(((wf.get("connections", {}) or {}).get(n.get("name"), {})
                              or {}).get("main"))
                hooks.append({
                    "node": n.get("name"),
                    "method": params.get("httpMethod", "GET"),
                    "path": path,
                    "production_url": f"{base}/webhook/{path}",
                    "test_url": f"{base}/webhook-test/{path}",
                    "auth": params.get("authentication", "none"),
                    "connected": wired,
                })
            if "trigger" in ntype.lower() or ntype.endswith(".start"):
                triggers.append(short_type(ntype))
        out.append({
            "id": wf.get("id"),
            "name": wf.get("name"),
            "active": bool(wf.get("active")),
            "webhooks": hooks,
            "triggers": sorted(set(triggers)) or ["(none found)"],
        })
    out.sort(key=lambda w: (not w["webhooks"], not w["active"], w["name"].lower()))
    return {
        "base_url": base,
        "detail": detail,
        "count": len(out),
        "with_webhook": sum(1 for w in out if w["webhooks"]),
        "workflows": out,
    }


# --------------------------------------------------------------- mutation
# Adding a trigger rewrites a live workflow, so this is split in two: plan()
# shows exactly what would change and apply() performs it, after writing the
# untouched original to disk. The public API has no PATCH, only a full PUT, so
# the whole object is round-tripped and only read-only fields are stripped.

import json as _json
import uuid as _uuid
from datetime import datetime as _dt

from ..settings import settings as _settings

WEBHOOK_NODE_NAME = "Inbox Pilot Trigger"
_PUT_FIELDS = ("name", "nodes", "connections", "settings", "nodeGroups")


def _find_workflow(base: str, ref: str) -> dict[str, Any]:
    """`ref` is an id or an exact-ish name (case-insensitive)."""
    try:
        return _get(base, f"/api/v1/workflows/{ref}")
    except httpx.HTTPStatusError:
        pass
    page = _get(base, "/api/v1/workflows", {"limit": 250})
    wanted = ref.strip().lower()
    matches = [w for w in page.get("data", []) if (w.get("name") or "").strip().lower() == wanted]
    if not matches:
        matches = [w for w in page.get("data", []) if wanted in (w.get("name") or "").lower()]
    if not matches:
        raise KeyError(f"no workflow matching {ref!r}")
    if len(matches) > 1:
        names = ", ".join(repr(m["name"]) for m in matches[:6])
        raise KeyError(f"{ref!r} matches several workflows: {names}. Use the id.")
    wf = matches[0]
    return wf if "nodes" in wf else _get(base, f"/api/v1/workflows/{wf['id']}")


def _downstream_of_triggers(wf: dict[str, Any]) -> list[str]:
    """Node names that the existing trigger(s) feed into — the webhook should
    feed the same first step so both entry points share the logic."""
    conns = wf.get("connections", {}) or {}
    targets: list[str] = []
    for node in wf.get("nodes", []):
        t = node.get("type", "")
        if "trigger" not in t.lower() and not t.endswith(".start"):
            continue
        for branch in (conns.get(node.get("name"), {}) or {}).get("main", []) or []:
            for link in branch or []:
                if link.get("node") and link["node"] not in targets:
                    targets.append(link["node"])
    return targets


def _webhook_nodes(wf: dict[str, Any]) -> list[dict[str, Any]]:
    return [n for n in wf.get("nodes", []) if n.get("type") == "n8n-nodes-base.webhook"]


def _existing_webhook(wf: dict[str, Any], path: str) -> dict[str, Any] | None:
    for n in _webhook_nodes(wf):
        if (n.get("parameters", {}) or {}).get("path") == path:
            return n
    return None


def _is_wired(wf: dict[str, Any], node_name: str) -> bool:
    return bool(((wf.get("connections", {}) or {}).get(node_name, {}) or {}).get("main"))


def plan_webhook_trigger(ref: str, path: str, rules: Rules | None = None) -> dict[str, Any]:
    base, _ = reach(base_url(rules or Rules.load()))
    wf = _find_workflow(base, ref)
    targets = _downstream_of_triggers(wf)
    triggers = [f"{n['name']} ({short_type(n.get('type',''))})"
                for n in wf.get("nodes", [])
                if "trigger" in n.get("type", "").lower() or n.get("type", "").endswith(".start")]

    hooks = _webhook_nodes(wf)
    exact = _existing_webhook(wf, path)
    adopt = exact or (hooks[0] if hooks else None)
    action, notes = "add", []
    if exact and _is_wired(wf, exact["name"]):
        action = "none"
    elif adopt:
        action = "adopt"
        cur = (adopt.get("parameters", {}) or {}).get("path")
        if cur != path:
            notes.append(f"path {cur!r} → {path!r}")
        if (adopt.get("parameters", {}) or {}).get("httpMethod", "GET") != "POST":
            notes.append("method → POST")
        if not _is_wired(wf, adopt["name"]):
            notes.append(f"connect → {', '.join(targets) or '(nothing to connect to)'}")

    return {
        "base_url": base,
        "workflow": {"id": wf.get("id"), "name": wf.get("name"),
                     "active": bool(wf.get("active")), "node_count": len(wf.get("nodes", []))},
        "existing_triggers": triggers,
        "existing_webhooks": [
            {"node": n["name"], "path": (n.get("parameters", {}) or {}).get("path"),
             "connected": _is_wired(wf, n["name"])} for n in hooks],
        "connect_to": targets,
        "action": action,
        "adopt_node": adopt["name"] if action == "adopt" else None,
        "changes": notes,
        "already_present": action == "none",
        "webhook_path": path,
        "production_url": f"{base}/webhook/{path}",
        "warning": None if targets else
                   "No trigger in this workflow connects to anything, so the webhook "
                   "would be left unconnected. Wire it up in n8n after.",
    }


def apply_webhook_trigger(ref: str, path: str, rules: Rules | None = None,
                          activate: bool = False) -> dict[str, Any]:
    rules = rules or Rules.load()
    base, _ = reach(base_url(rules))
    wf = _find_workflow(base, ref)
    wid = wf["id"]

    backup_dir = _settings.data_dir / "n8n-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"{wid}-{stamp}.json"
    backup.write_text(_json.dumps(wf, indent=2, ensure_ascii=False), encoding="utf-8")

    exact = _existing_webhook(wf, path)
    if exact and _is_wired(wf, exact["name"]):
        return {"changed": False,
                "reason": f"node {exact['name']!r} already serves path {path!r} and is connected",
                "backup": str(backup), "production_url": f"{base}/webhook/{path}"}

    targets = _downstream_of_triggers(wf)
    nodes = [dict(n) for n in wf.get("nodes", [])]
    hooks = [n for n in nodes if n.get("type") == "n8n-nodes-base.webhook"]
    adopted = None

    if hooks:
        # Reuse the webhook that is already there rather than leaving a duplicate
        # on the canvas. n8n creates one with a UUID path and no connections when
        # you drop the node in without configuring it.
        node = next((n for n in hooks
                     if (n.get("parameters", {}) or {}).get("path") == path), hooks[0])
        adopted = node["name"]
        node["parameters"] = {**(node.get("parameters") or {}),
                              "httpMethod": "POST", "path": path,
                              "responseMode": "onReceived"}
        name = node["name"]

    else:
        name = WEBHOOK_NODE_NAME
        taken = {n.get("name") for n in nodes}
        i = 2
        while name in taken:
            name, i = f"{WEBHOOK_NODE_NAME} {i}", i + 1
        xs = [n.get("position", [0, 0])[0] for n in nodes] or [0]
        ys = [n.get("position", [0, 0])[1] for n in nodes] or [0]
        node = {
            "parameters": {"httpMethod": "POST", "path": path,
                           "responseMode": "onReceived", "options": {}},
            "id": str(_uuid.uuid4()),
            "name": name,
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2.1,
            "position": [min(xs) - 220, max(ys) + 180],
            "webhookId": str(_uuid.uuid4()),
        }
        nodes.append(node)

    conns = dict(wf.get("connections", {}) or {})
    if targets:
        conns[name] = {"main": [[{"node": t, "type": "main", "index": 0} for t in targets]]}

    payload = {k: v for k, v in
               {"name": wf.get("name"), "nodes": nodes, "connections": conns,
                "settings": wf.get("settings") or {}}.items() if k in _PUT_FIELDS}

    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as c:
        r = c.put(f"{base}/api/v1/workflows/{wid}", headers=_headers(), json=payload)
    if r.status_code >= 400:
        raise RuntimeError(
            f"n8n rejected the update ({r.status_code}): {r.text[:400]}. "
            f"Nothing was changed; the original is saved at {backup}."
        )

    # An already-active workflow keeps serving the OLD webhook path until n8n
    # re-registers it, so bounce activation whenever we changed a live one.
    reregistered = None
    if wf.get("active"):
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as c:
            d = c.post(f"{base}/api/v1/workflows/{wid}/deactivate", headers=_headers())
            a = c.post(f"{base}/api/v1/workflows/{wid}/activate", headers=_headers())
        reregistered = (d.status_code < 400 and a.status_code < 400) or \
                       f"re-activation failed: {d.status_code}/{a.status_code} — " \
                       f"toggle the workflow off and on in n8n"
    activated = None
    if activate and not wf.get("active"):
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as c:
            a = c.post(f"{base}/api/v1/workflows/{wid}/activate", headers=_headers())
        activated = a.status_code < 400 or f"activate failed: {a.status_code} {a.text[:200]}"

    return {
        "changed": True,
        "workflow": {"id": wid, "name": wf.get("name")},
        "adopted_existing": adopted,
        "node_added": None if adopted else name,
        "reregistered": reregistered,
        "connected_to": targets,
        "backup": str(backup),
        "activated": activated,
        "production_url": f"{base}/webhook/{path}",
    }
