"""Settings → Engine sign-in: status, sign-in and verification for the three CLI engines.

* status  - a cheap check that uses no tokens (`claude auth status`, `codex login status`, Gemini's
            cached sign-in), plus where the CLI was found.
* sign in - the CLIs sign in through a browser from an interactive terminal, so MailRoom opens
            Terminal with the right command (`claude auth login`, `codex login`, `gemini`). Nothing is
            typed or stored by MailRoom; the CLI keeps its own sign-in (Keychain / its config folder).
* verify  - the status check, then one tiny prompt ("Reply with the word ok."), which also reports
            the exact model the engine used. It uses a very small part of your plan.
"""
from __future__ import annotations

import shlex
import subprocess
import sys
import time
from typing import Any

from .providers import get_provider
from .providers.base import ProviderError
from .providers.cli_providers import find_binary
from .settings import settings

ENGINES: dict[str, dict[str, Any]] = {
    "claude_cli": {"label": "Claude", "args": ["auth", "login"],
                   "steps": "A browser opens: sign in with the Claude account that has your Pro or Max plan. "
                            "When the Terminal window says you are signed in, come back and press Verify.",
                   "install": "Install Claude Code: https://code.claude.com"},
    "codex_cli": {"label": "ChatGPT", "args": ["login"],
                  "steps": "A browser opens: choose Sign in with ChatGPT and use the account with your ChatGPT "
                           "plan. When Terminal says you are logged in, come back and press Verify.",
                  "install": "Install the Codex CLI: brew install codex (or npm i -g @openai/codex)"},
    "gemini_cli": {"label": "Gemini", "args": [],
                   "steps": "Antigravity CLI (agy) opens in Terminal and, the first time, a browser window to sign "
                            "in with your Google account. When the agy prompt appears you are signed in: close it "
                            "(Ctrl+C), come back and press Verify.",
                   "install": "Install Antigravity CLI: curl -fsSL https://antigravity.google/cli/install.sh | bash"},
}


def _provider(eid: str):
    if eid not in ENGINES:
        raise KeyError(f"unknown engine {eid}")
    return get_provider(eid)


def status(eid: str) -> dict[str, Any]:
    """No tokens used. state: ok | signed_out | missing."""
    p = _provider(eid)
    exe = find_binary(p.binary)
    row = {"id": eid, "label": ENGINES[eid]["label"], "binary": exe or "", "steps": ENGINES[eid]["steps"]}
    if not exe:
        return {**row, "state": "missing", "detail": f"`{p.binary}` was not found. {ENGINES[eid]['install']}"}
    try:
        return {**row, "state": "ok", "detail": p.login_status()}
    except ProviderError as exc:
        return {**row, "state": "signed_out", "detail": str(exc)[:300]}
    except Exception as exc:  # noqa: BLE001 - shown in the row
        return {**row, "state": "signed_out", "detail": f"{type(exc).__name__}: {exc}"[:300]}


def open_signin(eid: str) -> dict[str, Any]:
    """Open Terminal with the CLI's own sign-in command (macOS)."""
    p = _provider(eid)
    exe = find_binary(p.binary)
    if not exe:
        raise RuntimeError(f"`{p.binary}` was not found. {ENGINES[eid]['install']}")
    cmd = " ".join(shlex.quote(a) for a in [exe, *ENGINES[eid]["args"]])
    if sys.platform != "darwin":
        raise RuntimeError(f"Open a terminal and run: {cmd}")
    d = settings.data_dir / "run"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"signin-{eid}.command"
    f.write_text("#!/bin/zsh -l\n"
                 f"echo 'MailRoom: signing in to {ENGINES[eid]['label']}'\necho\n{cmd}\n"
                 "echo\necho 'Finished. Go back to MailRoom and press Verify. You can close this window.'\n",
                 encoding="utf-8")
    f.chmod(0o700)
    r = subprocess.run(["open", "-a", "Terminal", str(f)], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise RuntimeError(f"could not open Terminal ({(r.stderr or r.stdout).strip()[:200]}). Run: {cmd}")
    return {"ok": True, "command": cmd, "steps": ENGINES[eid]["steps"]}


def verify(eid: str) -> dict[str, Any]:
    """Status check, then one tiny prompt that proves the engine answers and names the model used."""
    st = status(eid)
    if st["state"] == "missing":
        return {**st, "ok": False}
    p = _provider(eid)
    t0 = time.monotonic()
    ok, detail = p.health()
    took = round(time.monotonic() - t0, 1)
    served = (getattr(p, "served", None) or {}).get("model") or ""
    return {**st, "ok": ok, "state": "ok" if ok else st["state"] if st["state"] != "ok" else "error",
            "model": served or p.model, "seconds": took,
            "detail": (f"answered in {took}s" + (f" · model {served}" if served else "")) if ok else detail[:400]}
