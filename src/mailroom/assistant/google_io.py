"""Gmail, Calendar and Docs calls used by the assistant, shaped like n8n's items
so the ported logic reads the same fields (from.text, from.value, headers ...)."""
from __future__ import annotations

import base64
import email.utils
import re
from datetime import datetime
from email.message import EmailMessage
from typing import Any

from ..connectors import google_auth

DOCS_SCOPES = ("https://www.googleapis.com/auth/documents",
               "https://www.googleapis.com/auth/drive.file")


def _svc(api: str, version: str):
    return google_auth.service(api, version)


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")


def _parts(payload: dict[str, Any], mime: str) -> str:
    if payload.get("mimeType") == mime and (payload.get("body") or {}).get("data"):
        return _b64(payload["body"]["data"])
    for p in payload.get("parts", []) or []:
        found = _parts(p, mime)
        if found:
            return found
    return ""


def _attachments(payload: dict[str, Any]) -> list[dict[str, str]]:
    out = []
    if payload.get("filename"):
        out.append({"filename": payload["filename"], "mimeType": payload.get("mimeType", "")})
    for p in payload.get("parts", []) or []:
        out += _attachments(p)
    return out


def to_item(msg: dict[str, Any]) -> dict[str, Any]:
    payload = msg.get("payload", {}) or {}
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    name, addr = email.utils.parseaddr(headers.get("from", ""))
    return {
        "id": msg["id"], "threadId": msg.get("threadId"),
        "subject": headers.get("subject", ""),
        "from": {"text": headers.get("from", ""), "value": [{"address": addr.lower(), "name": name}]},
        "to": headers.get("to", ""), "date": headers.get("date", ""),
        "internalDate": msg.get("internalDate", "0"),
        "messageId": headers.get("message-id", ""),
        "headers": {k: f"{k}: {v}" for k, v in headers.items()},
        "text": _parts(payload, "text/plain"), "html": _parts(payload, "text/html"),
        "snippet": msg.get("snippet", ""), "labelIds": msg.get("labelIds", []),
        "attachments": _attachments(payload),
    }


def search(query: str, after: datetime, limit: int, full: bool = True) -> list[dict[str, Any]]:
    """Messages matching `query` received after `after` (like n8n's receivedAfter)."""
    svc = _svc("gmail", "v1")
    q = f"{query} after:{int(after.timestamp())}".strip()
    ids, token = [], None
    while len(ids) < limit:
        resp = svc.users().messages().list(userId="me", q=q, maxResults=min(100, limit - len(ids)),
                                           pageToken=token).execute()
        ids += [m["id"] for m in resp.get("messages", []) or []]
        token = resp.get("nextPageToken")
        if not token:
            break
    fmt = "full" if full else "metadata"
    out = []
    for mid in ids[:limit]:
        kw: dict[str, Any] = {"userId": "me", "id": mid, "format": fmt}
        if not full:
            kw["metadataHeaders"] = ["From", "Subject", "Date", "To"]
        out.append(to_item(svc.users().messages().get(**kw).execute()))
    return out


def create_draft(*, subject: str, body: str, to: str = "", thread_id: str | None = None,
                 in_reply_to: str | None = None) -> str:
    """A Gmail draft. With no `to`, it has no recipient and cannot be sent by accident."""
    mime = EmailMessage()
    if to:
        mime["To"] = to
    mime["Subject"] = subject
    if in_reply_to:
        mime["In-Reply-To"] = in_reply_to
        mime["References"] = in_reply_to
    mime.set_content(body)
    msg: dict[str, Any] = {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode()}
    if thread_id:
        msg["threadId"] = thread_id
    return _svc("gmail", "v1").users().drafts().create(userId="me", body={"message": msg}).execute()["id"]


