"""Prompt construction. Kept separate so prompts can be versioned and diffed
independently of the orchestration code - they are the part that will change
most often as the rules evolve."""
from __future__ import annotations

import json
from typing import Any

from ..models import EmailRef
from ..rules import Rules

CLASSIFY_SYSTEM = """You triage the inbox of {name} ({role}).
Classify one email. Be conservative: when unsure, choose the lower priority and
set needs_appointment=false.

Categories (choose exactly one id):
{categories}

Priority levels:
{levels}

Output JSON with exactly these keys:
  category            string, one of the category ids above
  priority            one of P1, P2, P3, P4
  priority_reason     <=20 words, the concrete evidence for the level
  needs_appointment   true only if the sender explicitly asks to meet, call,
                      schedule, reschedule or confirm a time
  appointment_hint    string or null - constraints the sender stated
                      (e.g. "prefers next week, mornings, 45 min")
  summary             <=30 words, what the sender wants and what is owed
  language            ISO 639-1 code of the email body
  confidence          0.0-1.0
"""

DRAFT_SYSTEM = """You write reply drafts for {name} ({role}).
Rules:
- Write in {language}. Match the register of the incoming email.
- Tone: {tone}. Hard limit {max_words} words.
- Never invent facts, commitments, prices, or dates that were not supplied.
- If information is missing to answer properly, say plainly what you need.
- Do not apologize for delays unless the email complains about one.
- Sign as "{signature}". No subject line inside the body.
{slot_clause}
Output JSON with exactly these keys:
  subject   string - the reply subject line
  body      string - the full reply body, newlines allowed
  caveats   string - anything {name} must verify before sending; "" if none
"""

SLOT_CLAUSE = """- The sender asked for a meeting. Offer EXACTLY these free slots,
  verbatim, as a short list, and ask them to pick one:
{slots}
- State the time zone ({tz}) once.
"""


def classify_system(rules: Rules) -> str:
    cats = "\n".join(f"  - {c['id']}: {c['description']}" for c in rules.categories)
    levels = "\n".join(f"  - {k}: {v}" for k, v in rules.priority_levels.items())
    ident = rules.identity
    return CLASSIFY_SYSTEM.format(
        name=ident.get("full_name", "the user"),
        role=ident.get("role", ""),
        categories=cats,
        levels=levels,
    )


def classify_user(email: EmailRef) -> str:
    return json.dumps({
        "from": f"{email.sender_name or ''} <{email.sender}>".strip(),
        "to": email.to,
        "subject": email.subject,
        "received_at": email.received_at.isoformat(),
        "labels": email.labels,
        "body": email.body_text,
    }, ensure_ascii=False)


def draft_system(rules: Rules, language: str, slots: list[str] | None) -> str:
    ident = rules.identity
    d = rules.drafting
    slot_clause = ""
    if slots:
        slot_clause = SLOT_CLAUSE.format(
            slots="\n".join(f"    * {s}" for s in slots), tz=rules.timezone
        )
    return DRAFT_SYSTEM.format(
        name=ident.get("full_name", "the user"),
        role=ident.get("role", ""),
        language=language,
        tone=d.get("tone", "professional and concise"),
        max_words=d.get("max_words", 180),
        signature=ident.get("signature", ident.get("full_name", "")),
        slot_clause=slot_clause,
    )


def draft_user(email: EmailRef, classification: dict[str, Any]) -> str:
    return json.dumps({
        "from": f"{email.sender_name or ''} <{email.sender}>".strip(),
        "subject": email.subject,
        "body": email.body_text,
        "category": classification.get("category"),
        "what_they_want": classification.get("summary"),
        "appointment_hint": classification.get("appointment_hint"),
    }, ensure_ascii=False)
