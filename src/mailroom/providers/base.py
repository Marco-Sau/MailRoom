"""Provider abstraction.

Every provider exposes the same two calls, so the pipeline is written once and
the choice of Claude / GPT / Gemini / Ollama is a config value.

Design notes / trade-offs:
- `complete_json` asks for a strict JSON object. Where the vendor supports a
  native structured-output mode we use it; otherwise we fall back to prompt
  discipline + tolerant parsing. This keeps one contract across four very
  different APIs at the cost of not exploiting each vendor's best-case schema
  enforcement.
- Calls are per-email, not batched. Latency is dominated by round-trips, so the
  pipeline fans out with a bounded worker pool instead.
"""
from __future__ import annotations

import abc
import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

JSON_BLOCK = re.compile(r"\{.*\}", re.S)
DEFAULT_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


class ProviderError(RuntimeError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


class LLMProvider(abc.ABC):
    name: str = "base"
    #: providers that bill per token; used only for reporting
    metered: bool = True

    def __init__(self, model: str, api_key: str | None = None, **kw: Any) -> None:
        self.model = model
        self.api_key = api_key
        self.options = kw
        self.usage = Usage()
        #: reasoning effort for this call (None = the model's own default); set by the registry
        self.effort: str | None = kw.get("effort")
        #: what the engine itself reported on the last call: {"model": exact id, "effort": ...}
        self.served: dict[str, str] = {}

    @abc.abstractmethod
    def complete(self, system: str, user: str, *, max_tokens: int = 1500,
                 temperature: float = 0.2) -> str:
        ...

    def complete_json(self, system: str, user: str, *, max_tokens: int = 1500) -> dict[str, Any]:
        raw = self.complete(
            system + "\n\nRespond with a single JSON object and nothing else.",
            user, max_tokens=max_tokens, temperature=0.0,
        )
        return parse_json(raw)

    def record_served(self) -> None:
        """Remember what the engine reported (exact model, effort) so Settings can show it."""
        if not self.served.get("model"):
            return
        self.served.setdefault("asked", self.model)
        if self.effort and not self.served.get("effort"):
            self.served["effort"] = self.effort
        try:
            from datetime import datetime, timezone

            from .. import model_prefs
            model_prefs.note_served(self.name, {**self.served,
                                                "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        except Exception:  # noqa: BLE001 - reporting is best-effort
            pass

    def preflight(self) -> None:
        """Cheap check run before a job starts; raise ProviderError if the engine cannot work."""
        return None

    def health(self) -> tuple[bool, str]:
        try:
            self.complete("You are a health check.", "Reply with the word ok.", max_tokens=8)
            return True, "ok"
        except Exception as exc:  # noqa: BLE001 - surfaced verbatim in the UI
            return False, str(exc)


THINK = re.compile(r"<think>.*?</think>", re.S)


def parse_json(raw: str) -> dict[str, Any]:
    raw = THINK.sub("", raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = JSON_BLOCK.search(raw)
        if not m:
            raise ProviderError(f"model did not return JSON: {raw[:300]!r}")
        return json.loads(m.group(0))
