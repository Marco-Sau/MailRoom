"""Email Assistant (Claude / ChatGPT / Gemini / Ollama): the n8n Gmail Assistant,
run by the app, with the chosen model doing only the thinking.

Same three phases as the n8n orchestrator, in the same order:

1. Drafting - reply drafts for tracked senders (or the whole inbox), 48 h lookback;
   meeting requests get free slots computed from your busy calendars; a reply
   that confirms a proposed slot gets a BOOK / DO NOT BOOK note.
2. Events   - webinar / course / event emails of the last 24 h are checked
   against your calendars; FREE or CONFLICT note with add/ignore or keep/swap.
3. Recap    - the last 24 h of mail, ranked by priority, in a Google Doc.

State is shared with n8n (Inbox Pilot Settings, Tracked Senders, Processed
Sender Emails, Inbox Pilot Decisions), so an email handled here is not handled
again by n8n, and the BOOK/ADD/KEEP/SWAP links are answered by n8n's Decisions
workflow exactly as before. Nothing is ever sent: drafts only.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .. import app_settings, db
from ..assistant import google_io, logic, prompts
from ..assistant.logic import ZONE
from ..connectors import google_auth, n8n_tables
from ..models import RunResult, RunSpec
from ..providers import get_provider
from ..providers.base import LLMProvider, ProviderError
from ..rules import Rules
from . import control
from .base import Runner

def _decide_url() -> str:
    return app_settings.local("decisionUrl")      # Settings → Connections


def _recap_folder() -> str | None:
    return app_settings.local("recapFolderId") or None
FALLBACK_BUSY = ["primary"]
PHASES = ("drafting", "events", "recap")
ENGINE_LABEL = {"claude_cli": "Claude", "codex_cli": "ChatGPT", "gemini_cli": "Gemini", "ollama": "Ollama",
                "anthropic": "Claude API", "openai": "OpenAI API", "gemini": "Gemini API"}


def _with_addresses(query: str, st: dict[str, str]) -> str:
    """Append the 'Your addresses' filter (same string the n8n workflows append)."""
    extra = (st.get("addressQuery") or "").strip()
    return f"{query} {extra}".strip() if extra else query


class Ctx:
    """Run log + stats, pushed to the app every time something happens."""

    def __init__(self, run_id: int, provider: LLMProvider) -> None:
        self.run_id, self.provider = run_id, provider
        self.log: list[str] = []
        self.stats: dict[str, Any] = {"engine": provider.name, "model": provider.model, "llm_calls": 0,
                                      "emails_checked": 0, "reply_drafts": 0, "meeting_proposals": 0,
                                      "booking_notes": 0, "event_notes": 0, "recap_url": "", "errors": 0,
                                      "model_errors": 0}
        self.now = datetime.now(ZONE)
        self._served_said = ""
        self.hours: int | None = None        # Run-tab window; None = the standard windows in Settings
        self.st: dict[str, str] = {}          # shared settings rows (n8n Settings table) for this run

    def window(self, key: str) -> int:
        return self.hours or app_settings.shared(self.st, key)

    def cfg(self, key: str) -> Any:
        return app_settings.shared(self.st, key)

    def _say_served(self) -> None:
        sv = getattr(self.provider, "served", None) or {}
        key = f"{sv.get('model')}|{sv.get('effort')}"
        if sv.get("model") and key != self._served_said:
            self._served_said = key
            self.stats["served_model"] = sv["model"]
            self.say(f"model reported by the engine: {sv['model']}"
                     + (f" · effort {sv['effort']}" if sv.get("effort") else ""))

    def say(self, msg: str) -> None:
        self.log.append(f"[{datetime.now(ZONE):%H:%M:%S}] {msg}")
        self.push()

    def push(self) -> None:
        self.stats["tokens_in"] = self.provider.usage.input_tokens
        self.stats["tokens_out"] = self.provider.usage.output_tokens
        try:
            db.update_run_progress(self.run_id, self.stats, self.log)
        except Exception:  # noqa: BLE001 - progress is best-effort
            pass

    def llm_json(self, pair: tuple[str, str], max_tokens: int) -> dict[str, Any]:
        control.check()
        self.stats["llm_calls"] += 1
        try:
            out = self.provider.complete_json(pair[0], pair[1], max_tokens=max_tokens)
            if not isinstance(out, dict):
                raise ProviderError("model did not return a JSON object")
        except control.Stopped:
            raise
        except Exception:
            self.stats["model_errors"] += 1
            raise
        finally:
            self._notes()
        self._say_served()
        return out

    def llm_text(self, pair: tuple[str, str], max_tokens: int) -> str:
        control.check()
        self.stats["llm_calls"] += 1
        try:
            out = self.provider.complete(pair[0], pair[1], max_tokens=max_tokens, temperature=0.2)
        except control.Stopped:
            raise
        except Exception:
            self.stats["model_errors"] += 1
            raise
        finally:
            self._notes()
        self._say_served()
        return out.strip()

    def _notes(self) -> None:
        for n in getattr(self.provider, "notes", []) or []:
            self.say("note: " + n)
        if getattr(self.provider, "notes", None):
            self.provider.notes.clear()


class Busy:
    """Events of the busy calendars (+ extra ids), read once per calendar per run."""

    def __init__(self, st: dict[str, str], now: datetime) -> None:
        self.st, self.now = st, now
        self.names = {c["id"]: c.get("name") or c["id"] for c in logic.pj(st.get("calendars"), [])
                      if isinstance(c, dict) and c.get("id")}
        busy = logic.pj(st.get("busyCalendars"), None)
        self.base = [str(x).strip() for x in (busy if isinstance(busy, list) and busy else FALLBACK_BUSY) if str(x).strip()]
        self.cache: dict[str, tuple[int, list[dict[str, Any]] | None, str]] = {}

    def name(self, cid: str) -> str:
        return self.names.get(cid) or ("Primary" if cid == "primary" else cid)

    def _read(self, cid: str, days: int) -> tuple[list[dict[str, Any]] | None, str]:
        have = self.cache.get(cid)
        if have and have[0] >= days:
            return have[1], have[2]
        try:
            evs = google_io.calendar_events(cid, self.now, self.now + timedelta(days=days))
            for e in evs:
                e["__calendarId"], e["__calendarName"] = cid, self.name(cid)
            self.cache[cid] = (days, evs, "")
            return evs, ""
        except Exception as exc:  # noqa: BLE001 - one unreadable calendar = refuse to propose
            self.cache[cid] = (days, None, str(exc)[:160])
            return None, str(exc)[:160]

    def events(self, days: int, extra: list[str] | None = None) -> tuple[list[dict[str, Any]], bool, list[str]]:
        ids: list[str] = []
        for cid in self.base + [x.strip() for x in (extra or []) if x and x.strip()]:
            if cid not in ids:
                ids.append(cid)
        out, failed = [], []
        for cid in ids:
            evs, err = self._read(cid, days)
            if evs is None:
                failed.append(f"{self.name(cid)} ({err})")
            else:
                out += [e for e in evs if (logic.parse_dt((e.get("start") or {}).get("dateTime"))
                                           or self.now) <= self.now + timedelta(days=days)]
        return out, not failed, failed


class AssistantRunner(Runner):
    name = "assistant"

    def __init__(self, provider_factory: Callable[[str, str | None], LLMProvider] | None = None) -> None:
        self._factory = provider_factory or (lambda n, m: get_provider(n, m))

    # ------------------------------------------------------------------ run
    def run(self, spec: RunSpec) -> RunResult:
        engine = spec.provider or "claude_cli"
        provider = self._factory(engine, spec.model)
        run_id = db.start_run(spec.job_id, spec.name, "assistant", provider.name, provider.model)
        started = datetime.now(timezone.utc)
        ctx = Ctx(run_id, provider)
        ctx.hours = spec.hours
        control.begin(run_id, "assistant")
        phases = [p for p in (spec.steps or PHASES) if p in PHASES]
        status, error = "succeeded", None
        try:
            eff = getattr(provider, "effort", None)
            ctx.say(f"MailRoom · {ENGINE_LABEL.get(engine, engine)} ({provider.model}{', effort ' + eff if eff else ''}) thinks; the app does "
                    f"every Gmail, Calendar and Docs action. Nothing is sent.")
            provider.preflight()               # e.g. an Ollama model that is not installed: stop now
            if spec.hours:
                ctx.say(f"look-back window: the last {logic.span(spec.hours)} for replies, events and recap")
            g = google_auth.status()
            if not g.get("connected"):
                raise RuntimeError("Google is not connected to this app (n8n has its own Google login; the app "
                                   "needs one too). Settings → Advanced → Secrets: add the Google OAuth desktop client JSON, "
                                   "then Settings → Advanced → Google account → Connect / re-authorize Google.")
            if g.get("missing_scopes"):
                ctx.say("note: Google access is missing " + ", ".join(x.rsplit("/", 1)[-1] for x in g["missing_scopes"])
                        + " - Settings → Advanced → Google account → Connect / re-authorize Google")
            base = n8n_tables._base(Rules.load())
            st = logic.settings_from_rows(n8n_tables.rows("settings", base))
            ctx.st = st
            if st.get("addressQuery"):
                ctx.say("address filter: " + st["addressQuery"])
            ctx.say(f"settings: reply scope = {'whole inbox' if st.get('draftScope') == 'inbox' else 'tracked senders'}, "
                    f"max {st.get('maxEmails') or 25} emails, sign as {st.get('signatureName') or '(name not set)'}")
            if not _decide_url():
                ctx.say("warning: the decision page webhook is not set, so links in BOOK? and event notes will not "
                        "work (Settings → Advanced → Connections)")
            busy = Busy(st, ctx.now)
            failures = []
            for phase in phases:
                control.check()
                try:
                    {"drafting": self.drafting, "events": self.events, "recap": self.recap}[phase](ctx, base, st, busy)
                except control.Stopped:
                    raise
                except Exception as exc:  # noqa: BLE001 - like n8n: a failed phase does not stop the next
                    ctx.stats["errors"] += 1
                    failures.append(phase)
                    ctx.say(f"{phase.upper()} FAILED: {exc}")
            if failures:
                status, error = "failed", "phase failed: " + ", ".join(failures)
            elif ctx.stats["llm_calls"] and ctx.stats["model_errors"] == ctx.stats["llm_calls"]:
                status, error = "failed", "the model failed on every call (see the log)"
                ctx.say("FAILED: the model failed on every call, so nothing was ranked or drafted.")
            ctx.say("done. " + self._summary(ctx))
        except control.Stopped:
            status, error = "cancelled", "stopped by you"
            ctx.say("STOPPED by you. Drafts already created stay in Gmail Drafts; nothing was sent.")
        except Exception as exc:  # noqa: BLE001
            status, error = "failed", str(exc)
            ctx.say(f"FAILED: {exc}")
        finally:
            control.end()
        ctx.push()
        db.finish_run(run_id, status, ctx.stats, ctx.log, error)
        db.audit("assistant", f"run_{status}", str(run_id), (error or spec.name)[:500])
        return RunResult(run_id=run_id, job_id=spec.job_id, status=status, started_at=started,
                         finished_at=datetime.now(timezone.utc), stats=ctx.stats, error=error, log=ctx.log)

    @staticmethod
    def _summary(ctx: Ctx) -> str:
        s = ctx.stats
        out = (f"{s['emails_checked']} emails checked · {s['reply_drafts']} reply drafts "
               f"({s['meeting_proposals']} with proposed times) · {s['booking_notes']} BOOK notes · "
               f"{s['event_notes']} event notes · {s['llm_calls']} model calls")
        if s.get("tokens_in") or s.get("tokens_out"):
            out += f" · {s.get('tokens_in', 0)} in / {s.get('tokens_out', 0)} out tokens"
        return out

    # ------------------------------------------------------------- drafting
    def drafting(self, ctx: Ctx, base: str, st: dict[str, str], busy: Busy) -> None:
        ctx.say("— DRAFTING —")
        senders = [s for s in n8n_tables.rows("senders", base)
                   if s.get("matchPatterns") and str(s.get("active")).lower() not in ("false", "0", "none")]
        q = logic.build_query(st, senders)
        if not q:
            self._follow_up_drafts(ctx, base)
            ctx.say("no active tracked senders: nothing to draft (add senders in Settings, or choose Whole inbox)")
            return
        hrs = ctx.window("lookbackDrafting")
        msgs = google_io.search(q["gmailQuery"], ctx.now - timedelta(hours=hrs), q["maxEmails"])
        ctx.say(f"{len(msgs)} emails in the last {logic.span(hrs)} match"
                + (f" {len(senders)} tracked senders" if q["scope"] == "senders" else " the inbox filter"))
        if q["scope"] == "inbox":
            kept = [m for m in msgs if not logic.is_bulk(m)]
            if len(kept) != len(msgs):
                ctx.say(f"dropped {len(msgs) - len(kept)} bulk/automated emails")
            msgs = kept
        processed = self._follow_up_drafts(ctx, base)
        done = logic.handled_ids(processed)
        todo = [m for m in msgs if m["id"] not in done]
        if len(todo) != len(msgs):
            ctx.say(f"skipped {len(msgs) - len(todo)} already handled (by n8n or an earlier run)")
        redo = {str(r.get("trackedGmailId")) for r in processed if r.get("draftStatus") == "deleted"}  # for the log
        for m in todo:
            if m["id"] in redo:
                ctx.say(f"   re-drafting: {str(m.get('subject'))[:60]} (previous draft deleted, never sent)")
                ctx.stats["redrafted"] = ctx.stats.get("redrafted", 0) + 1
        if not todo:
            ctx.say("no new emails to draft")
            return
        for n, m in enumerate(todo, 1):
            control.check()
            j = logic.attach_context(m, processed, senders, st)
            who = (j.get("from") or {}).get("text") or "?"
            ctx.say(f"[{n}/{len(todo)}] {who[:60]} · {str(j.get('subject'))[:70]}")
            ctx.stats["emails_checked"] += 1
            try:
                row = self._draft_one(ctx, j, st, busy, processed)
            except control.Stopped:
                raise
            except Exception as exc:  # noqa: BLE001 - not marked processed, so the next run retries it
                ctx.stats["errors"] += 1
                ctx.say(f"   could not handle it ({exc}); it will be retried next run")
                continue
            n8n_tables.insert("processed", base, row)
            processed.append(row)         # holds the offered slots for the rest of this run

    def _follow_up_drafts(self, ctx: Ctx, base: str) -> list[dict[str, Any]]:
        """Check what happened to earlier reply drafts: still waiting, sent, or deleted."""
        processed = n8n_tables.rows("processed", base)
        try:
            drafts = google_io.draft_summaries()
            sent = google_io.sent_messages(ctx.now - timedelta(days=ctx.cfg("followUpDays") + 1))
        except Exception as exc:  # noqa: BLE001 - drafting still works without the follow-up
            ctx.say(f"draft follow-up skipped ({str(exc)[:120]})")
            return processed
        changes = logic.draft_check(processed, drafts, sent, ctx.now, ctx.cfg("followUpDays"))
        by_id = {r.get("id"): r for r in processed}
        for c in changes:
            data = {k: v for k, v in c.items() if k != "id"}
            n8n_tables.update("processed", base, c["id"], data)
            if c["id"] in by_id:
                by_id[c["id"]].update(data)
        o = logic.run_summary(processed, [], ctx.now, ctx.now, days=ctx.cfg("followUpDays"))
        ctx.say(f"earlier drafts (7 days): {o['draftsReady']} waiting to be sent, {o['draftsSent']} sent, "
                f"{o['draftsDeleted']} deleted without sending" + (f" ({len(changes)} updated)" if changes else ""))
        return processed

    def _draft_one(self, ctx: Ctx, j: dict[str, Any], st: dict[str, str], busy: Busy,
                   processed: list[dict[str, Any]]) -> dict[str, Any]:
        o = ctx.llm_json(prompts.classify(j, ctx.now), 2500)
        j["output"] = o
        confirming = o.get("isConfirmingProposal") is True
        appt = o.get("isAppointmentRequest") is True and not confirming
        reply = str(o.get("draftReply") or "").strip()
        status, slots_json = "none", ""

        if confirming:
            idx = o.get("confirmedSlotIndex")
            slots = j.get("pendingSlotsParsed") or []
            chosen = slots[idx] if isinstance(idx, int) and 0 <= idx < len(slots) else None
            if chosen and chosen.get("startISO"):
                status = "awaiting_approval"
                self._booking_note(ctx, {**j, "startISO": chosen["startISO"], "endISO": chosen["endISO"]}, st)
                ctx.say(f"   confirms {chosen.get('label')}: BOOK / DO NOT BOOK note created")
            else:
                ctx.say("   reads as a confirmation, but not of a proposed slot: plain reply")
        elif appt:
            evs, ok, failed = busy.events(76, [j.get("senderCalendarId") or ""])
            if failed:
                ctx.say("   calendars not readable: " + "; ".join(failed))
            j = logic.compute_slots([j], evs, ok, processed, st, ctx.now)[0]
            ctx.say(f"   meeting request ({j['schedulingCategoryLabel']}, {j['meetingMinutes']} min): "
                    f"{j['freeSlotsText']}" + (" [outside the requested window]" if j["slotsRelaxed"] else ""))
            reply = ctx.llm_text(prompts.appointment(j, ctx.now), 1500) or reply
            if j["freeSlots"]:
                status, slots_json = "pending", logic.js_json(j["freeSlots"])
                ctx.stats["meeting_proposals"] += 1

        if not appt and not confirming and o.get("needsReply") is False:
            ctx.say("   no reply needed")
            return self._row(j, "no_reply_needed", "")
        if not appt and not confirming and o.get("refersToEarlier") is True:
            reply = self._reference_reply(ctx, j, o) or reply
        subject = j.get("subject") or "(no subject)"
        subject = subject if subject.lower().startswith("re:") else "Re: " + subject
        to = ((j.get("from") or {}).get("value") or [{}])[0].get("address") or (j.get("from") or {}).get("text", "")
        google_io.create_draft(subject=subject, body=reply or "(No reply generated)", to=to,
                               thread_id=j.get("threadId"), in_reply_to=j.get("messageId") or None)
        ctx.stats["reply_drafts"] += 1
        ctx.say("   reply draft created in Gmail Drafts (not sent)")
        return self._row(j, status, slots_json)

    def _reference_reply(self, ctx: Ctx, j: dict[str, Any], o: dict[str, Any]) -> str:
        """The sender refers to something sent earlier: look for it in Gmail, then draft accordingly."""
        desc = str(o.get("referenceDescription") or "something sent earlier")
        addr = ((j.get("from") or {}).get("value") or [{}])[0].get("address") or ""
        kws = [str(k) for k in (o.get("referenceKeywords") or []) if str(k).strip()][:5]
        if not addr:
            return ""
        try:
            msgs = google_io.search(logic.reference_query(addr), ctx.now - timedelta(days=ctx.cfg("earlierItemsDays")),
                                    ctx.cfg("earlierItemsMaxEmails"))
        except Exception as exc:  # noqa: BLE001 - never tell the sender "not found" when we could not look
            ctx.say(f"   refers to {desc[:80]}, but the mailbox search failed ({str(exc)[:100]}); plain reply")
            return ""
        cands = logic.reference_candidates(msgs, j, kws)
        r = ctx.llm_json(prompts.reference_reply(j, cands, ctx.now, ctx.cfg("earlierItemsDays")), 2500)
        idx = r.get("matchIndex")
        if isinstance(idx, int) and 0 <= idx < len(cands):
            c = cands[idx]
            ctx.say(f"   refers to {desc[:80]}: found {c['name'][:80]} (received {c['at']}"
                    + (", that email is still unread" if c.get("unread") else "") + "); reply confirms it arrived")
            ctx.stats["references_found"] = ctx.stats.get("references_found", 0) + 1
        else:
            ctx.say(f"   refers to {desc[:80]}: not found in the last {ctx.cfg('earlierItemsDays')} days "
                    f"({len(cands)} other items checked); reply asks to resend it")
            ctx.stats["references_missing"] = ctx.stats.get("references_missing", 0) + 1
        return str(r.get("draftReply") or "").strip()

    def _row(self, j: dict[str, Any], status: str, slots_json: str) -> dict[str, Any]:
        return {"trackedGmailId": j["id"], "processedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "threadId": j.get("threadId") or "", "proposedSlotsJson": slots_json,
                "proposalStatus": status, "meetingEventId": "",
                "draftStatus": "" if status == "no_reply_needed" else "ready"}

    def _booking_note(self, ctx: Ctx, j: dict[str, Any], st: dict[str, str]) -> None:
        note = logic.booking_note(j, secrets.token_hex(20), st, _decide_url())
        google_io.create_draft(subject=note["draftSubject"], body=note["draftBody"], thread_id=j.get("threadId"))
        base = n8n_tables._base(Rules.load())
        n8n_tables.insert("decisions", base, {
            "kind": "meeting", "status": "pending", "conflictsJson": "[]", "token": note["token"],
            "gmailId": note["gmailId"], "threadId": note["threadId"], "title": note["title"],
            "sessionsJson": note["sessionsJson"], "counterpartEmail": note["counterpartEmail"],
            "summary": note["summary"], "calendarId": note["calendarId"], "calendarName": note["calendarName"]})
        ctx.stats["booking_notes"] += 1

    # --------------------------------------------------------------- events
    def events(self, ctx: Ctx, base: str, st: dict[str, str], busy: Busy) -> None:
        ctx.say("— EVENTS —")
        hrs = ctx.window("lookbackEvents")
        query = app_settings.event_query(ctx.cfg("eventKeywords"))
        msgs = google_io.search(_with_addresses(query, st), ctx.now - timedelta(hours=hrs), ctx.cfg("eventsMaxEmails"))
        seen = {str(r.get("gmailId")) for r in n8n_tables.rows("decisions", base) if r.get("gmailId")}
        todo = [m for m in msgs if m["id"] not in seen]
        ctx.say(f"{len(msgs)} possible event emails in the last {logic.span(hrs)}, {len(todo)} not checked before")
        for n, m in enumerate(todo, 1):
            control.check()
            ctx.say(f"[{n}/{len(todo)}] {str(m.get('subject'))[:80]}")
            try:
                o, err = ctx.llm_json(prompts.extract_event(m, ctx.now), 2000), False
            except control.Stopped:
                raise
            except Exception as exc:  # noqa: BLE001 - recorded as llm_error, like n8n
                o, err = {}, True
                ctx.stats["errors"] += 1
                ctx.say(f"   model error: {exc}")
            ev = logic.merge_extraction(m, o, err, ctx.now)
            if not ev["isEvent"]:
                ctx.say(f"   {ev['recordStatus'].replace('_', ' ')}")
                n8n_tables.insert("decisions", base, {"kind": "none", "status": ev["recordStatus"],
                                                      "gmailId": ev["gmailId"], "threadId": ev["threadId"],
                                                      "title": ev["title"], "summary": ev["summary"]})
                continue
            evs, ok, failed = busy.events(181)
            if failed:
                ctx.say("   calendars not readable: " + "; ".join(failed))
            ev = logic.detect_conflicts(ev, evs, ok)
            if ev["alreadyInCalendar"]:
                ctx.say("   already in your calendar")
                n8n_tables.insert("decisions", base, {"kind": "none", "status": "already_in_calendar",
                                                      "gmailId": ev["gmailId"], "threadId": ev["threadId"],
                                                      "title": ev["title"], "summary": ev["summary"]})
                continue
            token = secrets.token_hex(20)
            note = logic.event_note(ev, token, _decide_url())
            google_io.create_draft(subject=note["draftSubject"], body=note["draftBody"], thread_id=ev.get("threadId"))
            n8n_tables.insert("decisions", base, {
                "kind": "event", "status": "pending", "token": token, "gmailId": ev["gmailId"],
                "threadId": ev["threadId"], "title": ev["title"], "sessionsJson": note["sessionsJson"],
                "conflictsJson": note["conflictsJson"], "link": ev["link"], "organizer": ev["organizer"],
                "counterpartEmail": ev["fromAddress"], "summary": ev["summary"]})
            ctx.stats["event_notes"] += 1
            ctx.say(f"   {note['state']}: note created in Gmail Drafts (no recipient)")

    # ---------------------------------------------------------------- recap
    @staticmethod
    def _save_recap(ctx: Ctx, base: str, doc: dict[str, Any], url: str) -> None:
        items = logic.js_json(doc["items"])
        row = {"source": ctx.provider.name, "title": doc["title"], "generatedAt": doc["generatedAt"],
               "docUrl": url, "emailCount": doc["count"], "itemsJson": items, "runJson": logic.js_json(doc["summary"])}
        try:
            n8n_tables.insert("recaps", base, row)
            ctx.say("recap saved in MailRoom → Recaps (shared with n8n)")
        except Exception as exc:  # noqa: BLE001 - keep a local copy instead
            db.save_recap(ctx.provider.name, doc["title"], doc["generatedAt"], url, doc["count"], items,
                          logic.js_json(doc["summary"]))
            ctx.say(f"recap saved in MailRoom → Recaps (local copy; n8n table not reachable: {str(exc)[:120]})")

    @staticmethod
    def _rank(ctx: Ctx, batch: list[dict[str, Any]], b: int, n_batches: int,
              total: int) -> tuple[list[dict[str, Any] | None], str | None]:
        """Rank one batch. Returns one entry per email (None = not ranked) and the error, if any.
        Answers are matched by subject first, so a skipped email does not shift every later one."""
        rubric = app_settings.priority_rubric(ctx.cfg)
        lines = []
        for k, m in enumerate(batch):
            try:
                day = datetime.fromtimestamp(int(m.get("internalDate") or 0) / 1000, timezone.utc).strftime("%Y-%m-%d")
            except (TypeError, ValueError):
                day = "unknown"
            snip = " ".join(str(m.get("snippet") or "").split())[:200]
            lines.append(f"{k + 1}. Subject: {m.get('subject') or '(no subject)'}\n"
                         f"From: {(m.get('from') or {}).get('text') or 'unknown'}\nDate: {day}\nSnippet: {snip}")
        try:
            out = ctx.llm_json(prompts.recap_batch(b, n_batches, total, lines, rubric), 5000)
        except control.Stopped:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the emails, unranked
            ctx.say(f"   model error on batch {b}: {exc}")
            return [None] * len(batch), str(exc)
        got = [e for e in (out.get("emails") if isinstance(out.get("emails"), list) else []) if isinstance(e, dict)]
        return logic.match_ranked(batch, got), None

    def recap(self, ctx: Ctx, base: str, st: dict[str, str], busy: Busy) -> None:
        ctx.say("— RECAP —")
        can_doc = google_io.has_scopes(*google_io.DOCS_SCOPES)
        if not can_doc:
            ctx.say("note: no Google Docs permission yet, so the recap is saved in MailRoom only. "
                    "Settings → Advanced → Google account → Connect / re-authorize Google to also get the Doc.")
        rows_p = n8n_tables.rows("processed", base)
        processed = {str(r.get("trackedGmailId")): r for r in rows_p if r.get("trackedGmailId")}   # newest row wins
        try:
            prev = [r.get("generatedAt") for r in n8n_tables.rows("recaps", base) if r.get("generatedAt")]
        except Exception:  # noqa: BLE001
            prev = []
        since = max(prev, key=logic._ms) if prev else (ctx.now - timedelta(hours=24)).isoformat()
        summary = logic.run_summary(rows_p, n8n_tables.rows("decisions", base), since, datetime.now(ZONE),
                                    days=ctx.cfg("followUpDays"), engine=ENGINE_LABEL.get(ctx.provider.name, ctx.provider.name))
        hrs = ctx.window("lookbackRecap")
        cap, size = ctx.cfg("recapMaxEmails"), ctx.cfg("recapBatchSize")
        msgs = google_io.search(_with_addresses("", st), ctx.now - timedelta(hours=hrs), cap, full=False)
        ctx.say(f"{len(msgs)} emails in the last {logic.span(hrs)}"
                + (f" (the recap lists at most {cap}: Settings → Advanced → Look-back windows and limits)" if len(msgs) >= cap else ""))
        batches = [msgs[i:i + size] for i in range(0, len(msgs), size)]
        rows: list[dict[str, Any]] = []
        for b, batch in enumerate(batches, 1):
            control.check()
            ctx.say(f"ranking batch {b}/{len(batches)}")
            ranked, err = self._rank(ctx, batch, b, len(batches), len(msgs))
            missing = [k for k, e in enumerate(ranked) if e is None]
            if missing and len(batch) > 1:
                # Small models drop emails from long batches: ask again for the missing ones only.
                ctx.say(f"   {len(missing)} of {len(batch)} not ranked; asking again for those")
                again, err2 = self._rank(ctx, [batch[k] for k in missing], b, len(batches), len(msgs))
                for k, e in zip(missing, again):
                    ranked[k] = e
                err = err2 or err
            for m, e in zip(batch, ranked):
                if e is None:
                    why = "model error, see the run log" if err else "the model skipped it"
                    e = {"subject": m.get("subject"), "from": (m.get("from") or {}).get("text"),
                         "summary": f"(not ranked: {why})", "priority": 3}
                rows.append({**e, "gmailId": m["id"], "threadId": m.get("threadId") or ""})
        unranked = sum(1 for r in rows if str(r.get("summary", "")).startswith("(not ranked"))
        if unranked:
            ctx.stats["errors"] += 1
            ctx.say(f"   {unranked} of {len(rows)} emails could not be ranked")
        doc = logic.recap_document(rows, processed, ctx.now, summary)
        for line in logic.summary_lines(summary):
            ctx.say("   " + line)
        url = ""
        if can_doc:
            try:
                made = google_io.create_doc(doc["title"], doc["requests"], _recap_folder())
                url = made["url"]
                ctx.stats["recap_url"] = url
                ctx.say(f"recap doc ({doc['count']} emails) saved in {made['where']}: {url}")
            except Exception as exc:  # noqa: BLE001 - the in-app copy below still gets saved
                ctx.stats["errors"] += 1
                ctx.say(f"   could not create the Google Doc ({exc}); the recap is still saved in MailRoom")
        self._save_recap(ctx, base, doc, url)
