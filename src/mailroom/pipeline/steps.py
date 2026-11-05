"""The triage pipeline, step by step.

Each step is a plain function taking a mutable RunContext so that a job can
enable a subset (`steps: [fetch, classify, prioritize, sheets]`) without the
steps knowing about each other.

Concurrency: classification and drafting are per-email network calls dominated
by latency, so they run in a bounded thread pool. The pool is small on purpose -
provider rate limits, not local CPU, are the binding constraint, and Ollama on
a laptop serializes anyway.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..connectors import gcal, gmail, gsheets
from ..models import Classification, DraftProposal, EmailRef, RunSpec, TriagedEmail
from ..providers.base import LLMProvider
from ..rules import Rules
from . import prompts

log = logging.getLogger(__name__)
DEFAULT_WORKERS = 4
VALID_PRIORITIES = {"P1", "P2", "P3", "P4"}


@dataclass
class RunContext:
    spec: RunSpec
    rules: Rules
    provider: LLMProvider
    run_id: int
    emails: list[EmailRef] = field(default_factory=list)
    triaged: list[TriagedEmail] = field(default_factory=list)
    slot_strings: list[str] = field(default_factory=list)
    slots: list = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def say(self, msg: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.log_lines.append(f"[{stamp}] {msg}")
        log.info(msg)


# --------------------------------------------------------------------------- fetch
def step_fetch(ctx: RunContext) -> None:
    account = gmail.get_account(ctx.spec.account)
    query = ctx.spec.params.get("query", "in:inbox newer_than:2d")
    limit = int(ctx.spec.params.get("max_emails", 40))
    ctx.emails = account.fetch(query, limit)
    ctx.stats["fetched"] = len(ctx.emails)
    ctx.say(f"fetched {len(ctx.emails)} messages ({query!r})")


# ----------------------------------------------------------------- classify + rules
def _classify_one(ctx: RunContext, e: EmailRef) -> TriagedEmail:
    raw = ctx.provider.complete_json(
        prompts.classify_system(ctx.rules), prompts.classify_user(e), max_tokens=600
    )
    cat = raw.get("category")
    if cat not in ctx.rules.category_ids:
        cat = "other"
    pri = raw.get("priority")
    if pri not in VALID_PRIORITIES:
        pri = "P3"
    c = Classification(
        category=cat,
        priority=pri,
        priority_reason=str(raw.get("priority_reason", ""))[:300],
        needs_appointment=bool(raw.get("needs_appointment")),
        appointment_hint=raw.get("appointment_hint") or None,
        summary=str(raw.get("summary", ""))[:500],
        language=(raw.get("language") or "en")[:5],
        confidence=float(raw.get("confidence") or 0.0),
    )
    return TriagedEmail(email=e, classification=c)


def step_classify(ctx: RunContext) -> None:
    workers = 1 if ctx.provider.name == "ollama" else int(
        ctx.spec.params.get("workers", DEFAULT_WORKERS)
    )
    results: list[TriagedEmail] = []
    failures = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_classify_one, ctx, e): e for e in ctx.emails}
        for fut, e in futures.items():
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001 - one bad email must not kill the run
                failures += 1
                ctx.say(f"classify failed for {e.subject[:50]!r}: {exc}")
    ctx.triaged = results
    ctx.stats["classified"] = len(results)
    ctx.stats["classify_failures"] = failures
    ctx.say(f"classified {len(results)} messages ({failures} failed) via "
            f"{ctx.provider.name}/{ctx.provider.model}")


def step_prioritize(ctx: RunContext) -> None:
    """Deterministic overrides on top of the model's judgement."""
    changed = 0
    for t in ctx.triaged:
        new_pri, reason = ctx.rules.apply_overrides(
            sender=t.email.sender, subject=t.email.subject,
            category=t.classification.category, llm_priority=t.classification.priority,
        )
        if new_pri != t.classification.priority:
            t.classification.priority = new_pri
            t.classification.priority_reason = reason or t.classification.priority_reason
            changed += 1
    ctx.triaged.sort(key=lambda t: (t.classification.priority, t.email.received_at))
    ctx.stats["priority_overrides"] = changed
    ctx.say(f"applied {changed} deterministic priority overrides")


# ------------------------------------------------------------------ calendar + draft
def _compute_slots(ctx: RunContext) -> tuple[list[str], list]:
    cfg = ctx.rules.calendar
    tz = ctx.rules.timezone
    slots = gcal.propose_slots(cfg, tz)
    label = ZoneInfo(tz).key
    return [s.human(label) for s in slots], slots


