"""Settings → Models: the exact models each Run engine can use, the effort for each, and the choice.

The CLIs (Claude Code, Codex, Gemini CLI) have no "list models" command on a subscription
sign-in, so their lists are the exact IDs and effort levels each vendor documents, checked on
AS_OF (sources below). "Custom…" allows a newer ID. After every call the app also records the
exact model the CLI itself reports (Claude: modelUsage, Gemini: stats.models, Codex: its header),
shown as "reported by the CLI".

Ollama's list is live (installed models; thinking support from /api/show). n8n's list is what the
Anthropic credential in n8n returned on AS_OF; model and effort are stored in the n8n Settings
table (keys n8nModel, n8nEffort), where the Drafting, Events and Recap workflows read them.
"""
from __future__ import annotations

import re
from typing import Any

from .. import model_prefs
from .registry import PROVIDERS, resolve_effort, resolve_model

AS_OF = "2026-10-27"
N8N_KEY, N8N_EFFORT_KEY = "n8nModel", "n8nEffort"
N8N_DEFAULTS = "workflow defaults (Drafting and Events: claude-sonnet-4-6, Recap: claude-haiku-4-5-20251001)"

E5 = ["low", "medium", "high", "xhigh", "max"]          # Claude Code: Fable, Opus/Sonnet 5.x, Opus 4.8/4.7
E4 = ["low", "medium", "high", "max"]                   # Claude Code: Opus 4.6, Sonnet 4.6
CODEX_ALL = ["low", "medium", "high", "xhigh", "max"]   # "ultra" runs sub-agents: not for one email


def _m(mid: str, label: str, note: str = "", efforts: list[str] | None = None,
       default_effort: str = "") -> dict[str, Any]:
    return {"id": mid, "label": label, "note": note, "efforts": list(efforts or []),
            "defaultEffort": default_effort}


CURATED: dict[str, dict[str, Any]] = {
    "claude_cli": {
        "source": "https://code.claude.com/docs/en/model-config",
        "models": [
            _m("claude-sonnet-5-5", "Claude Sonnet 5.5", "everyday work; what the 'sonnet' alias points to", E5, "medium"),
            _m("claude-opus-5-5", "Claude Opus 5.5", "strongest Opus; uses your plan's limits faster", E5, "medium"),
            _m("claude-haiku-5-5", "Claude Haiku 5.5", "fastest and lightest; what the 'haiku' alias points to"),
            _m("claude-fable-5-1", "Claude Fable 5.1", "only where your plan includes Fable; in this mode Claude Code "
               "never asks before billing usage credits", E5, "high"),
            _m("claude-fable-5", "Claude Fable 5", "only where your plan includes Fable", E5, "high"),
            _m("claude-opus-5", "Claude Opus 5", "", E5, "high"),
            _m("claude-sonnet-5", "Claude Sonnet 5", "", E5, "high"),
            _m("claude-opus-4-8", "Claude Opus 4.8", "", E5, "high"),
            _m("claude-sonnet-4-6", "Claude Sonnet 4.6", "", E4, "high"),
            _m("claude-haiku-4-5", "Claude Haiku 4.5", "no effort setting"),
        ],
    },
    "codex_cli": {
        "source": "https://learn.chatgpt.com/docs/models",
        "models": [
            _m("gpt-6.1-sol", "GPT-6.1 Sol", "near-Astra quality at lower cost; OpenAI's pick for repeated work",
               CODEX_ALL, ""),
            _m("gpt-6-astra", "GPT-6 Astra", "most capable; the hardest work", CODEX_ALL, ""),
            _m("gpt-6.1-terra", "GPT-6.1 Terra", "fast and efficient", CODEX_ALL, ""),
            _m("gpt-6-luna", "GPT-6 Luna", "most efficient; for high-volume tasks", CODEX_ALL, ""),
        ],
    },
    "gemini_cli": {
        # Fallback only: the row lists `agy models` live when Antigravity is signed in.
        "source": "https://antigravity.google/docs/cli/headless/",
        "models": [
            _m("gemini-3.8-flash-medium", "Gemini 3.8 Flash · medium thinking"),
            _m("gemini-3.8-flash-high", "Gemini 3.8 Flash · high thinking"),
            _m("gemini-3.8-flash-low", "Gemini 3.8 Flash · low thinking"),
        ],
    },
    "n8n": {
        "source": "Anthropic credential in n8n (model list from the Anthropic API)",
        "models": [
            # Effort is offered only where the n8n node ignores temperature (Opus 4.7+, Sonnet 5+, Fable),
            # so turning thinking on never clashes with the workflows' temperature setting.
            _m("claude-sonnet-5-5", "Claude Sonnet 5.5", "", ["low", "medium", "high"]),
            _m("claude-opus-5-5", "Claude Opus 5.5", "", ["low", "medium", "high"]),
            _m("claude-fable-5-1", "Claude Fable 5.1", "", ["low", "medium", "high"]),
            _m("claude-opus-5", "Claude Opus 5", "", ["low", "medium", "high"]),
            _m("claude-sonnet-5", "Claude Sonnet 5", "", ["low", "medium", "high"]),
            _m("claude-fable-5", "Claude Fable 5", "", ["low", "medium", "high"]),
            _m("claude-opus-4-8", "Claude Opus 4.8", "", ["low", "medium", "high"]),
            _m("claude-opus-4-7", "Claude Opus 4.7", "", ["low", "medium", "high"]),
            _m("claude-sonnet-4-6", "Claude Sonnet 4.6", "current Drafting and Events default; no effort in n8n"),
            _m("claude-opus-4-6", "Claude Opus 4.6", "no effort in n8n"),
            _m("claude-opus-4-5-20251101", "Claude Opus 4.5", "no effort in n8n"),
            _m("claude-haiku-4-5-20251001", "Claude Haiku 4.5", "current Recap default; no effort"),
            _m("claude-sonnet-4-5-20250929", "Claude Sonnet 4.5", "no effort in n8n"),
        ],
    },
}

