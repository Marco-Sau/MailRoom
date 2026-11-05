"""Port of the n8n Code-node logic. These pin the behaviours that matter; the
full JS-vs-Python differential run (thousands of random calendars) lives in the
dev scratchpad because it needs node + the n8n snippets."""
import json
from datetime import datetime, timedelta
from pathlib import Path

from mailroom.assistant import logic
from mailroom.assistant.logic import ZONE

ROWS = json.loads((Path(__file__).parent / "fixtures" / "n8n_settings_rows.json").read_text())
ST = logic.settings_from_rows(ROWS)
NOW = datetime(2026, 10, 5, 9, 10, tzinfo=ZONE)          # Monday


def req(cat="career", thread="t1", **out):
    return {"id": "m1", "threadId": thread, "senderCategory": cat, "subject": "Call?",
            "from": {"text": "Ann <ann@x.it>", "value": [{"address": "ann@x.it", "name": "Ann"}]},
            "output": {"isAppointmentRequest": True, **out}}


def ev(s, e, cal="primary", **kw):
    return {"id": f"e{s}", "status": "confirmed", "start": {"dateTime": s.isoformat()},
            "end": {"dateTime": e.isoformat()}, "__calendarId": cal, **kw}


def at(day, hh, mm=0):
    return (NOW + timedelta(days=day)).replace(hour=hh, minute=mm, second=0, microsecond=0)


def starts(r):
    return [logic.parse_dt(s["startISO"]).astimezone(ZONE) for s in r["freeSlots"]]


def test_build_query_scopes():
    senders = [{"matchPatterns": "anna@acme.com, @client.com", "active": True},
               {"matchPatterns": "bob@x.it", "active": False}]
    q = logic.build_query(ST, senders)
    assert q["scope"] == "senders" and q["maxEmails"] == 25
    assert q["gmailQuery"].startswith('{from:"anna@acme.com" from:"@client.com"} -in:sent')
    assert logic.build_query({**ST, "draftScope": "inbox", "maxEmails": "99"}, [])["maxEmails"] == 50
    assert logic.build_query(ST, [{"matchPatterns": "x@y", "active": "false"}]) is None


def test_bulk_detection():
    mk = lambda addr, h=None: {"from": {"value": [{"address": addr}]}, "headers": h or {}}  # noqa: E731
    assert logic.is_bulk(mk("noreply@x.com"))
    assert logic.is_bulk(mk("a@x.com", {"list-unsubscribe": "list-unsubscribe: <x>"}))
    assert logic.is_bulk(mk("a@x.com", {"auto-submitted": "auto-submitted: auto-replied"}))
    assert not logic.is_bulk(mk("a@x.com", {"auto-submitted": "auto-submitted: no"}))
    assert not logic.is_bulk(mk("anna.reply@x.com"))


def test_attach_context_uses_latest_pending_and_sender_settings():
    slots = [{"startISO": "2026-10-07T17:00:00.000+02:00", "endISO": "2026-10-07T17:30:00.000+02:00", "label": "Wed"}]
    rows = [{"threadId": "tA", "proposalStatus": "pending", "processedAt": "2026-10-01", "proposedSlotsJson": "[]"},
            {"threadId": "tA", "proposalStatus": "pending", "processedAt": "2026-10-03", "proposedSlotsJson": json.dumps(slots)}]
    senders = [{"matchPatterns": "anna@acme.com", "senderName": "Anna", "notes": "formal", "category": "fulltime",
                "calendarId": "job@group"}]
    j = logic.attach_context({"threadId": "tA", "from": {"text": "Anna <anna@acme.com>"}}, rows, senders, ST)
    assert j["hasPendingProposal"] and j["pendingSlotsText"] == "Wed"
    assert (j["senderCategory"], j["senderCalendarId"], j["senderNotes"]) == ("fulltime", "job@group", "formal")
    other = logic.attach_context({"threadId": "tZ", "from": {"text": "z@z"}}, rows, senders, ST)
    assert not other["hasPendingProposal"] and other["senderCategory"] == "career"


