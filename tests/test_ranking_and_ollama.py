"""Recap ranking robustness, model-failure status, and the Ollama provider's guards."""
import json

import pytest

from mailroom.assistant import logic
from mailroom.models import RunSpec
from mailroom.providers.base import ProviderError
from mailroom.providers.ollama_provider import OllamaProvider
from mailroom.runners import assistant_runner as ar

from tests.test_assistant_runner import FakeModel, world  # noqa: F401 - pytest fixture


def m(subject):
    return {"id": subject, "subject": subject}


def test_match_ranked_by_subject_when_one_is_skipped():
    batch = [m("A"), m("B"), m("C")]
    got = [{"subject": "a", "priority": 5}, {"subject": "C", "priority": 1}]     # B dropped
    out = logic.match_ranked(batch, got)
    assert out[0]["priority"] == 5 and out[1] is None and out[2]["priority"] == 1


def test_match_ranked_positional_only_when_counts_line_up():
    batch = [m("A"), m("B")]
    assert [e["n"] for e in logic.match_ranked(batch, [{"subject": "x", "n": 1}, {"subject": "y", "n": 2}])] == [1, 2]
    assert logic.match_ranked(batch, [{"subject": "x", "n": 1}]) == [None, None]


def _recap_spec():
    return RunSpec(job_id="x", name="x", runner="assistant", provider="ollama", steps=["recap"])


def test_recap_asks_again_for_skipped_emails(world):
    model = FakeModel()
    calls = []

    def complete(system, user, *, max_tokens=1500, temperature=0.2):
        n = user.count("Subject: ")
        calls.append(n)
        subjects = [line.split("Subject: ", 1)[1] for line in user.splitlines() if "Subject: " in line]
        keep = subjects[: max(1, n // 2)] if len(calls) == 1 else subjects       # first answer drops half
        return json.dumps({"emails": [{"subject": s, "priority": 2, "topic": "T", "summary": "ok"} for s in keep]})
    model.complete = complete
    res = ar.AssistantRunner(lambda n, mdl: model).run(_recap_spec())
    assert res.status == "succeeded", res.log
    items = json.loads([d for k, d in world["inserts"] if k == "recaps"][0]["itemsJson"])
    assert len(items) == 12 and not [i for i in items if i["summary"].startswith("(not ranked")]
    assert calls[:2] == [10, 5]                                                   # retry only the 5 missing


def test_every_model_call_failing_fails_the_run(world):
    model = FakeModel()

    def boom(*a, **k):
        raise ProviderError("Ollama model 'qwen3:14b' is not installed on this Mac.")
    model.complete = boom
    res = ar.AssistantRunner(lambda n, mdl: model).run(_recap_spec())
    assert res.status == "failed" and "every call" in res.error
    items = json.loads([d for k, d in world["inserts"] if k == "recaps"][0]["itemsJson"])   # recap still saved
    assert all(i["summary"] == "(not ranked: model error, see the run log)" for i in items)


def test_preflight_stops_before_any_gmail_work(world):
    model = FakeModel()

    def missing():
        raise ProviderError("Ollama model 'qwen3:14b' is not installed on this Mac.")
    model.preflight = missing
    res = ar.AssistantRunner(lambda n, mdl: model).run(_recap_spec())
    assert res.status == "failed" and "not installed" in res.error
    assert not world["searches"] and not model.prompts


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._body = code, body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return self._body


def test_ollama_sends_think_false_json_format_and_context(monkeypatch):
    p = OllamaProvider("qwen3:32b")
    sent = []
    monkeypatch.setattr(p, "_post", lambda payload: sent.append(json.loads(json.dumps(payload))) or
                        _Resp(200, {"message": {"content": '{"ok": 1}'}}))
    assert p.complete_json("sys", "user") == {"ok": 1}
    assert sent[0]["think"] is False and sent[0]["format"] == "json"
    assert sent[0]["options"]["num_ctx"] == OllamaProvider.NUM_CTX


def test_ollama_retries_without_think_on_old_servers(monkeypatch):
    p = OllamaProvider("llama3:latest")
    sent = []

    def post(payload):
        sent.append(dict(payload))
        if "think" in payload:
            return _Resp(400, '{"error":"unknown field think"}')
        return _Resp(200, {"message": {"content": "hi"}})
    monkeypatch.setattr(p, "_post", post)
    assert p.complete("s", "u") == "hi" and "think" not in sent[-1]


def test_ollama_preflight_names_installed_models(monkeypatch):
    p = OllamaProvider("qwen3:14b")
    monkeypatch.setattr(p, "list_models", lambda: ["qwen3:32b", "gpt-oss:20b"])
    with pytest.raises(ProviderError, match="Installed: qwen3:32b, gpt-oss:20b"):
        p.preflight()
    ok = OllamaProvider("llama3")
    monkeypatch.setattr(ok, "list_models", lambda: ["llama3:latest"])
    ok.preflight()                                                               # ':latest' tag is implied
