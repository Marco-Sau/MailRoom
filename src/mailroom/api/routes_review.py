"""Review tab: the Gmail drafts MailRoom created, readable and editable in the app."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db

router = APIRouter(prefix="/api/review", tags=["review"])


class DraftEdit(BaseModel):
    subject: str
    body: str


def _google_error(exc: Exception) -> HTTPException:
    msg = str(exc)
    if "not connected" in msg.lower():
        return HTTPException(409, "Connect Google in Settings first: Review reads your Gmail drafts "
                                  "through this app's Google sign-in.")
    return HTTPException(502, f"{type(exc).__name__}: {msg[:300]}")


def _remember_discard(msg: dict) -> None:
    """A reply draft discarded here is never re-drafted by the follow-up."""
    from ..assistant import google_io
    from ..connectors import n8n_tables as t
    tid = msg.get("threadId")
    if not tid or google_io.header_map(msg).get("subject", "").startswith("[Inbox Pilot]"):
        return
    try:
        base = t._base()
        rows = [r for r in t.rows("processed", base) if r.get("threadId") == tid
                and r.get("proposalStatus") in ("none", "pending", "awaiting_approval", "withdrawn")]
        if rows:
            newest = max(rows, key=lambda r: int(r.get("id") or 0))
            data = {"draftStatus": "discarded"}
            if newest.get("proposalStatus") == "pending":
                data["proposalStatus"] = "withdrawn"           # its times are no longer held
            t.update("processed", base, newest["id"], data)
    except Exception:  # noqa: BLE001 - the draft is gone either way; worst case it is re-drafted once
        pass


@router.get("")
def review_list(fresh: bool = False):
    from .. import review
    try:
        return review.list_review(fresh)
    except Exception as exc:  # noqa: BLE001
        raise _google_error(exc) from exc


@router.put("/{draft_id}")
def review_edit(draft_id: str, payload: DraftEdit):
    from ..assistant import google_io
    if not payload.body.strip():
        raise HTTPException(400, "the draft body cannot be empty")
    try:
        google_io.update_draft(draft_id, subject=payload.subject, body=payload.body)
    except Exception as exc:  # noqa: BLE001
        raise _google_error(exc) from exc
    from .. import review
    review.invalidate()
    db.audit("user", "draft_edited", draft_id, payload.subject[:200])
    return {"ok": True}


@router.delete("/{draft_id}")
def review_discard(draft_id: str):
    from ..assistant import google_io
    try:
        meta = (google_io.get_draft(draft_id, "metadata").get("message") or {})
        google_io.delete_draft(draft_id)
    except Exception as exc:  # noqa: BLE001
        raise _google_error(exc) from exc
    _remember_discard(meta)
    from .. import review
    review.invalidate()
    db.audit("user", "draft_discarded", draft_id, None)
    return {"ok": True}
