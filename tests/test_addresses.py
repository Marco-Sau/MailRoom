import json

import pytest

from mailroom import addresses as ad

P = "me@example.com"
HOT = "alex.doe@hotmail.example"


def test_query_tracks_everything_by_default():
    assert ad.build_query(P, [P, HOT], []) == ""


def test_untick_an_alias():
    q = ad.build_query(P, [P, HOT, "x@y.it"], [HOT])
    assert q == "-{to:alex.doe@hotmail.example cc:alex.doe@hotmail.example deliveredto:alex.doe@hotmail.example}"


def test_untick_the_main_address_keeps_only_mail_to_the_others():
    q = ad.build_query(P, [P, HOT, "w@work.example"], [P])
    assert q == ("{to:alex.doe@hotmail.example cc:alex.doe@hotmail.example deliveredto:alex.doe@hotmail.example "
                 "to:w@work.example cc:w@work.example deliveredto:w@work.example}")


def test_cannot_untick_everything():
    with pytest.raises(ValueError):
        ad.build_query(P, [P, HOT], [P, HOT])


def test_summarize_finds_aliases_forwards_and_received_as():
    msgs = [
        {"to": [P], "delivered-to": [P]},
        {"to": [f"Alex <{HOT}>"], "delivered-to": [P]},                       # Outlook forward
        {"to": ["alex.doe@work.example"], "delivered-to": [P, "alex.doe@work.example"]},  # Gmail-style forward
        {"to": ["solo@old-account.it"], "delivered-to": [P]},                 # sole recipient: forward of yours
        {"to": ["team@list.example.org"], "list-id": ["<team.list.example.org>"], "delivered-to": [P]},  # list
        {"delivered-to": [P]},                                                 # BCC / undisclosed
        {"to": ["a@b.com"], "x-forwarded-for": [f"{P} w@work.it"], "delivered-to": [P]},
        {"to": ["solo@old-account.it"], "delivered-to": [P]},
        {"to": ["ana@partner.example", "ben@partner.example"], "cc": ["cleo@partner.example"], "delivered-to": [P]},  # you in BCC
        {"to": ["ana@partner.example", "ben@partner.example"], "delivered-to": [P]},                         # again
        {"to": ["alex.doe@work.example"], "delivered-to": [P, "alex.doe@work.example"]},
        {"to": ["a@b.com"], "x-forwarded-for": [f"{P} w@work.it"], "delivered-to": [P]},
        {"to": ['"boss@work.it"'], "delivered-to": [P]},                        # seen once: noise
    ]
    out = {r["address"]: r for r in ad.summarize(P, [HOT], msgs)}
    assert out[P]["kinds"] == ["main"] and out[P]["count"] == 2
    assert out[HOT]["kinds"] == ["send-as"] and out[HOT]["count"] == 1
    assert out["alex.doe@work.example"]["kinds"] == ["forwarded"] and out["alex.doe@work.example"]["count"] == 2
    assert out["w@work.it"]["kinds"] == ["forwarded"] and out["w@work.it"]["count"] == 2
    assert out["solo@old-account.it"]["kinds"] == ["received-as"] and out["solo@old-account.it"]["count"] == 2
    assert not {"team@list.example.org", "ana@partner.example", "ben@partner.example", "cleo@partner.example"} & set(out)
    assert "boss@work.it" not in out and '"boss@work.it"' not in out       # single sighting dropped
    assert list(out)[0] == P                                                  # main first


def test_save_excluded_writes_shared_settings(monkeypatch):
    rows = [{"id": 1, "key": "primaryAddress", "value": P},
            {"id": 2, "key": "ownAddresses", "value": json.dumps([{"address": P, "kinds": ["main"], "count": 3},
                                                                  {"address": HOT, "kinds": ["send-as"], "count": 2}])}]
    writes = []
    monkeypatch.setattr(ad.t, "_base", lambda *a: "b")
    monkeypatch.setattr(ad.t, "rows", lambda kind, base: [dict(r) for r in rows])

    def ins(kind, base, data):
        rows.append({"id": len(rows) + 1, **data}); writes.append(data)

    def upd(kind, base, rid, data):
        next(r for r in rows if r["id"] == rid).update(data); writes.append(data)
    monkeypatch.setattr(ad.t, "insert", ins)
    monkeypatch.setattr(ad.t, "update", upd)
    out = ad.save_excluded([HOT.upper(), "unknown@z.com"])
    assert out["excluded"] == [HOT] and out["query"].startswith("-{to:alex.doe@hotmail.example")
    assert [a["tracked"] for a in out["addresses"]] == [True, False]
    with pytest.raises(ValueError):
        ad.save_excluded([P, HOT])


def test_drafting_and_runner_append_the_filter():
    from mailroom.assistant import logic
    from mailroom.runners.assistant_runner import _with_addresses
    st = {"draftScope": "inbox", "addressQuery": "-{to:a@b cc:a@b deliveredto:a@b}"}
    assert logic.build_query(st, [])["gmailQuery"].endswith(" -{to:a@b cc:a@b deliveredto:a@b}")
    assert _with_addresses("webinar", st) == "webinar -{to:a@b cc:a@b deliveredto:a@b}"
    assert _with_addresses("webinar", {}) == "webinar"


def test_quoted_addresses_are_normalized():
    assert ad.norm('"Alex.Doe@hotmail.example"') == "alex.doe@hotmail.example"
    out = {r["address"] for r in ad.summarize(P, [], [{"to": ['"x@y.it"']}, {"to": ["x@y.it"]}])}
    assert out == {P, "x@y.it"}
