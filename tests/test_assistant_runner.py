"""End-to-end run of the Email Assistant with Gmail, Calendar, Docs, n8n tables
and the model all replaced by fakes. Checks what lands where, and that nothing
but drafts is ever created."""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mailroom.assistant import logic
from mailroom.assistant.logic import ZONE
from mailroom.models import RunSpec
from mailroom.providers.base import LLMProvider
from mailroom.runners import assistant_runner as ar
from mailroom.runners import control

ROWS = json.loads((Path(__file__).parent / "fixtures" / "n8n_settings_rows.json").read_text())
NOW = datetime.now(ZONE)
PENDING = [{"startISO": (NOW + timedelta(days=2)).replace(hour=17, minute=0, second=0, microsecond=0).isoformat(),
            "endISO": (NOW + timedelta(days=2)).replace(hour=17, minute=30, second=0, microsecond=0).isoformat(),
            "label": "Thursday, 17:00-17:30"}]


def mail(i, subject, addr="anna@acme.com", thread=None, text="body"):
    return {"id": f"g{i}", "threadId": thread or f"t{i}", "subject": subject, "messageId": f"<m{i}@x>",
            "from": {"text": f"Anna <{addr}>", "value": [{"address": addr, "name": "Anna"}]},
            "headers": {}, "text": text, "html": "", "snippet": subject, "date": "today",
            "internalDate": str(int(NOW.timestamp() * 1000))}


