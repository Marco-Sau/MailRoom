"""Review: a window onto the Gmail drafts MailRoom (app engines and n8n) created.

Nothing is held back: drafts are already in Gmail → Drafts, never sent. Here you
read them next to the email they answer, edit them, open them in Gmail to send,
or discard them.

Which drafts are MailRoom's (works for n8n too, without changing its workflows):
* decision notes  - subject starts with "[Inbox Pilot]" (BOOK? / FREE / CONFLICT / CHECK);
* reply drafts    - drafts in a thread the assistant processed (Processed Sender Emails),
                    excluding threads where it decided no reply was needed.
A draft you wrote yourself in such a thread is listed too (Gmail does not record who
created a draft).
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .assistant import google_io, logic
from .connectors import n8n_tables as t

NOTE_PREFIX = "[Inbox Pilot]"
_LINK = re.compile(r"(https://\S+[?&]a=(book|decline|add|ignore|keep|swap)\b\S*)")
ACTION_LABEL = {"book": "BOOK it", "decline": "Do NOT book", "add": "Add to calendar", "ignore": "Ignore",
                "keep": "Keep my schedule", "swap": "Swap to the event"}


def note_kind(subject: str) -> str | None:
    if not subject.startswith(NOTE_PREFIX):
        return None
    rest = subject[len(NOTE_PREFIX):].strip()
    if rest.startswith("BOOK?"):
        return "booking"
    for st in ("FREE", "CONFLICT", "CHECK"):
        if rest.startswith(st):
            return "event-" + st.lower()
    return "note"


def actions(body: str) -> list[dict[str, str]]:
    seen, out = set(), []
    for url, act in _LINK.findall(body or ""):
        if act not in seen:
            seen.add(act)
            out.append({"action": act, "label": ACTION_LABEL[act], "url": url})
    return out


def _processed() -> dict[str, dict[str, Any]]:
    rows = t.rows("processed", t._base())
    by_thread: dict[str, dict[str, Any]] = {}
    for r in rows:
        tid = r.get("threadId")
        if not tid:
            continue
        prev = by_thread.get(tid)
        if not prev or str(r.get("processedAt") or "") >= str(prev.get("processedAt") or ""):
            by_thread[tid] = r
    return by_thread


def _item(d: dict[str, Any], processed: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    msg = d.get("message") or {}
    h = google_io.header_map(msg)
    subject = h.get("subject", "")
    tid = msg.get("threadId") or ""
    kind = note_kind(subject)
    row = processed.get(tid)
    if not kind:
        if not row or row.get("proposalStatus") == "no_reply_needed":
            return None
        kind = "reply"
    body = google_io.message_text(msg)
    item = {"draftId": d["id"], "messageId": msg.get("id"), "threadId": tid, "kind": kind,
            "to": h.get("to", ""), "subject": subject, "body": body,
            "updated": msg.get("internalDate", "0"),
            "gmailUrl": f"https://mail.google.com/mail/u/0/#all/{tid}" if tid else "https://mail.google.com/mail/u/0/#drafts",
            "actions": actions(body) if kind != "reply" else [], "status": "", "slots": [], "original": None}
    if row:
        item["status"] = row.get("proposalStatus") or ""
        item["slots"] = [s.get("label") for s in logic.pj(row.get("proposedSlotsJson") or "[]", []) if isinstance(s, dict)]
    return item


_cache: dict[str, Any] = {"at": 0.0, "data": None}
CACHE_SECONDS = 30


def invalidate() -> None:
    _cache["at"], _cache["data"] = 0.0, None


def _decision_threads() -> set[str]:
    try:
        return {str(r["threadId"]) for r in t.rows("decisions", t._base()) if r.get("threadId")}
    except Exception:  # noqa: BLE001 - notes are still found by subject
        return set()


def list_review(fresh: bool = False) -> dict[str, Any]:
    import time
    if not fresh and _cache["data"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
    drafts = google_io.list_drafts()
    processed = _processed()
    ours = set(processed) | _decision_threads()

    def fetch(d: dict[str, Any]) -> dict[str, Any] | None:
        if (d.get("message") or {}).get("threadId") in ours:
            return google_io.get_draft(d["id"])
        meta = google_io.get_draft(d["id"], "metadata")          # cheap check for a stray note
        subj = google_io.header_map(meta.get("message") or {}).get("subject", "")
        return google_io.get_draft(d["id"]) if subj.startswith(NOTE_PREFIX) else None

    with ThreadPoolExecutor(max_workers=6) as pool:
        full = [d for d in pool.map(fetch, drafts) if d]
    items = [i for i in (_item(d, processed) for d in full) if i]
    threads = {i["threadId"] for i in items if i["threadId"]}
    with ThreadPoolExecutor(max_workers=6) as pool:
        originals = dict(zip(threads, pool.map(_safe_original, threads)))
    for i in items:
        i["original"] = originals.get(i["threadId"])
    items.sort(key=lambda i: int(i["updated"] or 0), reverse=True)
    data = {"items": items, "counts": {"replies": sum(1 for i in items if i["kind"] == "reply"),
                                       "notes": sum(1 for i in items if i["kind"] != "reply")}}
    _cache["at"], _cache["data"] = time.monotonic(), data
    return data


def _safe_original(tid: str) -> dict[str, Any] | None:
    try:
        return google_io.thread_original(tid)
    except Exception:  # noqa: BLE001 - context is optional
        return None
