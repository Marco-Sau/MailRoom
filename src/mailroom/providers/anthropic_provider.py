from __future__ import annotations

import httpx

from .base import DEFAULT_TIMEOUT, LLMProvider, ProviderError

API = "https://api.anthropic.com/v1/messages"
VERSION = "2023-06-01"


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def complete(self, system: str, user: str, *, max_tokens: int = 1500,
                 temperature: float = 0.2) -> str:
        if not self.api_key:
            raise ProviderError("anthropic_api_key is not set")
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as c:
            r = c.post(API, json=payload, headers={
                "x-api-key": self.api_key,
                "anthropic-version": VERSION,
                "content-type": "application/json",
            })
        if r.status_code >= 400:
            raise ProviderError(f"anthropic {r.status_code}: {r.text[:400]}")
        data = r.json()
        u = data.get("usage", {})
        self.usage.input_tokens += u.get("input_tokens", 0)
        self.usage.output_tokens += u.get("output_tokens", 0)
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return "".join(parts)