# Shown for "Default" when the engine's default is the CLI's own choice rather than a listed model.
DEFAULT_LABEL = {"gemini_cli": "Antigravity's default model"}

CUSTOM_EFFORTS = {"claude_cli": E5, "codex_cli": CODEX_ALL, "gemini_cli": ["low", "medium", "high"],
                  "ollama": ["off", "on"], "n8n": ["low", "medium", "high"]}

ENGINES = [("claude_cli", "Claude"), ("codex_cli", "ChatGPT"), ("gemini_cli", "Gemini"),
           ("ollama", "Ollama"), ("n8n", "n8n")]


def _ollama_models() -> tuple[list[dict[str, Any]], str | None]:
    from .catalog import _ollama
    from .ollama_provider import OllamaProvider
    try:
        listed = _ollama()
    except Exception as exc:  # noqa: BLE001 - shown in the row
        return [], f"Ollama not reachable: {type(exc).__name__}: {exc}"[:200]
    probe = OllamaProvider("probe")
    out = []
    for m in listed:
        mid = m["id"]
        if "embed" in mid:                                  # embedding models cannot write text
            continue
        try:
            caps = probe.show(mid).get("capabilities") or []
        except Exception:  # noqa: BLE001
            caps = []
        cloud = mid.endswith("-cloud") or ":cloud" in mid
        where = "runs on ollama.com, not on this Mac" if cloud else "local"
        if mid.startswith("gpt-oss"):                       # thinking cannot be switched off, only graded
            out.append(_m(mid, m["label"], where + "; thinks at the chosen level", ["low", "medium", "high"], "low"))
        elif "thinking" in caps:
            out.append(_m(mid, m["label"], where + "; can think before answering (slower)", ["off", "on"], "off"))
        else:
            out.append(_m(mid, m["label"], where))
    return out, None


def _agy_label(slug: str) -> str:
    m = re.fullmatch(r"(.*?)-(low|medium|high)", slug)
    base, think = (m.group(1), m.group(2)) if m else (slug, "")
    nice = " ".join(w.capitalize() if not re.match(r"^[\d.]+$", w) else w for w in base.split("-"))
    nice = nice.replace("Gpt", "GPT").replace("Oss", "OSS")
    return nice + (f" · {think} thinking" if think else "")


def _agy_models() -> tuple[list[dict[str, Any]], str | None, bool]:
    """Models of the signed-in Antigravity account; the documented list when agy cannot answer."""
    from .cli_providers import AntigravityCLIProvider, ProviderError
    try:
        slugs = AntigravityCLIProvider("probe").list_models()
    except ProviderError as exc:
        return CURATED["gemini_cli"]["models"], str(exc)[:200], False
    except Exception as exc:  # noqa: BLE001
        return CURATED["gemini_cli"]["models"], f"{type(exc).__name__}: {exc}"[:200], False
    if not slugs:
        return CURATED["gemini_cli"]["models"], None, False
    out = []
    for s in slugs:
        has_level = bool(re.search(r"-(low|medium|high)$", s))
        out.append(_m(s, _agy_label(s), "", [] if has_level else ["low", "medium", "high"]))
    return out, None, True


