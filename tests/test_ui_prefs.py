from fastapi.testclient import TestClient


def test_theme_and_lookback_roundtrip(tmp_path, monkeypatch):
    from mailroom.api import routes_settings as rs
    monkeypatch.setattr(rs, "_ui_path", lambda: tmp_path / "ui.json")
    from mailroom.main import app
    c = TestClient(app)
    assert c.get("/api/settings/ui").json() == {"theme": "auto", "lookbackHours": 0, "showAdvanced": False}
    assert c.put("/api/settings/ui", json={"theme": "light"}).json()["theme"] == "light"
    assert c.put("/api/settings/ui", json={"lookbackHours": 72}).json() == {"theme": "light", "lookbackHours": 72, "showAdvanced": False}
    assert c.get("/api/settings/ui").json() == {"theme": "light", "lookbackHours": 72, "showAdvanced": False}
    assert c.put("/api/settings/ui", json={"theme": "neon"}).status_code == 400
    assert c.put("/api/settings/ui", json={"lookbackHours": 200}).status_code == 400


def test_show_advanced_is_remembered(tmp_path, monkeypatch):
    from mailroom.api import routes_settings as rs
    monkeypatch.setattr(rs, "_ui_path", lambda: tmp_path / "ui.json")
    from mailroom.main import app
    c = TestClient(app)
    assert c.put("/api/settings/ui", json={"showAdvanced": True}).json()["showAdvanced"] is True
    assert c.get("/api/settings/ui").json()["showAdvanced"] is True
    assert c.put("/api/settings/ui", json={"theme": "dark"}).json()["showAdvanced"] is True
    assert c.put("/api/settings/ui", json={"showAdvanced": False}).json()["showAdvanced"] is False
