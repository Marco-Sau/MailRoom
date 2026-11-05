"""Per-provider model choice (and per-model effort), stored in config/models.yaml.

Not a secret, so it lives beside the other config rather than in the Keychain.

Resolution order for the model a run uses:
  1. an explicit `model:` on the job in jobs.yaml  (pin a job to one model)
  2. the choice made in Settings                   (config/models.yaml)
  3. the built-in default in providers/registry.py (last resort)
"""
from __future__ import annotations

from typing import Any

import yaml

from .settings import settings

PATH = settings.config_dir / "models.yaml"


def _load() -> dict[str, Any]:
    if not PATH.exists():
        return {}
    with open(PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def all_choices() -> dict[str, str]:
    return dict(_load().get("models", {}) or {})


def get(provider: str) -> str | None:
    return all_choices().get(provider)


def set_choice(provider: str, model: str) -> dict[str, str]:
    data = _load()
    data.setdefault("models", {})[provider] = model
    PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PATH, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=True, allow_unicode=True)
    return all_choices()


def clear(provider: str) -> dict[str, str]:
    data = _load()
    data.setdefault("models", {})
    (data.get("models") or {}).pop(provider, None)
    with open(PATH, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=True, allow_unicode=True)
    return all_choices()


def _save(data: dict[str, Any]) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PATH, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=True, allow_unicode=True)


# --------------------------------------------------------------- effort, per engine and model
def get_effort(provider: str, model: str) -> str | None:
    return ((_load().get("efforts") or {}).get(provider) or {}).get(model) or None


def all_efforts(provider: str) -> dict[str, str]:
    return dict((_load().get("efforts") or {}).get(provider) or {})


def set_effort(provider: str, model: str, effort: str | None) -> None:
    data = _load()
    per = data.setdefault("efforts", {}).setdefault(provider, {})
    if effort:
        per[model] = effort
    else:
        per.pop(model, None)
    _save(data)


# ------------------------------------------- what the engine itself reported on its last call
def confirmed(provider: str) -> dict[str, str]:
    return dict((_load().get("served") or {}).get(provider) or {})


def note_served(provider: str, info: dict[str, str]) -> None:
    """Remember the exact model (and effort) the CLI reported. Written only when it changes."""
    if not info.get("model"):
        return
    data = _load()
    cur = (data.get("served") or {}).get(provider) or {}
    if {k: cur.get(k) for k in ("model", "effort", "asked")} == {k: info.get(k) for k in ("model", "effort", "asked")}:
        return
    data.setdefault("served", {})[provider] = {k: v for k, v in info.items() if v}
    _save(data)
