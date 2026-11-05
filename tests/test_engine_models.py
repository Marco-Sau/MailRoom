"""Settings → Models: one row per Run engine; n8n's choice goes to the n8n Settings table."""
import pytest
from fastapi.testclient import TestClient

from mailroom import model_prefs
from mailroom.connectors import n8n_tables
from mailroom.providers import catalog, engine_models as em


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(model_prefs, "PATH", tmp_path / "models.yaml")
    from mailroom.providers import ollama_provider as _op
    monkeypatch.setattr(_op.OllamaProvider, "show", lambda self, m: {"capabilities": []})
    monkeypatch.setattr(catalog, "_ollama", lambda: [{"id": "qwen3:32b", "label": "qwen3:32b (20.2 GB)"},
                                                     {"id": "nomic-embed-text:v1.5", "label": "nomic"},
                                                     {"id": "mistral-large-3:675b-cloud", "label": "m"}])
    table = [{"id": 1, "key": "addressQuery", "value": "x"}]
    calls = []
    monkeypatch.setattr(n8n_tables, "_base", lambda rules=None: "https://n8n")
    monkeypatch.setattr(n8n_tables, "rows", lambda kind, base: [dict(r) for r in table])
    monkeypatch.setattr(n8n_tables, "insert", lambda kind, base, d: calls.append(("insert", kind, d)) or
                        table.append({"id": 2, **d}))
    monkeypatch.setattr(n8n_tables, "update", lambda kind, base, rid, d: calls.append(("update", kind, rid, d)))
    return calls


def row(rows, eid):
    return next(r for r in rows if r["id"] == eid)


def test_rows_cover_the_five_engines(env):
    rows = em.engine_rows()
    assert [r["id"] for r in rows] == ["claude_cli", "codex_cli", "gemini_cli", "ollama", "n8n"]
    ol = row(rows, "ollama")
    assert ol["effective"] == "qwen3:32b" and ol["live"]
    assert [m["id"] for m in ol["models"]] == ["qwen3:32b", "mistral-large-3:675b-cloud"]   # no embedding model
    assert "ollama.com" in ol["models"][1]["note"]
    assert row(rows, "n8n")["testable"] is False and row(rows, "claude_cli")["asOf"] == em.AS_OF


def test_cli_choice_saved_and_reset(env):
    rows = em.set_choice("codex_cli", "gpt-6-luna", "high")
    r = row(rows, "codex_cli")
    assert r["effective"] == "gpt-6-luna" and r["effort"] == "high"
    rows = em.set_choice("codex_cli", "gpt-6-astra")                       # another model keeps its own effort
    assert row(rows, "codex_cli")["effort"] is None
    assert em.effective_for_job("assistant", "codex_cli", "gpt-6-luna") == "gpt-6-luna · high"
    rows = em.set_choice("codex_cli", "")
    assert row(rows, "codex_cli")["chosen"] is None and row(rows, "codex_cli")["effective"] == "gpt-6.1-sol"


def test_lists_are_exact_ids_with_efforts(env):
    claude = {m["id"]: m for m in em.CURATED["claude_cli"]["models"]}
    assert "sonnet" not in claude and claude["claude-sonnet-5-5"]["defaultEffort"] == "medium"
    assert claude["claude-sonnet-4-6"]["efforts"] == ["low", "medium", "high", "max"]
    assert claude["claude-haiku-4-5"]["efforts"] == []
    for eng in ("claude_cli", "codex_cli", "gemini_cli"):                # "Default" is the only default entry
        assert "default" not in {m["id"] for m in em.CURATED[eng]["models"]}
    assert "gpt-5.5" not in {m["id"] for m in em.CURATED["codex_cli"]["models"]}   # retired 14 Oct 2026
    assert "ultra" not in {m["id"]: m for m in em.CURATED["codex_cli"]["models"]}["gpt-6-luna"]["efforts"]


def test_ollama_thinking_choices(env, monkeypatch):
    from mailroom.providers import ollama_provider as op
    caps = {"qwen3:32b": ["completion", "thinking"], "mistral-large-3:675b-cloud": ["completion"]}
    monkeypatch.setattr(op.OllamaProvider, "show", lambda self, m: {"capabilities": caps.get(m, [])})
    monkeypatch.setattr(catalog, "_ollama", lambda: [{"id": k, "label": k} for k in
                                                     ("qwen3:32b", "gpt-oss:20b", "mistral-large-3:675b-cloud")])
    ol = {m["id"]: m for m in row(em.engine_rows(), "ollama")["models"]}
    assert ol["qwen3:32b"]["efforts"] == ["off", "on"] and ol["gpt-oss:20b"]["efforts"] == ["low", "medium", "high"]
    assert ol["mistral-large-3:675b-cloud"]["efforts"] == []


def test_n8n_choice_goes_to_settings_table(env):
    em.set_choice("n8n", "claude-sonnet-5-5", "high")
    assert ("insert", "settings", {"key": "n8nModel", "value": "claude-sonnet-5-5"}) in env
    assert ("insert", "settings", {"key": "n8nEffort", "value": "high"}) in env
    assert em.effective_for_job("n8n", None, None) == "claude-sonnet-5-5 · high"
    with pytest.raises(ValueError):
        em.set_choice("n8n", "claude-sonnet-4-6", "high")   # temperature-bound model: no effort in n8n
    em.set_choice("n8n", "")                                # reset clears the effort too
    assert ("update", "settings", 2, {"value": ""}) in env
    assert em.effective_for_job("n8n", None, None) == "workflow defaults"


def test_bad_ids_rejected(env):
    with pytest.raises(ValueError):
        em.set_choice("claude_cli", "sonnet; rm -rf /")
    with pytest.raises(KeyError):
        em.set_choice("nope", "x")


def test_api_and_jobs_show_effective_model(env, monkeypatch):
    from mailroom.main import create_app
    c = TestClient(create_app())
    assert c.put("/api/settings/engine-models/ollama", json={"model": "qwen3:32b"}).status_code == 200
    assert c.put("/api/settings/engine-models/claude_cli", json={"model": "bad id!"}).status_code == 400
    eng = c.get("/api/settings/engine-models").json()["engines"]
    assert row(eng, "ollama")["chosen"] == "qwen3:32b"
    jobs = {j["job_id"]: j for j in c.get("/api/jobs").json()}
    assert all("effective_model" in j for j in jobs.values())