class FakeModel(LLMProvider):
    name = "claude_cli"

    def __init__(self):
        super().__init__("sonnet")
        self.prompts = []

    def complete(self, system, user, *, max_tokens=1500, temperature=0.2):
        self.prompts.append(user)
        self.usage.input_tokens += 100
        if "STEP 1 -- CONFIRMATION CHECK" in user:
            if "Can we meet" in user:
                return json.dumps({"isAppointmentRequest": True, "durationMinutes": 30, "preferredPartOfDay": "any",
                                   "needsReply": True, "draftReply": "", "isConfirmingProposal": False, "confirmedSlotIndex": -1})
            if "Thursday works" in user:
                return json.dumps({"isAppointmentRequest": False, "isConfirmingProposal": True, "confirmedSlotIndex": 0,
                                   "needsReply": True, "draftReply": "Great, Thursday it is. Alex"})
            if "Newsletter" in user:
                return json.dumps({"isAppointmentRequest": False, "isConfirmingProposal": False, "needsReply": False, "draftReply": ""})
            raise RuntimeError("model hiccup")
        if "requested an appointment" in user:
            return "Here are three times. Alex"
        if "scheduled EVENT" in user:
            if "Subject: Webinar" in user:
                start = (NOW + timedelta(days=5)).replace(hour=15, minute=0, second=0, microsecond=0)
                return json.dumps({"isEvent": True, "eventType": "webinar", "title": "AI Act",
                                   "sessions": [{"start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat()}]})
            return json.dumps({"isEvent": False})
        if "email recap job" in user:
            n = user.count("Subject: ")
            return json.dumps({"emails": [{"subject": f"s{k}", "priority": 4 if k == 0 else 1, "topic": "T",
                                           "summary": "x"} for k in range(n)]})
        raise AssertionError("unexpected prompt")


@pytest.fixture
def world(monkeypatch):
    tables = {"settings": [dict(r) for r in ROWS], "processed": [
        {"id": 1, "trackedGmailId": "g9", "threadId": "t9", "proposalStatus": "none", "processedAt": NOW.isoformat()},
        {"id": 2, "trackedGmailId": "g0", "threadId": "tP", "proposalStatus": "pending", "processedAt": NOW.isoformat(),
         "proposedSlotsJson": json.dumps(PENDING)}],
        "senders": [{"id": 1, "matchPatterns": "anna@acme.com", "senderName": "Anna", "active": True, "category": "career"}],
        "decisions": [{"gmailId": "e9"}], "recaps": []}
    inserts, drafts, docs, searches, updates = [], [], [], [], []
    inbox = [mail(1, "Can we meet next week?"), mail(2, "Newsletter"), mail(3, "Re: times", thread="tP", text="Thursday works"),
             mail(4, "Broken"), mail(9, "Old one")]
    events = [mail(21, "Webinar AI Act", addr="org@x.com"), mail(22, "Corso promo", addr="org@x.com"), mail(23, "seen")]
    events[2]["id"] = "e9"
    recap = [mail(100 + k, f"r{k}") for k in range(12)]

    monkeypatch.setattr(ar.n8n_tables, "_base", lambda rules=None: "https://n8n")
    monkeypatch.setattr(ar.n8n_tables, "rows", lambda kind, base: [dict(r) for r in tables[kind]])
    def ins(kind, base, data):
        inserts.append((kind, data))
        if kind in ("recaps", "processed"):
            tables[kind].append({"id": 100 + len(tables[kind]), **data})
    monkeypatch.setattr(ar.n8n_tables, "insert", ins)
    monkeypatch.setattr(ar.n8n_tables, "update", lambda kind, base, rid, data: updates.append((kind, rid, data)))
    monkeypatch.setattr(ar.google_io, "draft_summaries", lambda: [{"threadId": "t9", "subject": "Re: Old one"}])
    # the proposal on thread tP was sent (that is why Anna could confirm a time)
    monkeypatch.setattr(ar.google_io, "sent_messages",
                        lambda after: [{"threadId": "tP", "internalDate": str(int(NOW.timestamp() * 1000) + 1000)}])
    monkeypatch.setattr(ar, "Rules", type("R", (), {"load": staticmethod(lambda: None)}))

    def search(q, after, limit, full=True):
        searches.append(q)
        if q.startswith("{from:"):
            return inbox
        if "webinar" in q:
            return events
        return recap
    monkeypatch.setattr(ar.google_io, "search", search)
    monkeypatch.setattr(ar.google_io, "calendar_events", lambda cid, a, b: [])
    monkeypatch.setattr(ar.google_io, "create_draft", lambda **kw: drafts.append(kw) or f"d{len(drafts)}")
    monkeypatch.setattr(ar.google_io, "has_scopes", lambda *s: True)
    monkeypatch.setattr(ar.google_auth, "status", lambda: {"connected": True, "missing_scopes": []})
    monkeypatch.setattr(ar.google_io, "create_doc", lambda t, r, f: docs.append((t, r, f)) or
                        {"id": "doc1", "url": "https://docs/doc1", "where": "recap folder"})
    for fn in ("start_run", "finish_run", "update_run_progress", "audit"):
        monkeypatch.setattr(ar.db, fn, (lambda *a, **k: 7))
    return {"inserts": inserts, "drafts": drafts, "docs": docs, "searches": searches, "updates": updates, "tables": tables}


def test_full_run(world):
    model = FakeModel()
    spec = RunSpec(job_id="email-assistant-claude", name="Email Assistant Claude", runner="assistant",
                   provider="claude_cli", steps=["drafting", "events", "recap"])
    res = ar.AssistantRunner(lambda n, m: model).run(spec)
    assert res.status == "succeeded", res.log
    processed = [d for k, d in world["inserts"] if k == "processed"]
    by_id = {d["trackedGmailId"]: d for d in processed}
    # 1: meeting request -> proposed slots held as pending
    assert by_id["g1"]["proposalStatus"] == "pending" and len(json.loads(by_id["g1"]["proposedSlotsJson"])) == 3
    # 2: no reply needed -> recorded, no draft
    assert by_id["g2"]["proposalStatus"] == "no_reply_needed"
    # 3: confirmation -> awaiting your BOOK, with a decision row and a note without recipient
    assert by_id["g3"]["proposalStatus"] == "awaiting_approval"
    meeting = [d for k, d in world["inserts"] if k == "decisions" and d["kind"] == "meeting"]
    assert len(meeting) == 1 and meeting[0]["calendarId"] == "proj@group" and len(meeting[0]["token"]) == 40
    # 4: model failure -> not marked processed (retried next run); 9: already processed -> skipped
    assert "g4" not in by_id and "g9" not in by_id
    replies = [d for d in world["drafts"] if d.get("to")]
    assert {d["to"] for d in replies} == {"anna@acme.com"} and len(replies) == 2
    assert all(d["subject"].lower().startswith("re:") and d["thread_id"] for d in replies)
    assert any("Here are three times" in d["body"] for d in replies)
    notes = [d for d in world["drafts"] if not d.get("to")]
    assert any(d["subject"].startswith("[Inbox Pilot] BOOK?") for d in notes)
    assert any(d["subject"].startswith("[Inbox Pilot] FREE - Webinar: AI Act") for d in notes)
    # events: one note, one 'not_event' row, the already-seen email skipped
    ev_rows = [d for k, d in world["inserts"] if k == "decisions" and d["gmailId"] in ("g21", "g22", "e9")]
    assert sorted((d["gmailId"], d["kind"]) for d in ev_rows) == [("g21", "event"), ("g22", "none")]
    # recap: 12 emails in 2 batches, one doc in the n8n folder
    assert len(world["docs"]) == 1 and world["docs"][0][2] == ar._recap_folder()
    assert "HIGH  (2)" in world["docs"][0][1][0]["insertText"]["text"]
    assert res.stats["reply_drafts"] == 2 and res.stats["event_notes"] == 1 and res.stats["booking_notes"] == 1
    assert res.stats["recap_url"] == "https://docs/doc1" and res.error is None
    recap = [d for k, d in world["inserts"] if k == "recaps"]
    assert len(recap) == 1 and recap[0]["source"] == "claude_cli" and recap[0]["docUrl"] == "https://docs/doc1"
    items = json.loads(recap[0]["itemsJson"])
    assert recap[0]["emailCount"] == 12 and len(items) == 12 and items[0]["gmailId"] == "g100"


def test_stop_between_emails(world, monkeypatch):
    model = FakeModel()
    orig = model.complete

    def stop_after_first(*a, **k):
        out = orig(*a, **k)
        control.request_stop()
        return out
    model.complete = stop_after_first
    spec = RunSpec(job_id="x", name="x", runner="assistant", provider="claude_cli", steps=["drafting", "events"])
    res = ar.AssistantRunner(lambda n, m: model).run(spec)
    assert res.status == "cancelled"
    assert not [k for k, d in world["inserts"] if k == "decisions"]   # events phase never started


def test_recap_saved_in_app_without_docs_permission(world, monkeypatch):
    monkeypatch.setattr(ar.google_io, "has_scopes", lambda *s: False)
    spec = RunSpec(job_id="x", name="x", runner="assistant", provider="claude_cli", steps=["recap"])
    res = ar.AssistantRunner(lambda n, m: FakeModel()).run(spec)
    assert res.status == "succeeded" and not world["docs"]
    assert any("re-authorize Google" in line for line in res.log)
    recap = [d for k, d in world["inserts"] if k == "recaps"]
    assert len(recap) == 1 and recap[0]["docUrl"] == ""          # still saved in MailRoom


def test_google_not_connected_fails_fast(world, monkeypatch):
    monkeypatch.setattr(ar.google_auth, "status", lambda: {"connected": False})
    model = FakeModel()
    spec = RunSpec(job_id="x", name="x", runner="assistant", provider="claude_cli")
    res = ar.AssistantRunner(lambda n, m: model).run(spec)
    assert res.status == "failed" and "Connect / re-authorize Google" in res.error
    assert not model.prompts and not world["searches"]


def test_followup_marks_and_recap_summary(world, monkeypatch):
    monkeypatch.setattr(ar.google_io, "sent_messages", lambda after: [])         # proposal on tP never sent
    for r in world["tables"]["processed"]:
        r["processedAt"] = (NOW - timedelta(days=2)).isoformat()
    res = ar.AssistantRunner(lambda n, m: FakeModel()).run(
        RunSpec(job_id="x", name="x", runner="assistant", provider="claude_cli", steps=["drafting", "recap"]))
    assert res.status == "succeeded", res.log
    # g9's draft is still in Gmail -> ready; g0's proposal draft is gone and never sent -> deleted
    assert ("processed", 1, {"draftStatus": "ready"}) in world["updates"]
    assert ("processed", 2, {"draftStatus": "deleted", "proposalStatus": "withdrawn"}) in world["updates"]
    new_rows = [d for k, d in world["inserts"] if k == "processed"]
    assert all(r["draftStatus"] in ("ready", "") for r in new_rows)
    recap = [d for k, d in world["inserts"] if k == "recaps"][0]
    run = json.loads(recap["runJson"])
    assert run["engine"] == "Claude" and run["newEmails"] == len(new_rows)


def test_empty_run_still_writes_a_recap(world, monkeypatch):
    monkeypatch.setattr(ar.google_io, "search", lambda q, after, limit, full=True: [])
    for r in world["tables"]["processed"]:                                       # handled in an earlier run
        r["processedAt"] = (NOW - timedelta(days=2)).isoformat()
    res = ar.AssistantRunner(lambda n, m: FakeModel()).run(
        RunSpec(job_id="x", name="x", runner="assistant", provider="claude_cli", steps=["drafting", "events", "recap"]))
    assert res.status == "succeeded"
    recap = [d for k, d in world["inserts"] if k == "recaps"]
    assert len(recap) == 1 and recap[0]["emailCount"] == 0 and json.loads(recap[0]["runJson"])["newEmails"] == 0
