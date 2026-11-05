from __future__ import annotations

import httpx

from .base import DEFAULT_TIMEOUT, LLMProvider, ProviderError

BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiProvider(LLMProvider):
    name = "gemini"

    def complete(self, system: str, user: str, *, max_tokens: int = 1500,
                 temperature: float = 0.2) -> str:
        if not self.api_key:
            raise ProviderError("gemini_api_key is not set")
        url = f"{BASE}/{self.model}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as c:
            r = c.post(url, json=payload, headers={"x-goog-api-key": self.api_key})
        if r.status_code >= 400:
            raise ProviderError(f"gemini {r.status_code}: {r.text[:400]}")
        data = r.json()
        u = data.get("usageMetadata", {})
        self.usage.input_tokens += u.get("promptTokenCount", 0)
        self.usage.output_tokens += u.get("candidatesTokenCount", 0)
        cands = data.get("candidates") or []
        if not cands:
            raise ProviderError(f"gemini returned no candidates: {str(data)[:300]}")
        parts = cands[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)
