"""The 400-then-retry path is the only place the OpenAI provider mutates its own
config, so it needs a test: a silent failure here degrades every classify call."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mailroom.providers import openai_provider as mod

TEMP_400 = json.dumps({"error": {
    "message": "Unsupported value: 'temperature' does not support 0.2 with this model. "
               "Only the default (1) value is supported.",
    "type": "invalid_request_error", "param": "temperature",
    "code": "unsupported_value"}})
MAXTOK_400 = json.dumps({"error": {
    "message": "Unrecognized request argument supplied: max_completion_tokens",
    "type": "invalid_request_error"}})
OK = {"choices": [{"message": {"content": "hi"}}], "usage": {}}


class FakeClient:
    """Replays a scripted list of responses and records every payload sent."""

    def __init__(self, script, sent):
        self.script, self.sent = script, sent

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, headers=None):
        self.sent.append(json)
        status, body = self.script.pop(0)
        return SimpleNamespace(
            status_code=status, text=body if isinstance(body, str) else "",
            json=lambda: body if isinstance(body, dict) else {},
        )


@pytest.fixture
def patched(monkeypatch):
    def install(script, sent):
        monkeypatch.setattr(mod.httpx, "Client",
                            lambda **kw: FakeClient(script, sent))
    return install


def test_retries_without_temperature(patched):
    sent: list[dict] = []
    patched([(400, TEMP_400), (200, OK)], sent)
    p = mod.OpenAIProvider("gpt-5", api_key="k")
    assert p.complete("s", "u") == "hi"
    assert "temperature" in sent[0]
    assert "temperature" not in sent[1]
    assert p._omit_temperature is True


def test_flag_persists_so_the_retry_happens_once(patched):
    sent: list[dict] = []
    patched([(400, TEMP_400), (200, OK), (200, OK)], sent)
    p = mod.OpenAIProvider("gpt-5", api_key="k")
    p.complete("s", "u")
    p.complete("s", "u")
    assert len(sent) == 3                      # 2 for the first call, 1 for the second
    assert "temperature" not in sent[2]


def test_falls_back_to_legacy_max_tokens(patched):
    sent: list[dict] = []
    patched([(400, MAXTOK_400), (200, OK)], sent)
    p = mod.OpenAIProvider("gpt-4o-mini", api_key="k")
    p.complete("s", "u")
    assert "max_completion_tokens" in sent[0]
    assert "max_tokens" in sent[1] and "max_completion_tokens" not in sent[1]


def test_unrelated_400_is_not_retried(patched):
    sent: list[dict] = []
    patched([(400, '{"error": {"message": "model not found"}}')], sent)
    p = mod.OpenAIProvider("nope", api_key="k")
    with pytest.raises(mod.ProviderError, match="400"):
        p.complete("s", "u")
    assert len(sent) == 1


def test_missing_key_fails_before_any_request(patched):
    sent: list[dict] = []
    patched([], sent)
    with pytest.raises(mod.ProviderError, match="openai_api_key"):
        mod.OpenAIProvider("gpt-5", api_key=None).complete("s", "u")
    assert sent == []