def calendar_events(calendar_id: str, time_min: datetime, time_max: datetime) -> list[dict[str, Any]]:
    svc = _svc("calendar", "v3")
    out, token = [], None
    while True:
        resp = svc.events().list(calendarId=calendar_id, timeMin=time_min.isoformat(),
                                 timeMax=time_max.isoformat(), singleEvents=True,
                                 orderBy="startTime", maxResults=2500, pageToken=token).execute()
        out += resp.get("items", []) or []
        token = resp.get("nextPageToken")
        if not token:
            return out


# ------------------------------------------------------------- drafts (Review)
def list_drafts(limit: int = 200) -> list[dict[str, Any]]:
    """[{id, message: {id, threadId}}] for every Gmail draft."""
    svc = _svc("gmail", "v1")
    out, token = [], None
    while len(out) < limit:
        resp = svc.users().drafts().list(userId="me", maxResults=min(100, limit - len(out)), pageToken=token).execute()
        out += resp.get("drafts", []) or []
        token = resp.get("nextPageToken")
        if not token:
            return out
    return out


def get_draft(draft_id: str, fmt: str = "full") -> dict[str, Any]:
    kw: dict[str, Any] = {"userId": "me", "id": draft_id, "format": fmt}
    return _svc("gmail", "v1").users().drafts().get(**kw).execute()


def header_map(msg: dict[str, Any]) -> dict[str, str]:
    return {h["name"].lower(): h.get("value", "") for h in (msg.get("payload") or {}).get("headers", []) or []}


def message_text(msg: dict[str, Any]) -> str:
    payload = msg.get("payload") or {}
    text = _parts(payload, "text/plain")
    if not text:
        html = _parts(payload, "text/html")
        html = re.sub(r"<br\s*/?>|</p>|</div>", "\n", html, flags=re.I)
        text = re.sub(r"<[^>]+>", "", html)
        text = re.sub(r"&nbsp;", " ", text).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return text.replace("\r\n", "\n").strip()


def update_draft(draft_id: str, *, subject: str, body: str) -> dict[str, Any]:
    """Replace subject and body, keeping recipients, thread and reply headers."""
    cur = get_draft(draft_id, "metadata")
    msg = cur.get("message") or {}
    h = header_map(msg)
    mime = EmailMessage()
    for name in ("To", "Cc", "Bcc", "In-Reply-To", "References"):
        if h.get(name.lower()):
            mime[name] = h[name.lower()]
    mime["Subject"] = subject
    mime.set_content(body)
    new: dict[str, Any] = {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode()}
    if msg.get("threadId"):
        new["threadId"] = msg["threadId"]
    return _svc("gmail", "v1").users().drafts().update(
        userId="me", id=draft_id, body={"id": draft_id, "message": new}).execute()


def delete_draft(draft_id: str) -> None:
    """Deletes the draft only (a draft was never sent; your received mail is untouched)."""
    _svc("gmail", "v1").users().drafts().delete(userId="me", id=draft_id).execute()


def thread_original(thread_id: str) -> dict[str, Any] | None:
    """The newest non-draft message of a thread: what the draft answers."""
    t = _svc("gmail", "v1").users().threads().get(
        userId="me", id=thread_id, format="metadata", metadataHeaders=["From", "Subject", "Date", "To"]).execute()
    msgs = [m for m in t.get("messages", []) or [] if "DRAFT" not in (m.get("labelIds") or [])
            and "SENT" not in (m.get("labelIds") or [])]
    if not msgs:
        return None
    m = msgs[-1]
    h = header_map(m)
    return {"id": m["id"], "from": h.get("from", ""), "subject": h.get("subject", ""), "date": h.get("date", ""),
            "snippet": m.get("snippet", ""), "internalDate": m.get("internalDate", "0")}