def test_busy_interval_rules():
    s, e = at(1, 10), at(1, 11)
    assert logic.busy_interval(ev(s, e))
    assert logic.busy_interval(ev(s, e, status="cancelled")) is None
    assert logic.busy_interval(ev(s, e, transparency="transparent")) is None
    assert logic.busy_interval(ev(s, e, eventType="workingLocation")) is None
    assert logic.busy_interval(ev(s, e, attendees=[{"self": True, "responseStatus": "declined"}])) is None
    allday = logic.busy_interval({"start": {"date": "2026-10-06"}, "end": {"date": "2026-10-07"}})
    assert allday[2] and allday[0].hour == 0


def test_career_slots_avoid_work_hours_busy_and_notice():
    busy = [ev(at(0, 15), at(0, 20)), ev(at(1, 15), at(1, 17))]
    r = logic.compute_slots([req("career")], busy, True, [], ST, NOW)[0]
    assert len(r["freeSlots"]) == 3 and r["calendarOk"] and not r["slotsRelaxed"]
    for s in starts(r):
        assert s.isoweekday() <= 5 and 15 * 60 <= s.hour * 60 + s.minute and s.hour < 20
        assert s >= NOW + timedelta(hours=2)
        assert not (s.date() == at(1, 0).date() and s.hour < 17)
    assert len({s.date() for s in starts(r)}) == 3           # spread over different days


def test_fulltime_inside_presence_blocks_only():
    # Tuesday shift 09:00-15:30 in the job calendar = presence; a 10:00 meeting inside it is busy.
    busy = [ev(at(1, 9), at(1, 15, 30), cal="job@group"), ev(at(1, 10), at(1, 11), cal="primary")]
    r = logic.compute_slots([req("fulltime", preferredStartDate="2026-10-06", preferredEndDate="2026-10-06")],
                            busy, True, [], ST, NOW)[0]
    tue = [s for s in starts(r) if s.date() == at(1, 0).date()]
    assert tue and all(9 * 60 <= s.hour * 60 + s.minute and s.hour * 60 + s.minute + 30 <= 15 * 60 + 30 for s in tue)
    assert all(not (10 <= s.hour < 11) for s in tue)


def test_presence_blocks_other_categories():
    busy = [ev(at(1, 14), at(1, 19), cal="job@group")]
    r = logic.compute_slots([req("career", preferredStartDate="2026-10-06", preferredEndDate="2026-10-06")],
                            busy, True, [], ST, NOW)[0]
    assert all(s.date() != at(1, 0).date() or s.hour >= 19 for s in starts(r))


def test_pending_proposals_are_held_except_same_thread():
    slot = {"startISO": at(1, 17, 30).isoformat(), "endISO": at(1, 18).isoformat()}
    rows = [{"proposalStatus": "pending", "threadId": "other", "processedAt": NOW.isoformat(),
             "proposedSlotsJson": json.dumps([slot])}]
    one_day = dict(preferredStartDate="2026-10-06", preferredEndDate="2026-10-06", durationMinutes=30)
    held = logic.compute_slots([req("career", **one_day)], [], True, rows, ST, NOW)[0]
    assert at(1, 17, 30) not in starts(held)
    same = logic.compute_slots([req("career", thread="other", **one_day)], [], True, rows, ST, NOW)[0]
    assert at(1, 17, 30) in starts(same)


def test_relaxed_only_when_a_slot_is_outside_the_request():
    busy = [ev(at(1, 0), at(2, 0), cal="primary")]           # Tuesday fully busy
    r = logic.compute_slots([req("career", preferredStartDate="2026-10-06", preferredEndDate="2026-10-06")],
                            busy, True, [], ST, NOW)[0]
    assert r["freeSlots"] and r["slotsRelaxed"]


def test_calendar_unavailable_means_no_slots():
    r = logic.compute_slots([req()], [], False, [], ST, NOW)[0]
    assert r["freeSlots"] == [] and "temporarily unavailable" in r["freeSlotsText"]


def test_slots_sorted_after_widening():
    # Few free evenings in the window -> widening adds later days; result must stay chronological.
    busy = [ev(at(d, 15), at(d, 20)) for d in range(0, 14) if d != 4]
    r = logic.compute_slots([req("career", preferredStartDate="2026-10-09", preferredEndDate="2026-10-09")],
                            busy, True, [], ST, NOW)[0]
    s = starts(r)
    assert s == sorted(s)


