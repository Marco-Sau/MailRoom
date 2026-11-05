"""Settings → every value that used to be hard-coded, in one place.

Two kinds of storage:

* shared  - the n8n "Inbox Pilot Settings" table (key/value). The MailRoom engines AND the n8n
            workflows read them, so both behave the same (windows, limits, keywords, days).
* local   - data/app_settings.json on this Mac (git-ignored): connections, timeouts, address scan,
            quick links. Only MailRoom uses them.

Each field carries its own help text (shown behind the (i) icon in Settings). A missing or invalid
value always falls back to the default, so an empty table or file behaves exactly like before.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .settings import settings

LOCAL_PATH = settings.data_dir / "app_settings.json"

EVENT_KEYWORDS_DEFAULT = [
    "webinar", "webinars", "evento", "eventi", "seminario", "seminari", "seminar", "workshop", "conference",
    "conferenza", "convegno", "corso", "corsi", "course", "masterclass", "meetup", "save the date",
    "iscrizione", "iscriviti", "registrazione", "registration", "register", "live session", "diretta",
    "zoom.us", "teams.microsoft.com", "meet.google.com"]

QUICK_LINKS_DEFAULT = [
    {"label": "Gmail", "sub": "Inbox and drafts", "icon": "mail",
     "url": "https://mail.google.com/mail/?authuser={account}#inbox"},
    {"label": "Google Calendar", "sub": "Your week", "icon": "calendar",
     "url": "https://calendar.google.com/calendar/r/week?authuser={account}"},
    {"label": "Recap Docs", "sub": "The recap folder in Google Drive", "icon": "doc",
     "url": "https://drive.google.com/drive/folders/{recapFolder}?authuser={account}"},
]
QUICK_LINK_ICONS = ("mail", "calendar", "drive", "doc", "link", "n8n")

# Recap priority rubric: the level names and numbers are fixed (the app colors and sorts by them);
# what each level means, and the scoring rules, are editable.
PRIORITY_LEVELS = [(5, "CRITICAL", "priorityCritical"), (4, "HIGH", "priorityHigh"),
                   (3, "MEDIUM", "priorityMedium"), (2, "LOW", "priorityLow"), (1, "IGNORE", "priorityIgnore")]
PRIORITY_DEFAULTS = {
    "priorityCritical": "action required within roughly 24 hours, hard deadline, account security or "
                        "unauthorized-access alert, payment due, legal or contractual obligation.",
    "priorityHigh": "action required but not same-day; direct personal or professional correspondence expecting "
                    "a reply; work requests; appointments needing confirmation.",
    "priorityMedium": "worth keeping as a record but no action needed: receipts, order and delivery "
                      "confirmations, already-booked appointments, official documents.",
    "priorityLow": "informational only, mildly relevant: newsletters on subscribed topics, job postings, "
                   "community or event announcements.",
    "priorityIgnore": "marketing, promotions, cold sales outreach, spam, routine automated notifications with no "
                      "informational value.",
    "priorityRules": "judge priority by what the email actually demands of the recipient, not by how urgent the "
                     "sender claims it is — promotional mail written in urgent language is still 1. A one-time "
                     "passcode or login code is 5 only if it represents pending account access; an already-used "
                     "or expired code is 3. Do not inflate scores: in a typical inbox most emails are 1 or 2.",
}


@dataclass
class Field:
    key: str
    section: str
    label: str
    kind: str                 # int | float | text | url | keywords | links | prompt
    default: Any
    scope: str               # shared | local
    help: str
    min: float | None = None
    max: float | None = None
    unit: str = ""
    step: float | None = None
    multiline: bool = False   # prompt fields: keep line breaks


SECTIONS = [
    ("windows", "Look-back windows and limits",
     "How far back each step reads and how many emails it takes. Shared with n8n: the n8n workflows read the "
     "same values."),
    ("events", "Event detection",
     "Which words make an email a candidate event, webinar or course. Shared with n8n."),
    ("priority", "Recap priority levels",
     "How the model scores each email from 1 to 5 in the recap. You write what each level means and the "
     "scoring rules; the rest of the prompt (topic, summary, output format) stays fixed. Shared with n8n."),
    ("connections", "Connections", "Where MailRoom finds n8n, Ollama and your recap folder. This Mac only."),
    ("engines", "Engine timeouts", "How long MailRoom waits for each engine. This Mac only."),
    ("scan", "Address scan", "How Settings → Your addresses scans the mailbox. This Mac only."),
    ("links", "Quick links", "The tiles at the top of Review. This Mac only."),
]

FIELDS: list[Field] = [
    # ------------------------------------------------------------------ windows (shared)
    Field("lookbackDrafting", "windows", "Replies: standard window", "int", 48, "shared",
          "How many hours back the Standard choice on the Run tab reads when looking for emails to reply to. "
          "A run with another Look back choice uses that instead.\nExample: 48 = the last two days.\n"
          "Allowed: 1 to 168 hours (7 days).", 1, 168, "h"),
    Field("lookbackEvents", "windows", "Events: standard window", "int", 24, "shared",
          "How many hours back the Standard choice reads when looking for event, webinar and course emails.\n"
          "Allowed: 1 to 168 hours.", 1, 168, "h"),
    Field("lookbackRecap", "windows", "Recap: standard window", "int", 24, "shared",
          "How many hours of mail the Standard recap covers.\nAllowed: 1 to 168 hours.", 1, 168, "h"),
    Field("eventsMaxEmails", "windows", "Events: max emails per run", "int", 40, "shared",
          "The most event candidates checked in one run. Each one costs a model call.\nAllowed: 1 to 100.",
          1, 100),
    Field("recapMaxEmails", "windows", "Recap: max emails", "int", 300, "shared",
          "The most emails listed in one recap. With a long window you may reach it; the run log says so.\n"
          "Allowed: 10 to 500.", 10, 500),
    Field("recapBatchSize", "windows", "Recap: emails per model call", "int", 10, "shared",
          "How many emails the model ranks at once. Smaller batches are more reliable on small local models "
          "(Ollama), larger ones need fewer calls.\nAllowed: 3 to 25. Suggested: 10, or 5 for Ollama.", 3, 25),
    Field("followUpDays", "windows", "Draft follow-up: days", "int", 7, "shared",
          "How many days back each run checks what happened to earlier reply drafts (still waiting, sent, "
          "deleted) and which proposed meeting times are still held.\nAllowed: 1 to 30.", 1, 30, "days"),
    Field("earlierItemsDays", "windows", "Earlier documents: days", "int", 60, "shared",
          "When an email asks about something sent before (\"did you read the document I sent you?\"), how many "
          "days back MailRoom looks for it in Gmail.\nAllowed: 7 to 365.", 7, 365, "days"),
    Field("earlierItemsMaxEmails", "windows", "Earlier documents: emails searched", "int", 15, "shared",
          "How many earlier emails from that sender (with attachments or document links) are searched.\n"
          "Allowed: 5 to 50.", 5, 50),
    # ------------------------------------------------------------------ events (shared)
    Field("eventKeywords", "events", "Event keywords", "keywords", EVENT_KEYWORDS_DEFAULT, "shared",
          "An email is checked as a possible event only if it contains at least one of these words or phrases "
          "(subject or body). One per line; phrases with spaces are matched as a whole. Add words in any "
          "language you receive invitations in.\nExample: webinar, convegno, \"save the date\"."),
    # ------------------------------------------------------------------ priority (shared)
    *[Field(key, "priority", f"{n} · {name}", "prompt", PRIORITY_DEFAULTS[key], "shared",
            f"What makes an email level {n} ({name}). The model reads it as one line of the rubric:\n"
            f"\"{n} = {name} — <your text>\"\nName the kinds of email and the action they need; concrete "
            "examples work better than adjectives. One line; at most 600 characters.", None, 600)
      for n, name, key in PRIORITY_LEVELS],
    Field("priorityRules", "priority", "Scoring rules", "prompt", PRIORITY_DEFAULTS["priorityRules"], "shared",
          "Rules that apply across levels, read after the five definitions. Use them for tie-breaks and "
          "exceptions.\nExample: \"Anything from my bank is at least 4.\" or \"Mail from my accountant "
          "is never below 3.\"\nAt most 2000 characters; line breaks are kept.", None, 2000,
          multiline=True),
    # ------------------------------------------------------------------ connections (local)
    Field("n8nBaseUrl", "connections", "n8n address", "url", "", "local",
          "The address of your n8n, without a trailing slash. Used for the API (executions, data tables).\n"
          "Example: https://n8n.example.com"),
    Field("n8nWebhookUrl", "connections", "n8n run webhook", "url",
          "", "local",
          "The production URL of the Webhook node of the \"Gmail Assistant\" workflow: the n8n Run button "
          "calls it. Find it in n8n → Gmail Assistant → Webhook node → Production URL."),
    Field("decisionUrl", "connections", "Decision page webhook", "url",
          "", "local",
          "The URL behind the BOOK / ADD / KEEP / SWAP links in the notes MailRoom writes. It is the webhook of "
          "the \"Gmail Assistant — Decisions\" workflow. The n8n workflows keep their own copy."),
    Field("ollamaHost", "connections", "Ollama address", "url", "http://127.0.0.1:11434", "local",
          "Where Ollama listens. Change it only if Ollama runs on another port or another Mac.\n"
          "Default: http://127.0.0.1:11434"),
    Field("recapFolderId", "connections", "Recap Google Drive folder", "text", "",
          "local",
          "The ID of the Drive folder for recap Docs made by the MailRoom engines: the last part of the folder's "
          "address (drive.google.com/drive/folders/<ID>). Leave empty for My Drive. MailRoom can only write "
          "into folders it created unless Google is re-authorized with full Drive access."),
    # ------------------------------------------------------------------ engines (local)
    Field("cliTimeoutSeconds", "engines", "Claude / ChatGPT / Gemini: max wait per call", "int", 360, "local",
          "Seconds MailRoom waits for one answer from a command-line engine before giving up on that email.\n"
          "Allowed: 60 to 1800.", 60, 1800, "s"),
    Field("ollamaTimeoutSeconds", "engines", "Ollama: max wait per call", "int", 600, "local",
          "Seconds MailRoom waits for one Ollama answer. Large local models are slow on the first call while "
          "they load.\nAllowed: 60 to 3600.", 60, 3600, "s"),
    Field("ollamaContext", "engines", "Ollama: context window", "int", 8192, "local",
          "Tokens of text Ollama keeps in view per call. Too small cuts long emails or recap batches; larger "
          "uses more memory.\nAllowed: 2048 to 131072. Suggested: 8192.", 2048, 131072, "tokens"),
    Field("n8nFollowMinutes", "engines", "n8n: follow a run for at most", "int", 20, "local",
          "Minutes MailRoom keeps showing an n8n run's progress. After that the run continues in n8n on its own."
          "\nAllowed: 5 to 120.", 5, 120, "min"),
    Field("n8nPollSeconds", "engines", "n8n: progress refresh", "float", 2.5, "local",
          "Seconds between progress checks of an n8n run.\nAllowed: 1 to 30.", 1, 30, "s", 0.5),
    # ------------------------------------------------------------------ scan (local)
    Field("scanDays", "scan", "Scan: days of mail", "int", 30, "local",
          "How many days of received mail the address scan reads.\nAllowed: 7 to 180.", 7, 180, "days"),
    Field("scanMaxEmails", "scan", "Scan: max emails", "int", 500, "local",
          "The most emails read by one scan.\nAllowed: 50 to 2000.", 50, 2000),
    Field("scanMinSeen", "scan", "Scan: minimum emails per address", "int", 2, "local",
          "An address that is not your main address or an alias is listed only if it was the only recipient "
          "of at least this many emails.\nAllowed: 1 to 10.", 1, 10),
    # ------------------------------------------------------------------ links (local)
    Field("quickLinks", "links", "Quick links", "links", QUICK_LINKS_DEFAULT, "local",
          "Tiles shown at the top of Review. Each has a name, a short line under it, an icon and an address. "
          "Write {recapFolder} for the recap Drive folder (Advanced → Connections; Google Docs home when empty). "
          "Write {account} where your main Gmail address should go: it opens the right Google account when "
          "several are signed in.\nExamples:\nhttps://mail.google.com/mail/?authuser={account}#drafts\n"
          "https://drive.google.com/drive/my-drive?authuser={account}\nhttps://n8n.example.com/home/workflows"),
]
BY_KEY = {f.key: f for f in FIELDS}


# ---------------------------------------------------------------------------- parsing / validation
def _clean(f: Field, value: Any) -> Any:
    """Validated value, or raise ValueError with a message for the user."""
    if f.kind in ("int", "float"):
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{f.label}: enter a number") from None
        if f.kind == "int":
            if v != int(v):
                raise ValueError(f"{f.label}: enter a whole number")
            v = int(v)
        if f.min is not None and v < f.min or f.max is not None and v > f.max:
            raise ValueError(f"{f.label}: must be between {f.min:g} and {f.max:g}")
        return v
    if f.kind == "url":
        v = str(value or "").strip().rstrip("/")
        if not v:
            return ""        # not set
        if not re.fullmatch(r"https?://[^\s]+", v):
            raise ValueError(f"{f.label}: enter a full address starting with https:// or http://")
        return v
    if f.kind == "text":
        v = str(value or "").strip()
        if len(v) > 200 or re.search(r"\s", v):
            raise ValueError(f"{f.label}: no spaces, at most 200 characters")
        return v
    if f.kind == "prompt":
        v = str(value or "")
        v = v.strip() if f.multiline else re.sub(r"\s+", " ", v).strip()
        if not v:
            raise ValueError(f"{f.label}: write something (use reset for the default)")
        if f.max is not None and len(v) > f.max:
            raise ValueError(f"{f.label}: at most {f.max:g} characters (now {len(v)})")
        return v
    if f.kind == "keywords":
        items = value if isinstance(value, list) else str(value or "").splitlines()
        out = []
        for k in items:
            k = str(k).strip().strip('"').strip()
            if k and k.lower() not in [x.lower() for x in out]:
                if '"' in k or "{" in k or "}" in k or len(k) > 60:
                    raise ValueError(f"{f.label}: \"{k[:30]}\" may not contain quotes or braces (max 60 characters)")
                out.append(k)
        if not out:
            raise ValueError(f"{f.label}: keep at least one keyword")
        return out
    if f.kind == "links":
        if not isinstance(value, list):
            raise ValueError(f"{f.label}: a list is expected")
        out = []
        for i, l in enumerate(value, 1):
            label = str((l or {}).get("label") or "").strip()
            url = str((l or {}).get("url") or "").strip()
            if not label and not url:
                continue
            if not label or len(label) > 40:
                raise ValueError(f"Quick link {i}: give it a name (max 40 characters)")
            if not re.fullmatch(r"https?://[^\s]+", url):
                raise ValueError(f"Quick link {i}: the address must start with https:// or http://")
            icon = str((l or {}).get("icon") or "link")
            out.append({"label": label, "sub": str((l or {}).get("sub") or "").strip()[:60],
                        "icon": icon if icon in QUICK_LINK_ICONS else "link", "url": url})
        if len(out) > 12:
            raise ValueError("Quick links: at most 12")
        return out
    raise ValueError(f"unknown field type {f.kind}")


def _default(f: Field) -> Any:
    """The value used when nothing is saved. Connections fall back to the config files they replace."""
    try:
        if f.key == "n8nBaseUrl":
            from .rules import Rules
            v = ((Rules.load().raw.get("n8n") or {}).get("base_url") or "")
            return str(v).rstrip("/") or f.default
        if f.key == "n8nWebhookUrl":
            import yaml
            data = yaml.safe_load(settings.jobs_path.read_text(encoding="utf-8")) or {}
            for j in data.get("jobs", []):
                if j.get("runner") == "n8n" and j.get("webhook_url"):
                    return str(j["webhook_url"])
    except Exception:  # noqa: BLE001
        pass
    return f.default


def _parse(f: Field, raw: Any) -> Any:
    """Stored value -> typed value; anything unusable falls back to the default."""
    if raw is None or raw == "":
        return _default(f)
    try:
        if f.scope == "shared" and f.kind in ("keywords", "links") and isinstance(raw, str):
            raw = json.loads(raw)
        return _clean(f, raw)
    except (ValueError, TypeError):
        return _default(f)


# ---------------------------------------------------------------------------- local store
def _read_local() -> dict[str, Any]:
    try:
        data = json.loads(LOCAL_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_local(data: dict[str, Any]) -> None:
    LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LOCAL_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(LOCAL_PATH)


def local(key: str) -> Any:
    f = BY_KEY[key]
    return _parse(f, _read_local().get(key))


def local_explicit(key: str) -> Any | None:
    """The saved value only (None when the user never changed it)."""
    raw = _read_local().get(key)
    return None if raw in (None, "") else _parse(BY_KEY[key], raw)


def shared(st: dict[str, str] | None, key: str) -> Any:
    """A shared value from the n8n settings rows already loaded for a run (st)."""
    f = BY_KEY[key]
    return _parse(f, (st or {}).get(key))


# ---------------------------------------------------------------------------- read all / save
def _shared_rows() -> tuple[dict[str, dict[str, Any]], str, str | None]:
    from .connectors import n8n_tables as t
    try:
        base = t._base()
        return {r.get("key"): r for r in t.rows("settings", base) if r.get("key")}, base, None
    except Exception as exc:  # noqa: BLE001 - shown in Settings
        return {}, "", f"{type(exc).__name__}: {exc}"[:300]


def snapshot() -> dict[str, Any]:
    rows, _, err = _shared_rows()
    loc = _read_local()
    sections = []
    for sid, title, intro in SECTIONS:
        fields = []
        for f in FIELDS:
            if f.section != sid:
                continue
            raw = (rows.get(f.key) or {}).get("value") if f.scope == "shared" else loc.get(f.key)
            d = asdict(f)
            d["value"] = _parse(f, raw)
            d["default"] = _default(f)
            d["isDefault"] = raw in (None, "")
            fields.append(d)
        sections.append({"id": sid, "title": title, "intro": intro, "fields": fields})
    return {"sections": sections, "sharedError": err, "icons": list(QUICK_LINK_ICONS)}


def save(values: dict[str, Any]) -> dict[str, Any]:
    """Validate everything first, then write. A value equal to the default is stored as empty."""
    clean: dict[str, Any] = {}
    for k, v in values.items():
        if k not in BY_KEY:
            raise KeyError(f"unknown setting {k}")
        clean[k] = None if v is None else _clean(BY_KEY[k], v)
    loc = _read_local()
    shared_writes = {}
    for k, v in clean.items():
        f = BY_KEY[k]
        is_default = v is None or v == _default(f)
        if f.scope == "local":
            if is_default:
                loc.pop(k, None)
            else:
                loc[k] = v
        else:
            shared_writes[k] = "" if is_default else (json.dumps(v, ensure_ascii=False)
                                                         if isinstance(v, list) else str(v))
    if shared_writes:
        from .connectors import n8n_tables as t
        rows, base, err = _shared_rows()
        if err:
            raise RuntimeError(f"n8n is not reachable, shared settings were not saved: {err}")
        for k, v in shared_writes.items():
            if k in rows:
                t.update("settings", base, rows[k]["id"], {"value": v})
            elif v:
                t.insert("settings", base, {"key": k, "value": v})
    _write_local(loc)
    return snapshot()


def priority_rubric(get) -> str:
    """STEP 2 of the recap prompt from the six priority settings; get(key) returns a value.

    Built the same way by the n8n Recap workflow (Build Email Recap Payload)."""
    lines = ["STEP 2 — PRIORITY: assign every email an integer priority from 1 to 5 using this rubric:"]
    lines += [f"{n} = {name} — {get(key)}" for n, name, key in PRIORITY_LEVELS]
    return "\n".join(lines) + "\n\nScoring rules: " + get("priorityRules")


def event_query(keywords: list[str]) -> str:
    """Gmail search for event candidates, built from the keyword list (same string as n8n)."""
    words = " ".join(f'"{k}"' if (" " in k or "." in k) else k for k in keywords)
    return "-in:draft -in:sent -in:chats {" + words + "}"
