"""Draft review: nothing reaches Gmail until you press Approve, and Approve
creates a *draft* - the app never holds gmail.send scope."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db
from ..connectors import gmail

router = APIRouter(prefix="/api/drafts", tags=["drafts"])


class DraftEdit(BaseModel):
    subject: str | None = None
    body: str | None = None


@router.get("/pending")
def pending():
    return db.pending_drafts()


@router.patch("/{draft_id}")
def edit(draft_id: int, payload: DraftEdit):
    d = db.get_draft(draft_id)
    if not d:
        raise HTTPException(404, "no such draft")
    with db.connect() as c:
        c.execute("UPDATE drafts SET subject=COALESCE(?,subject), body=COALESCE(?,body)"
                  " WHERE id=?", (payload.subject, payload.body, draft_id))
    return {"ok": True}


@router.post("/{draft_id}/approve")
def approve(draft_id: int):
    d = db.get_draft(draft_id)
    if not d:
        raise HTTPException(404, "no such draft")
    if d["status"] != "proposed":
        raise HTTPException(409, f"draft is already {d['status']}")
    account = gmail.get_account(d["account"])
    try:
        rfc_id = account.rfc822_message_id(d["message_id"])
        gmail_id = account.create_draft(
            to=d["sender"], subject=d["subject"] or f"Re: {d['email_subject']}",
            body=d["body"], thread_id=d["thread_id"], in_reply_to=rfc_id,
        )
    except Exception as exc:  # noqa: BLE001
        db.set_draft_status(draft_id, "failed")
        db.audit("user", "draft_approve_failed", str(draft_id), str(exc)[:500])
        raise HTTPException(502, f"Gmail rejected the draft: {exc}") from exc
    db.set_draft_status(draft_id, "approved", gmail_id)
    db.audit("user", "draft_created_in_gmail", str(draft_id), gmail_id)
    return {"ok": True, "gmail_draft_id": gmail_id}


@router.post("/{draft_id}/discard")
def discard(draft_id: int):
    if not db.get_draft(draft_id):
        raise HTTPException(404, "no such draft")
    db.set_draft_status(draft_id, "discarded")
    db.audit("user", "draft_discarded", str(draft_id))
    return {"ok": True}