def _n8n_settings() -> dict[str, str]:
    return {"model": model_prefs.get("n8n") or "", "effort": model_prefs.get_effort("n8n", "*") or ""}


def n8n_choice() -> str:
    """The n8n model as last saved from MailRoom (mirrored locally; the n8n table is the source)."""
    return _n8n_settings()["model"]


def _find(models: list[dict[str, Any]], mid: str) -> dict[str, Any] | None:
    return next((m for m in models if m["id"] == mid), None)


def engine_rows() -> list[dict[str, Any]]:
    rows = []
    for eid, label in ENGINES:
        if eid == "n8n":
            s = _n8n_settings()
            models = CURATED[eid]["models"]
            rows.append({"id": eid, "label": label, "chosen": s["model"] or None, "default": "",
                         "defaultLabel": N8N_DEFAULTS, "effective": s["model"] or N8N_DEFAULTS,
                         "effort": s["effort"] or None, "models": models, "source": CURATED[eid]["source"],
                         "asOf": AS_OF, "live": False, "testable": False, "served": {}, "error": None,
                         "customEfforts": CUSTOM_EFFORTS[eid]})
            continue
        chosen = model_prefs.get(eid)
        if eid == "ollama":
            models, err = _ollama_models()
            src, live = "installed on this Mac (live)", True
        elif eid == "gemini_cli":
            models, err, live = _agy_models()
            src = "your Antigravity account (agy models, live)" if live else CURATED[eid]["source"]
        else:
            models, err = CURATED[eid]["models"], None
            src, live = CURATED[eid]["source"], False
        effective = resolve_model(eid)
        default = PROVIDERS[eid][2]
        dm = _find(models, default)
        default_label = (DEFAULT_LABEL.get(eid) if default == "default" else None) or (dm["label"] if dm else default)
        rows.append({"id": eid, "label": label, "chosen": chosen, "default": default,
                     "defaultLabel": default_label, "effective": default_label if effective == "default" else effective,
                     "effort": resolve_effort(eid, effective), "efforts": model_prefs.all_efforts(eid),
                     "models": models, "source": src, "asOf": None if live else AS_OF, "live": live,
                     "testable": True, "served": model_prefs.confirmed(eid), "error": err,
                     "customEfforts": CUSTOM_EFFORTS[eid]})
    return rows


def effective_for_job(runner: str, provider: str | None, pinned: str | None) -> str:
    if runner == "n8n":
        s = _n8n_settings()
        if not s["model"]:
            return "workflow defaults"
        return s["model"] + (f" · {s['effort']}" if s["effort"] else "")
    if provider in PROVIDERS:
        m = resolve_model(provider, pinned)
        e = resolve_effort(provider, m)
        return m + (f" · {e}" if e else "")
    return pinned or ""


_ID = re.compile(r"[A-Za-z0-9._:/\-\[\]]{1,100}")
_EFFORT = re.compile(r"[a-z]{2,10}")


def _valid(model: str, effort: str) -> tuple[str, str]:
    m, e = (model or "").strip(), (effort or "").strip().lower()
    if m and not _ID.fullmatch(m):
        raise ValueError("a model ID may use letters, digits and . _ : / - [ ] only")
    if e and not _EFFORT.fullmatch(e):
        raise ValueError("unknown effort level")
    return m, e


def set_choice(engine: str, model: str, effort: str = "") -> list[dict[str, Any]]:
    """Save (or, with an empty model, reset) the model for one engine, and its effort."""
    m, e = _valid(model, effort)
    if engine == "n8n":
        known = _find(CURATED["n8n"]["models"], m)
        if e and known is not None and e not in known["efforts"]:
            raise ValueError(f"{m} has no effort setting in n8n")
        if not m:
            e = ""
        from ..connectors import n8n_tables as t
        base = t._base()
        rows = {r.get("key"): r for r in t.rows("settings", base) if r.get("key")}
        for key, val in ((N8N_KEY, m), (N8N_EFFORT_KEY, e)):
            if key in rows:
                t.update("settings", base, rows[key]["id"], {"value": val})
            elif val:
                t.insert("settings", base, {"key": key, "value": val})
        model_prefs.set_effort("n8n", "*", e or None)
    elif engine not in PROVIDERS:
        raise KeyError(f"unknown engine {engine}")
    if m:
        model_prefs.set_choice(engine, m)      # for n8n: local mirror, so the Run card can show it
    else:
        model_prefs.clear(engine)
    if engine != "n8n":
        model_prefs.set_effort(engine, m or PROVIDERS[engine][2], e or None)
    return engine_rows()
