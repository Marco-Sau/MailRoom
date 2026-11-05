"""SQLite persistence. Single-writer, local-only; WAL keeps the UI readable
while a run is in flight."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .settings import settings

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      TEXT NOT NULL,
  job_name    TEXT NOT NULL,
  runner      TEXT NOT NULL,
  provider    TEXT,
  model       TEXT,
  status      TEXT NOT NULL,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  stats_json  TEXT NOT NULL DEFAULT '{}',
  log_json    TEXT NOT NULL DEFAULT '[]',
  error       TEXT
);

CREATE TABLE IF NOT EXISTS emails (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id           INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  account          TEXT NOT NULL,
  message_id       TEXT NOT NULL,
  thread_id        TEXT,
  sender           TEXT,
  subject          TEXT,
  received_at      TEXT,
  category         TEXT,
  priority         TEXT,
  priority_reason  TEXT,
  needs_appointment INTEGER DEFAULT 0,
  summary          TEXT,
  web_link         TEXT,
  UNIQUE(run_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_emails_run ON emails(run_id);
CREATE INDEX IF NOT EXISTS idx_emails_msg ON emails(message_id);

CREATE TABLE IF NOT EXISTS drafts (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  email_id       INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
  kind           TEXT NOT NULL,
  subject        TEXT,
  body           TEXT NOT NULL,
  slots_json     TEXT NOT NULL DEFAULT '[]',
  provider       TEXT,
  model          TEXT,
  status         TEXT NOT NULL DEFAULT 'proposed',
  gmail_draft_id TEXT,
  created_at     TEXT NOT NULL,
  decided_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);

-- Append-only audit of every side effect that leaves the machine.
CREATE TABLE IF NOT EXISTS recaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    doc_url TEXT,
    email_count INTEGER NOT NULL DEFAULT 0,
    items_json TEXT NOT NULL DEFAULT '[]',
    run_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS audit (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  at        TEXT NOT NULL,
  actor     TEXT NOT NULL,
  action    TEXT NOT NULL,
  target    TEXT,
  detail    TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as c:
        c.executescript(SCHEMA)


def start_run(job_id: str, job_name: str, runner: str, provider: str | None,
              model: str | None) -> int:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO runs(job_id, job_name, runner, provider, model, status, started_at)"
            " VALUES (?,?,?,?,?,'running',?)",
            (job_id, job_name, runner, provider, model, utcnow()),
        )
        return int(cur.lastrowid)


def finish_run(run_id: int, status: str, stats: dict[str, Any],
               log: list[str], error: str | None = None) -> None:
    with connect() as c:
        c.execute(
            "UPDATE runs SET status=?, finished_at=?, stats_json=?, log_json=?, error=?"
            " WHERE id=?",
            (status, utcnow(), json.dumps(stats), json.dumps(log[-500:]), error, run_id),
        )


def mark_interrupted(keep: set[int], reason: str) -> list[dict[str, Any]]:
    """Runs still marked 'running' whose process is gone (closed app, killed background job)."""
    with connect() as c:
        rows = [dict(r) for r in c.execute("SELECT id, job_id, job_name, started_at FROM runs WHERE status='running'")
                if r["id"] not in keep]
        for r in rows:
            c.execute("UPDATE runs SET status='interrupted', finished_at=?, error=? WHERE id=? AND status='running'",
                      (utcnow(), reason, r["id"]))
    return rows


def update_run_progress(run_id: int, stats: dict[str, Any], log: list[str]) -> None:
    """Live progress for a run that is still going (status stays 'running')."""
    with connect() as c:
        c.execute("UPDATE runs SET stats_json=?, log_json=? WHERE id=?",
                  (json.dumps(stats), json.dumps(log[-500:]), run_id))


def insert_email(run_id: int, e: dict[str, Any]) -> int:
    with connect() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO emails(run_id, account, message_id, thread_id, sender,"
            " subject, received_at, category, priority, priority_reason, needs_appointment,"
            " summary, web_link) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, e["account"], e["message_id"], e.get("thread_id"), e.get("sender"),
             e.get("subject"), e.get("received_at"), e.get("category"), e.get("priority"),
             e.get("priority_reason"), int(bool(e.get("needs_appointment"))),
             e.get("summary"), e.get("web_link")),
        )
        if cur.lastrowid:
            return int(cur.lastrowid)
        row = c.execute(
            "SELECT id FROM emails WHERE run_id=? AND message_id=?", (run_id, e["message_id"])
        ).fetchone()
        return int(row["id"])


def insert_draft(email_id: int, d: dict[str, Any]) -> int:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO drafts(email_id, kind, subject, body, slots_json, provider, model,"
            " status, created_at) VALUES (?,?,?,?,?,?,?,'proposed',?)",
            (email_id, d["kind"], d.get("subject"), d["body"],
             json.dumps(d.get("slots", [])), d.get("provider"), d.get("model"), utcnow()),
        )
        return int(cur.lastrowid)


def set_draft_status(draft_id: int, status: str, gmail_draft_id: str | None = None) -> None:
    with connect() as c:
        c.execute(
            "UPDATE drafts SET status=?, gmail_draft_id=COALESCE(?, gmail_draft_id),"
            " decided_at=? WHERE id=?",
            (status, gmail_draft_id, utcnow(), draft_id),
        )


def audit(actor: str, action: str, target: str | None = None, detail: str | None = None) -> None:
    with connect() as c:
        c.execute("INSERT INTO audit(at, actor, action, target, detail) VALUES (?,?,?,?,?)",
                  (utcnow(), actor, action, target, detail))


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as c:
        rows = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_run(run_id: int) -> dict[str, Any] | None:
    with connect() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(r) if r else None


def run_emails(run_id: int) -> list[dict[str, Any]]:
    with connect() as c:
        rows = c.execute(
            "SELECT e.*, d.id AS draft_id, d.kind AS draft_kind, d.status AS draft_status,"
            " d.body AS draft_body, d.subject AS draft_subject, d.slots_json"
            " FROM emails e LEFT JOIN drafts d ON d.email_id = e.id"
            " WHERE e.run_id=?"
            " ORDER BY CASE e.priority WHEN 'P1' THEN 1 WHEN 'P2' THEN 2"
            "          WHEN 'P3' THEN 3 ELSE 4 END, e.received_at DESC",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def pending_drafts() -> list[dict[str, Any]]:
    with connect() as c:
        rows = c.execute(
            "SELECT d.*, e.message_id, e.thread_id, e.sender, e.subject AS email_subject,"
            " e.account FROM drafts d JOIN emails e ON e.id=d.email_id"
            " WHERE d.status='proposed' ORDER BY d.id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_draft(draft_id: int) -> dict[str, Any] | None:
    with connect() as c:
        r = c.execute(
            "SELECT d.*, e.message_id, e.thread_id, e.sender, e.subject AS email_subject,"
            " e.account FROM drafts d JOIN emails e ON e.id=d.email_id WHERE d.id=?",
            (draft_id,),
        ).fetchone()
        return dict(r) if r else None


def save_recap(source: str, title: str, generated_at: str, doc_url: str, count: int, items_json: str,
               run_json: str = "{}") -> int:
    """Local copy, used only when the shared n8n Recaps table cannot be written."""
    with connect() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(recaps)")}
        if "run_json" not in cols:                       # databases created before run summaries
            c.execute("ALTER TABLE recaps ADD COLUMN run_json TEXT NOT NULL DEFAULT '{}'")
        cur = c.execute("INSERT INTO recaps(source, title, generated_at, doc_url, email_count, items_json, run_json)"
                        " VALUES (?,?,?,?,?,?,?)", (source, title, generated_at, doc_url, count, items_json, run_json))
        return int(cur.lastrowid)


def list_recaps(limit: int = 200) -> list[dict[str, Any]]:
    with connect() as c:
        rows = c.execute("SELECT * FROM recaps ORDER BY generated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_recap(recap_id: int) -> dict[str, Any] | None:
    with connect() as c:
        r = c.execute("SELECT * FROM recaps WHERE id=?", (recap_id,)).fetchone()
        return dict(r) if r else None
