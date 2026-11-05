"""Reply-drafting scope and tracked senders, edited through n8n data tables."""
from __future__ import annotations

import json

import pytest

from mailroom import drafting_settings as ds
from mailroom.connectors import n8n_tables as t


@pytest.fixture
def tables(monkeypatch):
    db = {"settings": [{"id": 1, "key": "draftScope", "value": "senders"},
                       {"id": 2, "key": "signatureName", "value": "Alex Doe"}],
          "senders": [{"id": 1, "matchPatterns": "dev@example.com", "senderName": "Dev",
                       "notes": "", "active": True}]}
    nxt = {"settings": 10, "senders": 10}
    monkeypatch.setattr(t, "_base", lambda rules=None: "B")
    monkeypatch.setattr(t, "rows", lambda kind, base: [dict(r) for r in db[kind]])

    def insert(kind, base, data):
        nxt[kind] += 1
        db[kind].append({"id": nxt[kind], **data})
    def update(kind, base, rid, data):
        next(r for r in db[kind] if r["id"] == rid).update(data)
    def delete(kind, base, rid):
        db[kind][:] = [r for r in db[kind] if r["id"] != rid]
    monkeypatch.setattr(t, "insert", insert)
    monkeypatch.setattr(t, "update", update)
    monkeypatch.setattr(t, "delete", delete)
    return db


def test_load_merges_defaults_and_counts_active(tables):
    d = ds.load()
    assert d["draftScope"] == "senders" and d["maxEmails"] == "25" and d["active_senders"] == 1


def test_switch_to_whole_inbox_updates_existing_and_inserts_missing(tables):
    d = ds.save_settings({"draftScope": "inbox", "maxEmails": 40, "defaultNotes": "Friendly",
                          "signatureName": None})
    assert d["draftScope"] == "inbox" and d["maxEmails"] == "40" and d["defaultNotes"] == "Friendly"
    keys = [r["key"] for r in tables["settings"]]
    assert keys.count("draftScope") == 1 and "maxEmails" in keys


@pytest.mark.parametrize("bad", [{"draftScope": "everything"}, {"maxEmails": 0},
                                 {"maxEmails": 99}, {"signatureName": " "}])
def test_invalid_settings_are_refused(tables, bad):
    with pytest.raises(ValueError):
        ds.save_settings(bad)


def test_patterns_are_normalised_and_validated():
    assert ds.normalize_patterns("Anna@Acme.it; client.com  @x.org,anna@acme.it") == \
        "anna@acme.it, @client.com, @x.org"
    for bad in ["", "not an address", "a@b"]:
        with pytest.raises(ValueError):
            ds.normalize_patterns(bad)


def test_add_update_delete_sender(tables):
    d = ds.add_sender("Anna@Acme.it", "Anna - Acme", "formal, in French")
    new = next(s for s in d["senders"] if s["matchPatterns"] == "anna@acme.it")
    assert new["active"] and new["notes"] == "formal, in French"
    with pytest.raises(ValueError, match="already tracked"):
        ds.add_sender("anna@acme.it")
    d = ds.update_sender(new["id"], {"active": False, "matchPatterns": None, "notes": "brief"})
    s = next(x for x in d["senders"] if x["id"] == new["id"])
    assert s["active"] is False and s["notes"] == "brief" and s["matchPatterns"] == "anna@acme.it"
    d = ds.delete_sender(new["id"])
    assert all(x["id"] != new["id"] for x in d["senders"])


def test_row_filters_target_one_id(monkeypatch):
    seen = []
    monkeypatch.setattr(t, "table_id", lambda kind, base: "TID")
    monkeypatch.setattr(t, "_req", lambda m, url, **kw: seen.append((m, url, kw)))
    t.update("senders", "B", 7, {"active": False})
    t.delete("senders", "B", 7)
    upd, dele = seen
    assert upd[0] == "PATCH" and upd[1] == "B/api/v1/data-tables/TID/rows/update"
    assert upd[2]["json"]["filter"]["filters"] == [{"columnName": "id", "condition": "eq", "value": 7}]
    assert dele[0] == "DELETE" and json.loads(dele[2]["params"]["filter"])["filters"][0]["value"] == 7


def test_hours_round_trip_and_errors():
    w = ds.parse_hours("Mon-Fri 09:00-17:00; Wed 18:00-20:00")
    assert w["3"] == [["09:00", "17:00"], ["18:00", "20:00"]] and "6" not in w
    assert ds.parse_hours(ds.format_hours(w)) == w
    assert ds.format_hours(ds.parse_hours("Daily 09:00-21:00")) == "Daily 09:00-21:00"
    assert ds.parse_hours("Sat/Sun 10:00-12:00")["7"] == [["10:00", "12:00"]]
    for bad in ["Someday 09:00-10:00", "Mon 10:00-09:00", "Mon nine to five"]:
        with pytest.raises(ValueError):
            ds.parse_hours(bad)


def test_scheduling_settings_saved_as_json(tables):
    tables["settings"].append({"id": 50, "key": "calendars",
                               "value": json.dumps([{"id": "personal", "name": "Personal"}, {"id": "job", "name": "Work"}])})
    d = ds.save_settings({"hours": {"fulltime": "Mon-Fri 09:00-17:00", "career": "Mon-Fri 17:30-20:00"},
                          "busyCalendars": ["personal", "job"], "bookingCalendars": {"fulltime": "job", "bogus": "x"},
                          "defaultCategory": "anytime", "workCalendar": "job", "presenceMinHours": 4})
    stored = {r["key"]: r["value"] for r in tables["settings"]}
    assert json.loads(stored["workHours"])["1"] == [["09:00", "17:00"]]
    assert json.loads(stored["bookingCalendars"]) == {"fulltime": "job"}
    assert d["hours"]["fulltime"] == "Mon-Fri 09:00-17:00" and d["busyCalendars"] == ["personal", "job"]
    assert d["defaultCategory"] == "anytime" and stored["presenceMinHours"] == "4"
    with pytest.raises(ValueError, match="unknown calendar"):
        ds.save_settings({"workCalendar": "nope"})
    with pytest.raises(ValueError):
        ds.save_settings({"busyCalendars": []})


def test_sender_category_and_calendar(tables):
    tables["settings"].append({"id": 50, "key": "calendars", "value": json.dumps([{"id": "job", "name": "Work"}])})
    d = ds.add_sender("boss@work.example", "Boss", "", True, "fulltime", "job")
    s = next(x for x in d["senders"] if x["matchPatterns"] == "boss@work.example")
    assert s["category"] == "fulltime" and s["calendarId"] == "job"
    with pytest.raises(ValueError):
        ds.update_sender(s["id"], {"category": "weekend"})
    d = ds.update_sender(s["id"], {"category": "", "calendarId": ""})
    s = next(x for x in d["senders"] if x["id"] == s["id"])
    assert s["category"] == "" and s["calendarId"] == ""
