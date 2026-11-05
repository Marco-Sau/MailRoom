"""Wire/domain models shared by the pipeline, storage and the HTTP API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Priority = Literal["P1", "P2", "P3", "P4"]
DraftKind = Literal["reply", "appointment"]
DraftStatus = Literal["proposed", "approved", "discarded", "failed"]
RunStatus = Literal["running", "succeeded", "failed", "cancelled"]


class EmailRef(BaseModel):
    """A normalized message, independent of Gmail vs IMAP."""
    account: str
    message_id: str
    thread_id: str | None = None
    sender: str
    sender_name: str | None = None
    to: list[str] = Field(default_factory=list)
    subject: str = ""
    received_at: datetime
    snippet: str = ""
    body_text: str = ""
    labels: list[str] = Field(default_factory=list)
    web_link: str | None = None


class Classification(BaseModel):
    category: str
    priority: Priority
    priority_reason: str = ""
    needs_appointment: bool = False
    appointment_hint: str | None = None
    summary: str = ""
    language: str = "en"
    confidence: float = 0.0


class TimeSlot(BaseModel):
    start: datetime
    end: datetime

    def human(self, tz_label: str = "") -> str:
        d = self.start.strftime("%a %d %b")
        return f"{d}, {self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')} {tz_label}".strip()


class DraftProposal(BaseModel):
    email_message_id: str
    kind: DraftKind
    subject: str
    body: str
    slots: list[TimeSlot] = Field(default_factory=list)
    model: str = ""
    provider: str = ""


class TriagedEmail(BaseModel):
    email: EmailRef
    classification: Classification
    draft: DraftProposal | None = None


class RunSpec(BaseModel):
    job_id: str
    name: str
    runner: Literal["pipeline", "n8n", "assistant"]
    provider: str | None = None
    model: str | None = None
    account: str = "gmail-personal"
    steps: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    webhook_url: str | None = None
    auth: Literal["key", "header", "none", "basic"] = "none"
    #: look-back window chosen on the Run tab (1-168 h); None = standard (replies 48 h, events/recap 24 h)
    hours: int | None = Field(default=None, ge=1, le=168)


class RunResult(BaseModel):
    run_id: int
    job_id: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None = None
    stats: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    log: list[str] = Field(default_factory=list)
