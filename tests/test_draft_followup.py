from datetime import datetime, timedelta

from mailroom.assistant import logic
from mailroom.assistant.logic import ZONE

NOW = datetime(2026, 10, 7, 10, 0, tzinfo=ZONE)


def ms(d):
    return str(int(d.timestamp() * 1000))


def row(i, thread, status="none", draft="", hours_ago=5, gid=None):
    return {"id": i, "trackedGmailId": gid or f"g{i}", "threadId": thread, "proposalStatus": status,
            "draftStatus": draft, "processedAt": (NOW - timedelta(hours=hours_ago)).isoformat()}


def test_draft_check_ready_sent_deleted_superseded_and_final():
    rows = [row(1, "tReady", draft="ready"), row(2, "tSent", "pending", "ready"), row(3, "tGone"),
            row(4, "tTwo", hours_ago=30), row(5, "tTwo", hours_ago=2), row(6, "tNo", "no_reply_needed"),
            row(7, "tDisc", draft="discarded"), row(8, "tOld", hours_ago=24 * 9),
            row(9, "tNote", "awaiting_approval")]
    drafts = [{"threadId": "tReady", "subject": "Re: x"}, {"threadId": "tTwo", "subject": "Re: y"},
              {"threadId": "tNote", "subject": "[Inbox Pilot] BOOK? - Meeting"}, {"threadId": "tDisc", "subject": "Re: z"}]
    sent = [{"threadId": "tSent", "internalDate": ms(NOW - timedelta(hours=1))},
            {"threadId": "tGone", "internalDate": ms(NOW - timedelta(hours=10))}]      # sent BEFORE the draft: not a send
    changes = logic.draft_check(rows, drafts, sent, NOW)
    ch = {c["id"]: c["draftStatus"] for c in changes}
    assert ch == {2: "sent", 3: "deleted", 4: "superseded", 5: "ready", 9: "deleted"}
    assert all("proposalStatus" not in c for c in changes)          # no dead *pending* row here
    gone = logic.draft_check([row(1, "tX", "pending", "ready")], [], [], NOW)
    assert gone == [{"id": 1, "draftStatus": "deleted", "proposalStatus": "withdrawn"}]
    # ready stays ready (no change), no_reply_needed / discarded / older than 7 days untouched


def test_handled_ids_redrafts_only_deleted_unsent_replies():
    rows = [row(1, "a", draft="deleted"), row(2, "b", "withdrawn", "deleted"), row(3, "c", "awaiting_approval", "deleted"),
            row(4, "d", draft="discarded"), row(5, "e", draft="sent"),
            row(6, "f", draft="deleted", gid="gX"), row(7, "f", draft="ready", gid="gX")]   # already re-drafted
    done = logic.handled_ids(rows)
    assert "g1" not in done and "g2" not in done                       # re-draft
    assert {"g3", "g4", "g5", "gX"} <= done


def test_dead_proposals_do_not_hold_times_or_feed_the_confirmation_check():
    slot = {"startISO": (NOW + timedelta(days=1)).replace(hour=16, minute=0).isoformat(),
            "endISO": (NOW + timedelta(days=1)).replace(hour=16, minute=30).isoformat(), "label": "x"}
    import json
    dead = {"proposalStatus": "pending", "threadId": "other", "processedAt": NOW.isoformat(),
            "proposedSlotsJson": json.dumps([slot]), "draftStatus": "deleted"}
    assert not logic.live_row(dead) and logic.live_row({"draftStatus": "ready"})
    ctx = logic.attach_context({"threadId": "other", "from": {"text": "a@b"}}, [dead], [], {})
    assert ctx["hasPendingProposal"] is False


def test_run_summary_counts_this_run_and_open_items():
    since = (NOW - timedelta(hours=1)).isoformat()
    rows = [row(1, "a", hours_ago=0.5, draft="ready"), row(2, "b", "pending", hours_ago=0.2, draft="ready"),
            row(3, "c", "no_reply_needed", hours_ago=0.4), row(4, "d", hours_ago=20, draft="sent"),
            row(5, "e", hours_ago=30, draft="deleted")]
    decisions = [{"kind": "event", "status": "pending", "createdAt": (NOW - timedelta(minutes=10)).isoformat()},
                 {"kind": "meeting", "status": "booked", "createdAt": (NOW - timedelta(days=2)).isoformat()}]
    s = logic.run_summary(rows, decisions, since, NOW, "Claude")
    assert (s["newEmails"], s["draftsCreated"], s["meetingProposals"], s["noReplyNeeded"]) == (3, 2, 1, 1)
    assert (s["eventNotes"], s["bookingNotes"], s["notesWaiting"]) == (1, 0, 1)
    assert (s["draftsReady"], s["draftsSent"], s["draftsDeleted"]) == (2, 1, 1)
    empty = logic.run_summary(rows, decisions, NOW.isoformat(), NOW)
    assert empty["newEmails"] == 0 and "nothing new" in logic.summary_lines(empty)[0]


def test_recap_shows_summary_and_draft_states():
    s = logic.run_summary([], [], NOW.isoformat(), NOW, "Gemini")
    doc = logic.recap_document([{"gmailId": "a", "subject": "S", "priority": 4}, {"gmailId": "b", "subject": "T", "priority": 4}],
                               {"a": {"proposalStatus": "none", "draftStatus": "ready"},
                                "b": {"proposalStatus": "pending", "draftStatus": "sent"}}, NOW, s)
    assert doc["text"].index("RUN SUMMARY  (Gemini)") < doc["text"].index("HIGH  (2)")
    assert "Reply: Draft ready, not sent" in doc["text"] and "Reply: Sent  |  Schedule: Proposed" in doc["text"]
    assert doc["summary"]["engine"] == "Gemini"
    none = logic.recap_document([], {}, NOW, s)
    assert "(No emails in this window.)" in none["text"] and "RUN SUMMARY" in none["text"]
