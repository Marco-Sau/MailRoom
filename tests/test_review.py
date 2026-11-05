import base64
import json

from mailroom import review


def _msg(mid, thread, subject, body, to="", labels=("DRAFT",)):
    return {"id": mid, "threadId": thread, "internalDate": "1791300000000", "labelIds": list(labels),
            "payload": {"mimeType": "text/plain", "headers": [{"name": "Subject", "value": subject}, {"name": "To", "value": to}],
                        "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}}


def test_review_lists_mailroom_drafts_only(monkeypatch):
    drafts = {
        "d1": _msg("m1", "tA", "Re: Call?", "Here are three times. Alex", "anna@acme.com"),
        "d2": _msg("m2", "tB", "[Inbox Pilot] BOOK? - Meeting with Anna - Thu 8 Oct 17:00",
                   "BOOK it:\nhttps://n8n/x?t=abc&a=book\n\nDo NOT book it:\nhttps://n8n/x?t=abc&a=decline"),
        "d3": _msg("m3", "tC", "My own draft", "personal"),                 # not MailRoom's thread
        "d4": _msg("m4", "tD", "Re: Thanks", "ok"),                         # assistant said no reply needed
        "d5": _msg("m5", "tZ", "[Inbox Pilot] CONFLICT - Webinar: AI - Fri", "KEEP:\nhttps://n8n/x?t=q&a=keep\nSWAP:\nhttps://n8n/x?t=q&a=swap"),
    }
    processed = [{"threadId": "tA", "proposalStatus": "pending", "processedAt": "2026-10-06",
                  "proposedSlotsJson": json.dumps([{"label": "Thursday 8 October, 17:00-17:30"}])},
                 {"threadId": "tD", "proposalStatus": "no_reply_needed", "processedAt": "2026-10-06"}]
    monkeypatch.setattr(review.google_io, "list_drafts", lambda: [{"id": k, "message": {"id": v["id"], "threadId": v["threadId"]}} for k, v in drafts.items()])
    monkeypatch.setattr(review.google_io, "get_draft", lambda i, fmt="full": {"id": i, "message": drafts[i]})
    monkeypatch.setattr(review.google_io, "thread_original", lambda tid: {"from": "Anna", "subject": "Call?", "snippet": "can we talk",
                                                                          "date": "", "internalDate": "1"})
    monkeypatch.setattr(review.t, "_base", lambda *a: "b")
    monkeypatch.setattr(review.t, "rows", lambda kind, base: processed if kind == "processed" else [{"threadId": "tB"}])
    review.invalidate()
    out = review.list_review(fresh=True)
    by = {i["draftId"]: i for i in out["items"]}
    assert set(by) == {"d1", "d2", "d5"}
    assert by["d1"]["kind"] == "reply" and by["d1"]["slots"] == ["Thursday 8 October, 17:00-17:30"]
    assert by["d1"]["body"].startswith("Here are three times") and by["d1"]["original"]["from"] == "Anna"
    assert by["d2"]["kind"] == "booking" and [a["action"] for a in by["d2"]["actions"]] == ["book", "decline"]
    assert by["d5"]["kind"] == "event-conflict" and [a["label"] for a in by["d5"]["actions"]] == ["Keep my schedule", "Swap to the event"]
    assert out["counts"] == {"replies": 1, "notes": 2}


def test_note_kind():
    assert review.note_kind("[Inbox Pilot] FREE - Webinar: X") == "event-free"
    assert review.note_kind("[Inbox Pilot] CHECK - Course: X") == "event-check"
    assert review.note_kind("Re: hello") is None


def test_update_draft_keeps_recipient_thread_and_reply_headers(monkeypatch):
    from email import message_from_bytes
    from mailroom.assistant import google_io
    sent = {}

    class Call:
        def __init__(self, result):
            self.result = result

        def execute(self):
            return self.result

    class Drafts:
        def get(self, **kw):
            return Call({"id": "d1", "message": {"id": "m1", "threadId": "tA", "payload": {"headers": [
                {"name": "To", "value": "anna@acme.com"}, {"name": "Subject", "value": "Re: Call?"},
                {"name": "In-Reply-To", "value": "<x@acme>"}, {"name": "References", "value": "<x@acme>"}]}}})

        def update(self, **kw):
            sent.update(kw)
            return Call({"id": "d1"})

    class Users:
        def drafts(self):
            return Drafts()

    class Svc:
        def users(self):
            return Users()
    monkeypatch.setattr(google_io, "_svc", lambda api, v: Svc())
    google_io.update_draft("d1", subject="Re: Call?", body="Edited text")
    msg = sent["body"]["message"]
    mime = message_from_bytes(base64.urlsafe_b64decode(msg["raw"]))
    assert msg["threadId"] == "tA" and mime["To"] == "anna@acme.com" and mime["In-Reply-To"] == "<x@acme>"
    assert mime.get_payload(decode=True).decode().strip() == "Edited text"
