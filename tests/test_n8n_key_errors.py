"""401/403 from the n8n REST API are explained, and a rejected key after the webhook was accepted
says that n8n is running the job anyway."""
import httpx

from mailroom.connectors import n8n as n8n_conn


class _Store:
    def __init__(self, key):
        self.key = key

    def get(self, name):
        return self.key


def _resp(code, body):
    return httpx.Response(code, json=body, request=httpx.Request("GET", "https://n8n/api/v1/executions/1"))


def test_401_means_invalid_or_expired(monkeypatch):
    monkeypatch.setattr(n8n_conn, "store", _Store("k"))
    e = n8n_conn.key_rejected(_resp(401, {"message": "unauthorized"}), "reading execution 1561")
    assert "HTTP 401: unauthorized" in str(e) and "expired" in str(e) and "Settings → Advanced → Secrets" in str(e)


def test_403_means_missing_scope(monkeypatch):
    monkeypatch.setattr(n8n_conn, "store", _Store("k"))
    e = n8n_conn.key_rejected(_resp(403, {"message": "Forbidden"}), "reading execution 1561")
    assert "HTTP 403" in str(e) and "scopes" in str(e)


def test_no_key_saved(monkeypatch):
    monkeypatch.setattr(n8n_conn, "store", _Store(None))
    assert "no n8n API key is saved" in str(n8n_conn.key_rejected(_resp(401, {}), "x"))


def test_follow_reports_run_continues_in_n8n(monkeypatch):
    from mailroom.runners import n8n_runner as nr

    def rejected(base, eid, full=False):
        raise PermissionError("n8n rejected the API key while reading execution 1561 (HTTP 401: unauthorized)")
    monkeypatch.setattr(nr.tracker, "fetch_execution", rejected)
    status, stats, body, err = nr.N8nRunner()._follow(1, "https://n8n", "1561", ["HTTP 202"])
    assert status == "failed" and stats["execution_id"] == "1561" and "API key rejected" in err
    assert any("runs it on its own" in line for line in body) and any("HTTP 401" in line for line in body)
