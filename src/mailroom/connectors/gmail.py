"""Gmail connector.

The MailAccount protocol below is the seam that lets an IMAP account
(for example a work mailbox over IMAPS) be added later without touching the
pipeline: it only ever sees EmailRef objects and a `create_draft` call.
"""
from __future__ import annotations

import base64
import email.utils
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Protocol

from ..models import EmailRef
from . import google_auth

MAX_BODY_CHARS = 8000
_WS = re.compile(r"[ \t]+")


class MailAccount(Protocol):
    account_id: str

    def fetch(self, query: str, max_results: int) -> list[EmailRef]: ...
    def create_draft(self, *, to: str, subject: str, body: str,
                     thread_id: str | None, in_reply_to: str | None) -> str: ...


def _decode(part_data: str) -> str:
    return base64.urlsafe_b64decode(part_data.encode()).decode("utf-8", errors="replace")


def _walk_for_text(payload: dict[str, Any]) -> str:
    """Prefer text/plain; fall back to stripped text/html."""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})
    if mime == "text/plain" and body.get("data"):
        return _decode(body["data"])
    if mime.startswith("multipart"):
        for part in payload.get("parts", []) or []:
            text = _walk_for_text(part)
            if text:
                return text
    if mime == "text/html" and body.get("data"):
        html = _decode(body["data"])
        return re.sub(r"<[^>]+>", " ", html)
    return ""


def _clean(text: str) -> str:
    text = _WS.sub(" ", text)
    lines = [ln.strip() for ln in text.splitlines()]
    # Drop quoted history; the model only needs the new content.
    out: list[str] = []
    for ln in lines:
        if ln.startswith(">") or re.match(r"^(On .+ wrote:|Il .+ ha scritto:)$", ln):
            break
        out.append(ln)
    return "\n".join([ln for ln in out if ln])[:MAX_BODY_CHARS]


class GmailAccount:
    account_id = "gmail-personal"

    def __init__(self, account_id: str | None = None) -> None:
        if account_id:
            self.account_id = account_id
        self._svc = None

    @property
    def svc(self):
        if self._svc is None:
            self._svc = google_auth.service("gmail", "v1")
        return self._svc

    def profile(self) -> dict[str, Any]:
        return self.svc.users().getProfile(userId="me").execute()

    def fetch(self, query: str, max_results: int = 40) -> list[EmailRef]:
        listing = self.svc.users().messages().list(
            userId="me", q=query, maxResults=min(max_results, 100)
        ).execute()
        out: list[EmailRef] = []
        for ref in listing.get("messages", []) or []:
            msg = self.svc.users().messages().get(
                userId="me", id=ref["id"], format="full"
            ).execute()
            out.append(self._to_ref(msg))
        return out

    def _to_ref(self, msg: dict[str, Any]) -> EmailRef:
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        name, addr = email.utils.parseaddr(headers.get("from", ""))
        ts = int(msg.get("internalDate", "0")) / 1000
        return EmailRef(
            account=self.account_id,
            message_id=msg["id"],
            thread_id=msg.get("threadId"),
            sender=addr.lower(),
            sender_name=name or None,
            to=[a for _, a in email.utils.getaddresses([headers.get("to", "")]) if a],
            subject=headers.get("subject", "(no subject)"),
            received_at=datetime.fromtimestamp(ts, tz=timezone.utc),
            snippet=msg.get("snippet", ""),
            body_text=_clean(_walk_for_text(msg["payload"])) or msg.get("snippet", ""),
            labels=msg.get("labelIds", []),
            web_link=f"https://mail.google.com/mail/u/0/#inbox/{msg.get('threadId')}",
        )

    def create_draft(self, *, to: str, subject: str, body: str,
                     thread_id: str | None = None, in_reply_to: str | None = None) -> str:
        mime = EmailMessage()
        mime["To"] = to
        mime["Subject"] = subject
        if in_reply_to:
            mime["In-Reply-To"] = in_reply_to
            mime["References"] = in_reply_to
        mime.set_content(body)
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        payload: dict[str, Any] = {"message": {"raw": raw}}
        if thread_id:
            payload["message"]["threadId"] = thread_id
        draft = self.svc.users().drafts().create(userId="me", body=payload).execute()
        return draft["id"]

    def rfc822_message_id(self, message_id: str) -> str | None:
        msg = self.svc.users().messages().get(
            userId="me", id=message_id, format="metadata",
            metadataHeaders=["Message-Id"]
        ).execute()
        for h in msg["payload"].get("headers", []):
            if h["name"].lower() == "message-id":
                return h["value"]
        return None


def get_account(account_id: str) -> MailAccount:
    if account_id.startswith("gmail"):
        return GmailAccount(account_id)
    raise NotImplementedError(
        f"account '{account_id}' not supported yet (IMAP backend is planned)"
    )