def test_dst_day_keeps_wall_clock_times():
    now = datetime(2026, 10, 23, 8, 0, tzinfo=ZONE)
    sun = datetime(2026, 10, 25, 0, 0, tzinfo=ZONE)          # clocks go back at 03:00
    busy = [ev(sun.replace(hour=7, minute=30), sun.replace(hour=14), cal="job@group")]
    r = logic.compute_slots([req("fulltime", preferredStartDate="2026-10-25", preferredEndDate="2026-10-25")],
                            busy, True, [], ST, now)[0]
    first = starts(r)[0]
    assert first.date() == sun.date() and (first.hour, first.minute) >= (7, 30)


def test_booking_note_calendar_choice_and_links():
    j = {"id": "g1", "threadId": "t1", "subject": "Hello", "senderCategory": "fulltime", "senderCalendarId": "",
         "from": {"value": [{"address": "anna@acme.com", "name": "Anna"}]},
         "startISO": "2026-10-07T17:00:00.000+02:00", "endISO": "2026-10-07T17:30:00.000+02:00"}
    n = logic.booking_note(j, "tok/1", ST, "https://n8n/decide")
    assert n["calendarId"] == "job@group" and n["calendarName"] == "Work"
    assert "https://n8n/decide?t=tok%2F1&a=book" in n["draftBody"]
    assert n["sessionsJson"] == '[{"startISO":"2026-10-07T17:00:00.000+02:00","endISO":"2026-10-07T17:30:00.000+02:00"}]'
    assert n["draftSubject"] == "[Inbox Pilot] BOOK? - Meeting with Anna - Wed 7 Oct 17:00"


def test_events_extraction_conflicts_and_note():
    item = {"id": "g1", "threadId": "t1", "subject": "Invito", "from": {"text": "Org <o@x>", "value": [{"address": "o@x"}]}}
    o = {"isEvent": True, "eventType": "webinar", "title": "AI Act",
         "sessions": [{"start": "2026-10-09T15:00:00+02:00"}, {"start": "2026-09-01T10:00:00+02:00"}]}
    e = logic.merge_extraction(item, o, False, NOW)
    assert e["isEvent"] and len(e["sessions"]) == 1                       # past session dropped
    assert logic.parse_dt(e["sessions"][0]["endISO"]) - logic.parse_dt(e["sessions"][0]["startISO"]) == timedelta(hours=1)
    holiday = {"id": "h", "start": {"date": "2026-10-09"}, "end": {"date": "2026-10-10"},
               "__calendarId": "en.usa#holiday@group.v.calendar.google.com"}
    call = ev(datetime(2026, 10, 9, 14, 30, tzinfo=ZONE), datetime(2026, 10, 9, 15, 30, tzinfo=ZONE), summary="Call")
    d = logic.detect_conflicts(e, [holiday, call], True)
    assert [c["summary"] for c in d["conflicts"]] == ["Call"]
    note = logic.event_note(d, "tk", "https://n8n/decide")
    assert note["state"] == "CONFLICT" and "a=keep" in note["draftBody"] and "a=swap" in note["draftBody"]
    same = ev(datetime(2026, 10, 9, 15, 2, tzinfo=ZONE), datetime(2026, 10, 9, 16, 0, tzinfo=ZONE))
    assert logic.detect_conflicts(e, [same], True)["alreadyInCalendar"]
    assert logic.merge_extraction(item, {}, True, NOW)["recordStatus"] == "llm_error"


def test_recap_document_groups_and_bold_ranges():
    emails = [{"gmailId": "a", "subject": "Città", "priority": 5, "topic": "Security", "summary": "OTP"},
              {"gmailId": "b", "subject": "News", "priority": "2", "topic": "", "summary": "Promo"},
              {"gmailId": "c", "subject": "Hi", "priority": 4}]
    processed = {"a": {"proposalStatus": "no_reply_needed"}, "c": {"proposalStatus": "pending"}}
    doc = logic.recap_document(emails, processed, NOW)
    t = doc["text"]
    assert t.index("CRITICAL  (1)") < t.index("HIGH  (1)") < t.index("LOW  (1)")
    assert "Reply: No reply needed" in t and "Schedule: Proposed, awaiting confirmation" in t
    assert "Reply: Not tracked" in t and "Topic: Uncategorized" in t
    assert doc["title"] == "Email Recap - 05-10-2026 09:10"
    bold = [r["updateTextStyle"]["range"] for r in doc["requests"][1:]]
    assert len(bold) == 3 and all(b["endIndex"] > b["startIndex"] for b in bold)