def draft_summaries() -> list[dict[str, str]]:
    """Every Gmail draft as {threadId, subject} (for the draft follow-up)."""
    from concurrent.futures import ThreadPoolExecutor
    svc_drafts = list_drafts(500)

    def one(d: dict[str, Any]) -> dict[str, str]:
        m = _svc("gmail", "v1").users().drafts().get(userId="me", id=d["id"], format="metadata").execute().get("message") or {}
        return {"threadId": m.get("threadId", ""), "subject": header_map(m).get("subject", "")}
    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(one, svc_drafts))


def sent_messages(after: datetime) -> list[dict[str, Any]]:
    """Messages you sent after `after`, as {threadId, internalDate}."""
    from concurrent.futures import ThreadPoolExecutor
    svc = _svc("gmail", "v1")
    q = f"in:sent after:{int(after.timestamp())}"
    ids, token = [], None
    while True:
        resp = svc.users().messages().list(userId="me", q=q, maxResults=500, pageToken=token).execute()
        ids += resp.get("messages", []) or []
        token = resp.get("nextPageToken")
        if not token or len(ids) >= 2000:
            break

    def one(m: dict[str, Any]) -> dict[str, Any]:
        g = _svc("gmail", "v1").users().messages().get(userId="me", id=m["id"], format="minimal").execute()
        return {"threadId": g.get("threadId", m.get("threadId")), "internalDate": g.get("internalDate", "0")}
    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(one, ids))


def profile_email() -> str:
    return _svc("gmail", "v1").users().getProfile(userId="me").execute().get("emailAddress", "")


def send_as_addresses() -> list[str]:
    """Gmail 'Send mail as' addresses (aliases and connected accounts)."""
    resp = _svc("gmail", "v1").users().settings().sendAs().list(userId="me").execute()
    return [s.get("sendAsEmail", "") for s in resp.get("sendAs", []) or [] if s.get("sendAsEmail")]


def recipient_headers(query: str, after: datetime, limit: int) -> list[dict[str, list[str]]]:
    """For each matching message: every value of the To/Cc/Delivered-To/X-Forwarded-*/X-Original-To/List-Id headers."""
    svc = _svc("gmail", "v1")
    q = f"{query} after:{int(after.timestamp())}".strip()
    ids, token = [], None
    while len(ids) < limit:
        resp = svc.users().messages().list(userId="me", q=q, maxResults=min(500, limit - len(ids)),
                                           pageToken=token).execute()
        ids += [m["id"] for m in resp.get("messages", []) or []]
        token = resp.get("nextPageToken")
        if not token:
            break
    wanted = ["To", "Cc", "Delivered-To", "X-Forwarded-To", "X-Forwarded-For", "X-Original-To", "List-Id"]
    out = []
    for mid in ids[:limit]:
        msg = svc.users().messages().get(userId="me", id=mid, format="metadata", metadataHeaders=wanted).execute()
        h: dict[str, list[str]] = {}
        for x in (msg.get("payload") or {}).get("headers", []) or []:
            h.setdefault(x["name"].lower(), []).append(x.get("value", ""))
        out.append(h)
    return out


def has_scopes(*scopes: str) -> bool:
    granted = set(google_auth.status().get("scopes") or [])
    return all(s in granted for s in scopes)


def create_doc(title: str, requests: list[dict[str, Any]], folder_id: str | None) -> dict[str, str]:
    """Create a Google Doc, fill it, and try to file it in `folder_id`."""
    docs = _svc("docs", "v1")
    doc = docs.documents().create(body={"title": title}).execute()
    doc_id = doc["documentId"]
    if requests:
        docs.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()
    where = "My Drive"
    if folder_id:
        try:
            _svc("drive", "v3").files().update(fileId=doc_id, addParents=folder_id,
                                               removeParents="root", fields="id").execute()
            where = "recap folder"
        except Exception as exc:  # noqa: BLE001 - drive.file cannot reach folders it did not create
            where = f"My Drive (could not move to the recap folder: {str(exc)[:120]})"
    return {"id": doc_id, "url": f"https://docs.google.com/document/d/{doc_id}/edit", "where": where}


def plain(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text or "").strip()
