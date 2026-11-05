"""Service-by-service self-test.

Each check is isolated and cheap: one API call, a short timeout, a verdict and a
human-readable detail. A failure never raises - it is reported - so a single
broken credential does not hide the state of everything else.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from .connectors import gcal, google_auth, gsheets
from .connectors import n8n as n8n_conn
from .jobs import load_jobs
from .providers import PROVIDERS, get_provider
from .rules import Rules
from .secrets_store import store


@dataclass
class Check:
    name: str
    group: str
    ok: bool
    detail: str
    hint: str = ""


def _run(name: str, group: str, fn: Callable[[], str], hint: str = "") -> Check:
    try:
        return Check(name, group, True, fn()[:300])
    except Exception as exc:  # noqa: BLE001 - the message is the product here
        return Check(name, group, False, f"{type(exc).__name__}: {exc}"[:300], hint)


# ------------------------------------------------------------------ individual
def _check_google_token() -> str:
    st = google_auth.status()
    if not st.get("connected"):
        raise RuntimeError(st.get("reason", "not connected"))
    google_auth.credentials()          # forces a refresh if needed
    missing = set(google_auth.SCOPES) - set(st.get("scopes") or [])
    if missing:
        raise RuntimeError("missing scopes: " + ", ".join(sorted(missing)))
    return f"token valid, {len(st['scopes'])} scopes, refresh token "\
           f"{'present' if st['has_refresh_token'] else 'MISSING'}"


def _check_gmail() -> str:
    from .connectors.gmail import GmailAccount
    p = GmailAccount().profile()
    return f"{p['emailAddress']} · {p.get('messagesTotal', '?')} messages"


def _check_gmail_query(rules: Rules) -> str:
    from .connectors.gmail import GmailAccount
    jobs = [j for j in load_jobs() if j.runner == "pipeline"]
    query = jobs[0].params.get("query", "in:inbox newer_than:1d") if jobs else "in:inbox newer_than:1d"
    msgs = GmailAccount().fetch(query, 3)
    if not msgs:
        return f"query {query!r} matched nothing (not an error)"
    return f"query {query!r} → {len(msgs)} sampled, newest: {msgs[0].subject[:60]!r}"


def _check_calendar(rules: Rules) -> str:
    cal_id = rules.calendar.get("calendar_id", "primary")
    now = datetime.now(timezone.utc)
    busy = gcal.busy_blocks(cal_id, now, now + timedelta(days=7))
    slots = gcal.propose_slots(rules.calendar, rules.timezone, busy=busy)
    return (f"calendar {cal_id!r}: {len(busy)} busy blocks in 7d, "
            f"{len(slots)} slots proposable"
            + (f" (first: {slots[0].human()})" if slots else ""))


def _check_sheets(rules: Rules) -> str:
    sid = rules.sheets.get("spreadsheet_id")
    if not sid:
        raise RuntimeError("rules.yaml → sheets.spreadsheet_id is empty")
    tab = rules.sheets.get("tab_name", "Inbox Recap")
    gsheets.ensure_tab(sid, tab, rules.sheets.get("columns", []))
    return f"spreadsheet reachable, tab {tab!r} ready"


def _check_n8n(rules: Rules) -> str:
    base, detail = n8n_conn.reach(n8n_conn.base_url(rules))
    return f"reachable: {detail} (webhook not fired)"


def _check_n8n_api(rules: Rules) -> str:
    info = n8n_conn.list_workflows(rules, limit=100)
    hooked = [w for w in info["workflows"] if w["webhooks"]]
    if hooked:
        w = hooked[0]
        return (f"{info['count']} workflows, {len(hooked)} with a webhook trigger; "
                f"first: {w['name']!r} → {w['webhooks'][0]['production_url']}")
    kinds: dict[str, int] = {}
    for w in info["workflows"]:
        for t in w["triggers"]:
            kinds[t] = kinds.get(t, 0) + 1
    summary = ", ".join(f"{k}×{v}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])[:6])
    raise RuntimeError(
        f"{info['count']} workflows reachable, but none has a Webhook trigger. "
        f"Triggers in use: {summary or 'none detected'}. n8n's public API has no "
        f"'run workflow' endpoint, so a Webhook node is the only way this app can "
        f"start a workflow on demand."
    )


def _check_n8n_webhook(rules: Rules) -> str:
    """Prove the job's webhook is live in the PUBLISHED workflow, without running it.

    1. Find the webhook node whose path matches jobs.yaml (via the REST API).
    2. Require method POST. Only then is step 3 side-effect free: a GET against a
       POST-only webhook cannot execute the workflow.
    3. GET the production URL. n8n answers 404 either way, but the body tells
       registered-for-another-method apart from not-registered-at-all, which is
       the only way to see draft-vs-published from outside.
    """
    jobs = [j for j in load_jobs() if j.runner == "n8n"]
    if not jobs:
        raise RuntimeError("no n8n job configured in jobs.yaml")
    j = jobs[0]
    if not j.webhook_url or "/webhook/" not in j.webhook_url:
        raise RuntimeError(f"job {j.job_id!r}: webhook_url must be a production /webhook/ URL")
    path = j.webhook_url.split("/webhook/", 1)[1].strip("/")

    info = n8n_conn.list_workflows(rules, limit=250)
    owner = hook = None
    for w in info["workflows"]:
        for h in w["webhooks"]:
            if h["path"] == path:
                owner, hook = w, h
    if not hook:
        raise RuntimeError(f"no workflow has a webhook with path {path!r}; "
                           f"copy the URL from Discover workflows")
    if hook["method"] != "POST":
        raise RuntimeError(f"{owner['name']!r} webhook method is {hook['method']}; "
                           f"this app sends POST")
    if not hook.get("connected", True):
        raise RuntimeError(f"{owner['name']!r} webhook is not connected to any node")
    if not owner["active"]:
        raise RuntimeError(f"{owner['name']!r} is not active/published")

    with httpx.Client(timeout=8.0) as c:
        r = c.get(j.webhook_url)
    body = r.text.lower()
    if "not registered for get" in body:
        return f"{owner['name']!r} → {hook['node']} is published and listening for POST"
    if "is not registered" in body:
        raise RuntimeError(f"{owner['name']!r} has the webhook in its draft but it is not "
                           f"in the published version — publish the workflow in n8n")
    raise RuntimeError(f"unexpected probe answer {r.status_code}: {r.text[:160]}")


# ----------------------------------------------------------------------- suite
def run_all(providers: bool = True) -> dict[str, Any]:
    rules = Rules.load()
    checks: list[Check] = []

    present = store.status()
    checks.append(Check(
        "Keychain", "secrets",
        any(present.values()),
        ", ".join(k for k, v in present.items() if v) or "no secrets stored yet",
        "Add at least one API key and the Google client JSON in Settings.",
    ))

    checks.append(_run("OAuth token", "google", _check_google_token,
                       "Click Connect Google. If scopes are missing, re-authorize."))
    checks.append(_run("Gmail", "google", _check_gmail,
                       "Enable the Gmail API in your Google Cloud project."))
    checks.append(_run("Gmail query", "google", lambda: _check_gmail_query(rules)))
    checks.append(_run("Calendar", "google", lambda: _check_calendar(rules),
                       "Enable the Calendar API; check calendar_id in rules.yaml."))
    checks.append(_run("Sheets", "google", lambda: _check_sheets(rules),
                       "Paste the spreadsheet ID into rules.yaml and share nothing "
                       "- it is your own account."))
    checks.append(_run("reachable", "n8n", lambda: _check_n8n(rules),
                       "Set n8n.base_url in rules.yaml to where n8n actually runs."))
    checks.append(_run("API key", "n8n", lambda: _check_n8n_api(rules),
                       "Create a key under n8n → Settings → n8n API and paste it "
                       "in Settings."))
    checks.append(_run("job webhook", "n8n", lambda: _check_n8n_webhook(rules),
                       "Copy the production URL from Discover workflows into "
                       "jobs.yaml."))

    if providers:
        for name in PROVIDERS:
            def probe(n=name) -> str:
                p = get_provider(n)
                if hasattr(p, "login_status"):      # CLI: installed + signed in, no tokens spent
                    return p.login_status()
                ok, detail = p.health()
                if not ok:
                    raise RuntimeError(detail)
                return f"{p.model} responded"
            hint = {
                "ollama": "Run `ollama pull <model>`, or set jobs.yaml to a model "
                          "you already have.",
                "anthropic": "Add the key from console.anthropic.com in Settings.",
                "gemini": "Add the key from aistudio.google.com/apikey in Settings.",
                "openai": "Add the key from platform.openai.com/api-keys, and check "
                          "the model ID exists on your account.",
                "claude_cli": "Install Claude Code, then run `claude` in Terminal and sign in "
                              "with your Claude Pro/Max account.",
                "codex_cli": "Install the Codex CLI, then run `codex login` and choose "
                             "Sign in with ChatGPT.",
                "gemini_cli": "Install Antigravity CLI (agy), then use Settings → Engine sign-in → "
                              "Gemini → Sign in.",
            }.get(name, "Check the API key and the model ID.")
            checks.append(_run(name, "providers", probe, hint))

    data = [asdict(c) for c in checks]
    return {"checks": data, "ok": sum(1 for c in data if c["ok"]), "total": len(data)}