def step_calendar(ctx: RunContext) -> None:
    if not any(t.classification.needs_appointment for t in ctx.triaged):
        ctx.say("no appointment requests; skipping calendar lookup")
        ctx.stats["slots_found"] = 0
        return
    strings, slots = _compute_slots(ctx)
    ctx.stats["slots_found"] = len(slots)
    ctx.slot_strings, ctx.slots = strings, slots
    ctx.say(f"found {len(slots)} free slots: {'; '.join(strings)}")


def _draft_one(ctx: RunContext, t: TriagedEmail) -> DraftProposal | None:
    slots = ctx.slot_strings if t.classification.needs_appointment else []
    system = prompts.draft_system(ctx.rules, t.classification.language, slots or None)
    raw = ctx.provider.complete_json(
        system, prompts.draft_user(t.email, t.classification.model_dump()), max_tokens=1200
    )
    body = (raw.get("body") or "").strip()
    if not body:
        return None
    caveats = (raw.get("caveats") or "").strip()
    if caveats:
        body += f"\n\n---\n[review note] {caveats}"
    return DraftProposal(
        email_message_id=t.email.message_id,
        kind="appointment" if t.classification.needs_appointment else "reply",
        subject=raw.get("subject") or f"Re: {t.email.subject}",
        body=body,
        slots=ctx.slots if t.classification.needs_appointment else [],
        provider=ctx.provider.name,
        model=ctx.provider.model,
    )


def step_draft(ctx: RunContext) -> None:
    allowed = set(ctx.rules.drafting.get("enabled_for_categories", []))
    targets = [t for t in ctx.triaged
               if t.classification.category in allowed and t.classification.priority != "P4"]
    workers = 1 if ctx.provider.name == "ollama" else int(
        ctx.spec.params.get("workers", DEFAULT_WORKERS)
    )
    made = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_draft_one, ctx, t): t for t in targets}
        for fut, t in futures.items():
            try:
                d = fut.result()
            except Exception as exc:  # noqa: BLE001
                ctx.say(f"draft failed for {t.email.subject[:50]!r}: {exc}")
                continue
            if d:
                t.draft = d
                made += 1
    ctx.stats["drafts_proposed"] = made
    ctx.say(f"proposed {made} drafts (staged for your approval, nothing sent)")


# ----------------------------------------------------------------------- persistence
def step_persist(ctx: RunContext) -> None:
    for t in ctx.triaged:
        email_id = db.insert_email(ctx.run_id, {
            "account": t.email.account,
            "message_id": t.email.message_id,
            "thread_id": t.email.thread_id,
            "sender": t.email.sender,
            "subject": t.email.subject,
            "received_at": t.email.received_at.isoformat(),
            "category": t.classification.category,
            "priority": t.classification.priority,
            "priority_reason": t.classification.priority_reason,
            "needs_appointment": t.classification.needs_appointment,
            "summary": t.classification.summary,
            "web_link": t.email.web_link,
        })
        if t.draft:
            db.insert_draft(email_id, {
                "kind": t.draft.kind,
                "subject": t.draft.subject,
                "body": t.draft.body,
                "slots": [s.model_dump(mode="json") for s in t.draft.slots],
                "provider": t.draft.provider,
                "model": t.draft.model,
            })


# ---------------------------------------------------------------------------- sheets
def step_sheets(ctx: RunContext) -> None:
    cfg = ctx.rules.sheets
    sid = cfg.get("spreadsheet_id")
    if not sid:
        ctx.say("sheets.spreadsheet_id is empty in rules.yaml; skipping recap")
        return
    tab = cfg.get("tab_name", "Inbox Recap")
    header = cfg.get("columns", [])
    gsheets.ensure_tab(sid, tab, header)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [[
        now,
        t.email.received_at.isoformat(timespec="seconds"),
        t.email.sender,
        t.email.subject,
        t.classification.category,
        t.classification.priority,
        "yes" if t.classification.needs_appointment else "no",
        t.classification.summary,
        "proposed" if t.draft else "none",
        t.email.web_link or "",
    ] for t in ctx.triaged]
    n = gsheets.append_rows(sid, tab, rows)
    ctx.stats["sheet_rows"] = n
    ctx.say(f"appended {n} rows to '{tab}'")


STEPS = {
    "fetch": step_fetch,
    "classify": step_classify,
    "prioritize": step_prioritize,
    "calendar": step_calendar,
    "draft": step_draft,
    "sheets": step_sheets,
}
