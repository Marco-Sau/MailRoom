"""Live model lists, straight from each vendor.

Hard-coded model IDs rot: a name that works today 404s after a deprecation.
Asking the vendor removes the guesswork, and it is also the cheapest possible
credential test - a GET that costs no tokens.

Each lister is independent and returns [] on failure with the reason attached,
so one dead provider never blanks the whole picker.
"""
from __future__ import annotations

from typing import Any

import httpx

from ..secrets_store import store

TIMEOUT = httpx.Timeout(20.0, connect=6.0)
# OpenAI lists embeddings, audio, moderation and image models too.
_OPENAI_CHAT_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt")
_OPENAI_EXCLUDE = ("embedding", "whisper", "tts", "dall-e", "moderation",
                   "realtime", "audio", "transcribe", "image")


def _anthropic() -> list[dict[str, str]]:
    key = store.require("anthropic_api_key")
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get("https://api.anthropic.com/v1/models",
                  headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                  params={"limit": 100})
    r.raise_for_status()
    return [{"id": m["id"], "label": m.get("display_name", m["id"])}
            for m in r.json().get("data", [])]


def _openai() -> list[dict[str, str]]:
    key = store.require("openai_api_key")
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get("https://api.openai.com/v1/models",
                  headers={"Authorization": f"Bearer {key}"})
    r.raise_for_status()
    ids = [m["id"] for m in r.json().get("data", [])]
    chat = [i for i in ids
            if i.startswith(_OPENAI_CHAT_PREFIXES)
            and not any(x in i for x in _OPENAI_EXCLUDE)]
    return [{"id": i, "label": i} for i in sorted(chat)]


def _gemini() -> list[dict[str, str]]:
    key = store.require("gemini_api_key")
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get("https://generativelanguage.googleapis.com/v1beta/models",
                  headers={"x-goog-api-key": key}, params={"pageSize": 200})
    r.raise_for_status()
    out = []
    for m in r.json().get("models", []):
        if "generateContent" not in (m.get("supportedGenerationMethods") or []):
            continue
        mid = m["name"].split("/", 1)[-1]
        out.append({"id": mid, "label": m.get("displayName", mid)})
    return sorted(out, key=lambda m: m["id"])


def _ollama() -> list[dict[str, str]]:
    from .ollama_provider import default_host
    DEFAULT_HOST = default_host()
    with httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0)) as c:
        r = c.get(f"{DEFAULT_HOST}/api/tags")
    r.raise_for_status()
    models = r.json().get("models", [])
    out = []
    for m in models:
        size = m.get("size")
        gb = f" ({size / 1e9:.1f} GB)" if isinstance(size, (int, float)) else ""
        out.append({"id": m["name"], "label": f"{m['name']}{gb}"})
    return sorted(out, key=lambda m: m["id"])


def _claude_cli() -> list[dict[str, str]]:
    # Aliases documented by Claude Code; each resolves to the newest model of that family.
    return [{"id": a, "label": f"{a} (latest {a.title()})"} for a in ("sonnet", "opus", "haiku")]


def _cli_default() -> list[dict[str, str]]:
    # No model listing without an API key: "default" lets the CLI pick; type an id to pin one.
    return []


LISTERS = {
    "anthropic": _anthropic,
    "openai": _openai,
    "gemini": _gemini,
    "ollama": _ollama,
    "claude_cli": _claude_cli,
    "codex_cli": _cli_default,
    "gemini_cli": _cli_default,
}


def list_for(provider: str) -> dict[str, Any]:
    fn = LISTERS.get(provider)
    if not fn:
        return {"provider": provider, "models": [], "error": "unknown provider"}
    try:
        return {"provider": provider, "models": fn(), "error": None}
    except Exception as exc:  # noqa: BLE001 - shown verbatim in the picker
        return {"provider": provider, "models": [], "error": f"{type(exc).__name__}: {exc}"[:300]}


def list_all() -> dict[str, Any]:
    return {p: list_for(p) for p in LISTERS}
