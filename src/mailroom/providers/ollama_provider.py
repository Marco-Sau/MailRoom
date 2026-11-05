from __future__ import annotations

import httpx

from .base import LLMProvider, ProviderError, parse_json

DEFAULT_HOST = "http://127.0.0.1:11434"
# Local models on a laptop are slow but free; give them a long leash.
LOCAL_TIMEOUT = httpx.Timeout(600.0, connect=5.0)


def _local(key: str, fallback):
    try:
        from .. import app_settings
        return app_settings.local(key)        # Settings → Connections / Engine timeouts
    except Exception:  # noqa: BLE001
        return fallback


def default_host() -> str:
    return str(_local("ollamaHost", DEFAULT_HOST)).rstrip("/")


class OllamaProvider(LLMProvider):
    """Local inference. No data leaves the machine - the right choice for
    sensitive mail. Cost is zero; latency and quality are the trade-off."""

    name = "ollama"
    metered = False

    def __init__(self, model: str, api_key: str | None = None, **kw) -> None:
        super().__init__(model, api_key, **kw)
        self.host = (kw.get("host") or default_host()).rstrip("/")

    #: Ollama's default context window (2-4k tokens) silently truncates a 10-email recap batch
    #: plus its answer; 8k fits every prompt MailRoom sends with room for the reply.
    NUM_CTX = 8192

    def complete(self, system: str, user: str, *, max_tokens: int = 1500,
                 temperature: float = 0.2) -> str:
        return self._chat(system, user, max_tokens=max_tokens, temperature=temperature)

    def complete_json(self, system: str, user: str, *, max_tokens: int = 1500) -> dict:
        raw = self._chat(system + "\n\nRespond with a single JSON object and nothing else.", user,
                         max_tokens=max_tokens, temperature=0.0, fmt="json")
        return parse_json(raw)

    def _chat(self, system: str, user: str, *, max_tokens: int, temperature: float,
              fmt: str | None = None) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            # Reasoning models (qwen3, deepseek-r1, gpt-oss) would otherwise spend the whole
            # token budget thinking before the answer starts. Settings → Models can turn it on
            # ("on", or "low"/"medium"/"high" for gpt-oss).
            "think": self._think(),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": int(_local("ollamaContext", self.NUM_CTX))},
        }
        if fmt:
            payload["format"] = fmt
        r = self._post(payload)
        if r.status_code == 400 and "think" in r.text.lower():
            payload.pop("think", None)            # older Ollama, or a model without a thinking switch
            r = self._post(payload)
        if r.status_code == 404 and "not found" in r.text.lower():
            raise ProviderError(self._missing_message())
        if r.status_code >= 400:
            raise ProviderError(f"ollama {r.status_code}: {r.text[:400]}")
        data = r.json()
        self.served = {"model": data.get("model") or self.model,
                       "effort": "off" if payload.get("think") is False else str(payload.get("think", ""))}
        self.record_served()
        self.usage.input_tokens += data.get("prompt_eval_count", 0)
        self.usage.output_tokens += data.get("eval_count", 0)
        return data.get("message", {}).get("content", "")

    def _think(self) -> bool | str:
        e = (self.effort or "").lower()
        if e in ("low", "medium", "high"):
            return e
        return e == "on"

    def _post(self, payload: dict) -> httpx.Response:
        try:
            with httpx.Client(timeout=httpx.Timeout(float(_local("ollamaTimeoutSeconds", 600)), connect=5.0)) as c:
                return c.post(f"{self.host}/api/chat", json=payload)
        except httpx.ConnectError as exc:
            raise ProviderError(f"Ollama is not reachable at {self.host}. Start the Ollama app "
                                f"(or `ollama serve`).") from exc

    def _missing_message(self, have: list[str] | None = None) -> str:
        if have is None:
            try:
                have = self.list_models()
            except Exception:  # noqa: BLE001
                have = []
        return (f"Ollama model {self.model!r} is not installed on this Mac. "
                + (f"Installed: {', '.join(have)}. " if have else "No models are installed. ")
                + f"Pick one in Settings → Models, or run `ollama pull {self.model}`.")

    def preflight(self) -> None:
        """Fail the run at once, before any Gmail work, if the chosen model is missing."""
        try:
            have = self.list_models()
        except httpx.HTTPError as exc:
            raise ProviderError(f"Ollama is not reachable at {self.host}. Start the Ollama app "
                                f"(or `ollama serve`).") from exc
        if self.model not in have and f"{self.model}:latest" not in have:
            raise ProviderError(self._missing_message(have))

    def show(self, model: str) -> dict:
        """Model details from Ollama (capabilities tell whether it can think)."""
        with httpx.Client(timeout=10.0) as c:
            r = c.post(f"{self.host}/api/show", json={"model": model})
        r.raise_for_status()
        return r.json()

    def list_models(self) -> list[str]:
        with httpx.Client(timeout=10.0) as c:
            r = c.get(f"{self.host}/api/tags")
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
