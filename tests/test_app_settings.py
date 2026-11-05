"""Settings → all formerly hard-coded values: defaults, validation, local vs shared storage."""
import json

import pytest
from fastapi.testclient import TestClient

from mailroom import app_settings as aps
from mailroom.assistant import logic


@pytest.fixture
def table(monkeypatch):
    rows = []
    from mailroom.connectors import n8n_tables as t
    monkeypatch.setattr(t, "_base", lambda rules=None: "https://n8n")
    monkeypatch.setattr(t, "rows", lambda kind, base: [dict(r) for r in rows])
    monkeypatch.setattr(t, "insert", lambda kind, base, d: rows.append({"id": len(rows) + 1, **d}))

    def upd(kind, base, rid, d):
        for r in rows:
            if r["id"] == rid:
                r.update(d)
    monkeypatch.setattr(t, "update", upd)
    return rows


def test_event_query_default_matches_the_old_constant():
    assert aps.event_query(aps.EVENT_KEYWORDS_DEFAULT) == logic.EVENT_QUERY


def test_defaults_when_nothing_saved(table):
    assert aps.local("cliTimeoutSeconds") == 360 and aps.shared({}, "lookbackDrafting") == 48
    assert aps.shared({"lookbackDrafting": "oops"}, "lookbackDrafting") == 48          # bad value -> default
    snap = aps.snapshot()
    keys = [f["key"] for s in snap["sections"] for f in s["fields"]]
    assert set(keys) == set(aps.BY_KEY) and all(f["help"] for s in snap["sections"] for f in s["fields"])


def test_save_local_and_shared(table):
    aps.save({"cliTimeoutSeconds": 900, "lookbackDrafting": 72, "eventKeywords": ["webinar", "save the date"]})
    assert aps.local("cliTimeoutSeconds") == 900
    st = {r["key"]: r["value"] for r in table}
    assert st["lookbackDrafting"] == "72" and json.loads(st["eventKeywords"]) == ["webinar", "save the date"]
    assert aps.shared(st, "eventKeywords") == ["webinar", "save the date"]
    aps.save({"lookbackDrafting": 48})                                                  # back to default -> cleared
    assert {r["key"]: r["value"] for r in table}["lookbackDrafting"] == ""


@pytest.mark.parametrize("values,msg", [
    ({"lookbackRecap": 500}, "between"), ({"cliTimeoutSeconds": "abc"}, "number"),
    ({"ollamaHost": "localhost:11434"}, "https://"), ({"eventKeywords": []}, "at least one"),
    ({"quickLinks": [{"label": "", "url": "https://x"}]}, "name"), ({"nope": 1}, "unknown"),
])
def test_validation(table, values, msg):
    with pytest.raises((ValueError, KeyError), match=msg):
        aps.save(values)


def test_api_roundtrip_and_rules_editor(table, tmp_path, monkeypatch):
    from mailroom.main import app
    from mailroom.settings import settings
    c = TestClient(app)
    r = c.put("/api/settings/app", json={"values": {"quickLinks": [
        {"label": "Drafts", "sub": "", "icon": "mail", "url": "https://mail.google.com/mail/?authuser={account}#drafts"}]}})
    assert r.status_code == 200
    links = [f for s in r.json()["sections"] for f in s["fields"] if f["key"] == "quickLinks"][0]["value"]
    assert links[0]["label"] == "Drafts"
    assert c.put("/api/settings/app", json={"values": {"recapBatchSize": 99}}).status_code == 400
    monkeypatch.setattr(type(settings), "config_dir", property(lambda self: tmp_path))
    monkeypatch.setenv("MAILROOM_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "rules.yaml").write_text("identity: {full_name: X}\n")
    assert c.put("/api/settings/rules-yaml", json={"text": "identity: [unclosed"}).status_code == 400
    assert c.put("/api/settings/rules-yaml", json={"text": "identity: {full_name: Y}"}).json()["ok"]
    assert "Y" in (tmp_path / "rules.yaml").read_text()
    assert list((tmp_path / "data").glob("rules.yaml.bak-*"))                         # previous file kept


def test_connections_override_config_files(table):
    from mailroom.connectors import n8n as n8n_conn
    from mailroom.jobs import load_jobs
    aps.save({"n8nBaseUrl": "https://n8n.other.example", "n8nWebhookUrl": "https://n8n.other.example/webhook/abc"})
    assert n8n_conn.base_url() == "https://n8n.other.example"
    assert [j.webhook_url for j in load_jobs() if j.runner == "n8n"] == ["https://n8n.other.example/webhook/abc"]


def test_priority_rubric_default_is_unchanged_and_custom_text_reaches_prompt():
    from mailroom import app_settings as a
    from mailroom.assistant import prompts
    default = a.priority_rubric(a.PRIORITY_DEFAULTS.__getitem__)
    assert default.startswith("STEP 2 — PRIORITY: assign every email an integer priority from 1 to 5")
    assert "5 = CRITICAL — action required within roughly 24 hours" in default
    assert default.splitlines()[-1].startswith("Scoring rules: judge priority by what the email actually demands")
    st = {"priorityHigh": "anything from my bank", "priorityRules": "Mail from my accountant is never below 3.\nBe strict."}
    rub = a.priority_rubric(lambda k: a.shared(st, k))
    assert "4 = HIGH — anything from my bank" in rub
    assert rub.endswith("Scoring rules: Mail from my accountant is never below 3.\nBe strict.")
    _, user = prompts.recap_batch(1, 1, 1, ["1. Subject: x"], rub)
    assert "4 = HIGH — anything from my bank" in user and "STEP 3 — SUMMARY" in user


def test_priority_fields_validate():
    import pytest
    from mailroom import app_settings as a
    lvl, rules = a.BY_KEY["priorityLow"], a.BY_KEY["priorityRules"]
    assert a._clean(lvl, "  newsletters\n and   job posts ") == "newsletters and job posts"
    assert a._clean(rules, " line one\nline two ") == "line one\nline two"
    with pytest.raises(ValueError):
        a._clean(lvl, "   ")
    with pytest.raises(ValueError):
        a._clean(lvl, "x" * 601)
    # a stored value that is no longer valid falls back to the default
    assert a.shared({"priorityLow": "x" * 700}, "priorityLow") == a.PRIORITY_DEFAULTS["priorityLow"]
    assert a.shared({}, "priorityRules") == a.PRIORITY_DEFAULTS["priorityRules"]
