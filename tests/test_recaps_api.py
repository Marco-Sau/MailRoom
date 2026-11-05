import json

from fastapi.testclient import TestClient


def test_recaps_merge_n8n_and_local(tmp_path, monkeypatch):
    from mailroom import db
    from mailroom.api import routes_recaps as rr
    from mailroom.settings import settings
    monkeypatch.setattr(type(settings), "db_path", property(lambda self: tmp_path / "t.sqlite3"))
    db.init_db()
    items = [{"gmailId": "a", "subject": "S", "priority": 5, "replyStatus": "Drafted"},
             {"gmailId": "b", "subject": "T", "priority": 1}]
    db.save_recap("claude_cli", "Email Recap - local", "2026-10-05T14:00:00+02:00", "", 2, json.dumps(items))
    n8n_rows = [{"id": 7, "source": "n8n", "title": "Email Recap - n8n", "generatedAt": "2026-10-06T14:00:00.000+02:00",
                 "docUrl": "https://docs/x", "emailCount": 1, "itemsJson": json.dumps(items[:1])}]
    monkeypatch.setattr(rr, "_n8n_rows", lambda: (n8n_rows, None))
    from mailroom.main import app
    c = TestClient(app)
    lst = c.get("/api/recaps").json()["recaps"]
    assert [x["key"] for x in lst] == ["n8n-7", "local-1"]                  # newest first
    assert lst[1]["levels"]["5"] == 1 and lst[1]["levels"]["1"] == 1 and lst[1]["drafted"] == 1
    one = c.get("/api/recaps/local-1").json()
    assert one["engine"] == "Claude" and [i["gmailId"] for i in one["items"]] == ["a", "b"]
    assert c.get("/api/recaps/n8n-7").json()["docUrl"] == "https://docs/x"
    assert c.get("/api/recaps/n8n-99").status_code == 404
