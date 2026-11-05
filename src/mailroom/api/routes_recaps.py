"""Recaps tab: every recap, from n8n and from the app engines, readable in MailRoom.

The shared store is the n8n data table "Inbox Pilot Recaps" (written by the n8n
Recap workflow and by the app engines). A run that could not reach n8n keeps a
local copy in SQLite; both are listed together, newest first.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException

from .. import db

router = APIRouter(prefix="/api/recaps", tags=["recaps"])
LABEL = {"n8n": "n8n", "claude_cli": "Claude", "codex_cli": "ChatGPT", "gemini_cli": "Gemini", "ollama": "Ollama",
         "anthropic": "Claude API", "openai": "OpenAI API", "gemini": "Gemini API"}


def _items(raw: Any) -> list[dict[str, Any]]:
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


def _run(raw: Any) -> dict[str, Any]:
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _summary(key: str, source: str, title: str, when: str, url: str, count: Any, items: list,
             run: dict[str, Any] | None = None) -> dict[str, Any]:
    levels = {str(p): 0 for p in range(1, 6)}
    for it in items:
        try:
            levels[str(min(5, max(1, int(it.get("priority") or 1))))] += 1
        except (TypeError, ValueError):
            levels["1"] += 1
    return {"key": key, "source": source, "engine": LABEL.get(source, source), "title": title,
            "generatedAt": when, "docUrl": url or "", "emailCount": int(count or len(items) or 0),
            "levels": levels, "run": run or {},
            "drafted": sum(1 for i in items if i.get("replyStatus") not in (None, "", "Not tracked", "No reply needed"))}


def _n8n_rows() -> tuple[list[dict[str, Any]], str | None]:
    from ..connectors import n8n_tables as t
    try:
        return t.rows("recaps", t._base()), None
    except Exception as exc:  # noqa: BLE001 - n8n down: still show local recaps
        return [], f"{type(exc).__name__}: {exc}"[:200]


@router.get("")
def list_recaps():
    out, rows_n8n = [], []
    rows_n8n, err = _n8n_rows()
    for r in rows_n8n:
        out.append(_summary(f"n8n-{r['id']}", r.get("source") or "n8n", r.get("title") or "Email recap",
                            str(r.get("generatedAt") or r.get("createdAt") or ""), r.get("docUrl"),
                            r.get("emailCount"), _items(r.get("itemsJson")), _run(r.get("runJson"))))
    for r in db.list_recaps():
        out.append(_summary(f"local-{r['id']}", r["source"], r["title"], r["generated_at"], r.get("doc_url"),
                            r["email_count"], _items(r["items_json"]), _run(r.get("run_json"))))
    out.sort(key=lambda x: x["generatedAt"], reverse=True)
    return {"recaps": out, "error": err}


@router.get("/{key}")
def get_recap(key: str):
    kind, _, rid = key.partition("-")
    if not rid.isdigit():
        raise HTTPException(404, "no such recap")
    if kind == "local":
        r = db.get_recap(int(rid))
        if not r:
            raise HTTPException(404, "no such recap")
        items = _items(r["items_json"])
        head = _summary(key, r["source"], r["title"], r["generated_at"], r.get("doc_url"), r["email_count"], items,
                        _run(r.get("run_json")))
    elif kind == "n8n":
        rows, err = _n8n_rows()
        r = next((x for x in rows if int(x.get("id", -1)) == int(rid)), None)
        if not r:
            raise HTTPException(502 if err else 404, err or "no such recap")
        items = _items(r.get("itemsJson"))
        head = _summary(key, r.get("source") or "n8n", r.get("title") or "Email recap",
                        str(r.get("generatedAt") or r.get("createdAt") or ""), r.get("docUrl"),
                        r.get("emailCount"), items, _run(r.get("runJson")))
    else:
        raise HTTPException(404, "no such recap")
    items.sort(key=lambda i: -int(i.get("priority") or 1))
    return {**head, "items": items}
