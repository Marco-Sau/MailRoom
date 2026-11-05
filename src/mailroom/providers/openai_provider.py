from __future__ import annotations

import httpx

from .base import DEFAULT_TIMEOUT, LLMProvider, ProviderError

API = "https://api.openai.com/v1/chat/completions"


class OpenAIProvider(LLMProvider):
    """Chat Completions.

    OpenAI's parameter surface differs by model family and the API only tells
    you at call time: reasoning models reject a non-default `temperature`, and
    older models reject `max_completion_tokens` in favour of `max_tokens`.
    Rather than maintain a model->capability table that goes stale with every
    release, the provider retries once on the specific 400 and remembers the
    answer for the rest of the process. Cost: one wasted round trip per process,
    once.
    """

    name = "openai"

    def __init__(self, model: str, api_key: str | None = None, **kw) -> None:
        super().__init__(model, api_key, **kw)
        self._omit_temperature = bool(kw.get("omit_temperature"))
        self._legacy_max_tokens = bool(kw.get("legacy_max_tokens"))

    def _payload(self, system: str, user: str, max_tokens: int,
                 temperature: float) -> dict:
        p: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        p["max_tokens" if self._legacy_max_tokens else "max_completion_tokens"] = max_tokens
        if not self._omit_temperature:
            p["temperature"] = temperature
        return p

    def complete(self, system: str, user: str, *, max_tokens: int = 1500,
                 temperature: float = 0.2) -> str:
        if not self.api_key:
            raise ProviderError("openai_api_key is not set")
        headers = {"Authorization": f"Bearer {self.api_key}"}

        with httpx.Client(timeout=DEFAULT_TIMEOUT) as c:
            r = c.post(API, json=self._payload(system, user, max_tokens, temperature),
                       headers=headers)

            if r.status_code == 400:
                body = r.text.lower()
                adjusted = False
                if "temperature" in body and not self._omit_temperature:
                    self._omit_temperature = True
                    adjusted = True
                if "max_completion_tokens" in body and not self._legacy_max_tokens:
                    self._legacy_max_tokens = True
                    adjusted = True
                if adjusted:
                    r = c.post(API,
                               json=self._payload(system, user, max_tokens, temperature),
                               headers=headers)

        if r.status_code >= 400:
            raise ProviderError(f"openai {r.status_code}: {r.text[:400]}")
        data = r.json()
        u = data.get("usage", {})
        self.usage.input_tokens += u.get("prompt_tokens", 0)
        self.usage.output_tokens += u.get("completion_tokens", 0)
        return data["choices"][0]["message"]["content"] or ""
