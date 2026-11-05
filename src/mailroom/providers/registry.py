from __future__ import annotations

from .. import model_prefs
from ..secrets_store import store
from .anthropic_provider import AnthropicProvider
from .base import LLMProvider
from .cli_providers import AntigravityCLIProvider, ClaudeCLIProvider, CodexCLIProvider
from .gemini_provider import GeminiProvider
from .ollama_provider import OllamaProvider
from .openai_provider import OpenAIProvider

PROVIDERS: dict[str, tuple[type[LLMProvider], str | None, str]] = {
    # name -> (class, secret key, default model)
    "anthropic": (AnthropicProvider, "anthropic_api_key", "claude-sonnet-4-5"),
    "openai": (OpenAIProvider, "openai_api_key", "gpt-5"),
    "gemini": (GeminiProvider, "gemini_api_key", "gemini-3.1-pro-preview"),   # 2.5 Pro: closed to new API users
    "ollama": (OllamaProvider, None, "qwen3:32b"),
    # Command-line tools on your subscriptions. "default" = the CLI's own choice.
    "claude_cli": (ClaudeCLIProvider, None, "claude-sonnet-5-5"),
    "codex_cli": (CodexCLIProvider, None, "gpt-6.1-sol"),
    "gemini_cli": (AntigravityCLIProvider, None, "default"),   # Gemini via Antigravity CLI
}
CLI_PROVIDERS = ("claude_cli", "codex_cli", "gemini_cli")


def resolve_model(name: str, model: str | None = None) -> str:
    """job pin -> Settings choice -> built-in default."""
    _, _, default_model = PROVIDERS[name]
    return model or model_prefs.get(name) or default_model


def resolve_effort(name: str, model: str, effort: str | None = None) -> str | None:
    """Explicit effort -> the one saved in Settings for this engine and model -> the model's own default."""
    return effort or model_prefs.get_effort(name, model)


def get_provider(name: str, model: str | None = None, effort: str | None = None, **kw) -> LLMProvider:
    if name not in PROVIDERS:
        raise KeyError(f"unknown provider '{name}'. Known: {', '.join(PROVIDERS)}")
    cls, secret_key, _ = PROVIDERS[name]
    api_key = store.get(secret_key) if secret_key else None
    m = resolve_model(name, model)
    p = cls(m, api_key=api_key, **kw)
    p.effort = resolve_effort(name, m, effort)
    return p
