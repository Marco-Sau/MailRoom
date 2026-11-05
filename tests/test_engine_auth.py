"""Settings → Engine sign-in: status without tokens, Terminal sign-in, verification with one tiny prompt."""
import subprocess

from fastapi.testclient import TestClient

from mailroom import engine_auth as ea
from mailroom.providers import cli_providers as cp
from mailroom.providers.base import ProviderError


class Fake:
    binary = "claude"
    model = "claude-sonnet-5-5"

    def __init__(self, signed_in=True, works=True):
        self.signed_in, self.works, self.served = signed_in, works, {}

    def login_status(self):
        if not self.signed_in:
            raise ProviderError("Claude: not signed in (OAuth session expired)")
        return "/x/claude · signed in (claude.ai)"

    def health(self):
        if self.works:
            self.served = {"model": "claude-sonnet-5-5"}
            return True, "ok"
        return False, "Claude: not signed in (OAuth session expired and could not be refreshed)"


def test_status_states(monkeypatch):
    monkeypatch.setattr(ea, "get_provider", lambda eid: Fake())
    monkeypatch.setattr(ea, "find_binary", lambda b: None)
    assert ea.status("claude_cli")["state"] == "missing"
    monkeypatch.setattr(ea, "find_binary", lambda b: "/x/claude")
    assert ea.status("claude_cli")["state"] == "ok"
    monkeypatch.setattr(ea, "get_provider", lambda eid: Fake(signed_in=False))
    st = ea.status("claude_cli")
    assert st["state"] == "signed_out" and "expired" in st["detail"]


def test_verify_reports_model_or_error(monkeypatch):
    monkeypatch.setattr(ea, "find_binary", lambda b: "/x/claude")
    monkeypatch.setattr(ea, "get_provider", lambda eid: Fake())
    v = ea.verify("claude_cli")
    assert v["ok"] and "claude-sonnet-5-5" in v["detail"]
    monkeypatch.setattr(ea, "get_provider", lambda eid: Fake(works=False))
    v = ea.verify("claude_cli")
    assert not v["ok"] and v["state"] == "error" and "could not be refreshed" in v["detail"]


def test_signin_opens_terminal_with_the_cli_command(monkeypatch, tmp_path):
    monkeypatch.setenv("MAILROOM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ea, "get_provider", lambda eid: Fake())
    monkeypatch.setattr(ea, "find_binary", lambda b: "/Users/x/.local/bin/claude")
    monkeypatch.setattr(ea.sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(ea.subprocess, "run", lambda a, **k: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
    r = ea.open_signin("claude_cli")
    assert r["command"] == "/Users/x/.local/bin/claude auth login"
    assert calls[0][:3] == ["open", "-a", "Terminal"]
    script = open(calls[0][3]).read()
    assert "claude auth login" in script and "press Verify" in script


def test_unknown_engine_404():
    from mailroom.main import app
    c = TestClient(app)
    assert c.get("/api/settings/engine-auth/ollama").status_code == 404
    assert [e["id"] for e in c.get("/api/settings/engine-auth").json()] == ["claude_cli", "codex_cli", "gemini_cli"]


def test_gemini_engine_is_antigravity():
    from mailroom.providers import get_provider
    g = get_provider("gemini_cli")
    assert g.binary == "agy" and "antigravity" in ea.ENGINES["gemini_cli"]["install"].lower()
