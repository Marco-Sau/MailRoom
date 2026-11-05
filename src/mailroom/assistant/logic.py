"""Pure logic of the Gmail Assistant, ported from the n8n Code nodes.

Every function here is deterministic and network-free, so the same decisions
(which emails to read, which times are free, what a decision note says) are
unit-tested once and behave exactly like the n8n workflows.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, Iterable
from urllib.parse import quote
from zoneinfo import ZoneInfo

from ..tz import local_zone

ZONE = local_zone()                 # MAILROOM_TIMEZONE, else this Mac's time zone
CATEGORIES = ("fulltime", "career", "anytime")
LABEL = {"fulltime": "full-time job", "career": "professional career", "anytime": "any time"}
BASE_EXCLUDE = "-in:sent -in:draft -in:chats -from:me"
INBOX_QUERY = ("in:inbox " + BASE_EXCLUDE +
               " -category:promotions -category:social -category:updates -category:forums"
               " -from:noreply -from:no-reply -from:donotreply -from:do-not-reply -from:mailer-daemon"
               " -from:notifications -from:newsletter")
EVENT_QUERY = ('-in:draft -in:sent -in:chats {webinar webinars evento eventi seminario seminari seminar '
               'workshop conference conferenza convegno corso corsi course masterclass meetup "save the date" '
               'iscrizione iscriviti registrazione registration register "live session" diretta "zoom.us" '
               '"teams.microsoft.com" "meet.google.com"}')
NOTE_FOOTER = ("Each link opens a confirmation page. Nothing changes until you press Confirm. "
               "This note has no recipient and cannot be sent by accident.")


def pj(text: Any, fallback: Any) -> Any:
    try:
        v = json.loads(text) if isinstance(text, str) else text
        return fallback if v is None else v
    except (TypeError, ValueError):
        return fallback


def js_json(v: Any) -> str:
    """JSON.stringify output: compact, non-ASCII kept (rows are shared with n8n)."""
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False)


def settings_from_rows(rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    return {str(r["key"]): "" if r.get("value") is None else str(r["value"]) for r in rows if r.get("key")}


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=ZONE)


def fmt_label(d: datetime) -> str:            # 'Monday 5 October, 09:30'
    d = d.astimezone(ZONE)
    return f"{d:%A} {d.day} {d:%B}, {d:%H:%M}"


def fmt_range(a: datetime, b: datetime) -> str:
    a, b = a.astimezone(ZONE), b.astimezone(ZONE)
    head = f"{a:%a} {a.day} {a:%b %Y}, {a:%H:%M}"
    return head + "-" + (f"{b:%H:%M}" if a.date() == b.date() else f"{b:%a} {b.day} {b:%b %H:%M}")


# ---------------------------------------------------------------- busy time
def self_declined(e: dict[str, Any]) -> bool:
    return any(a.get("self") and a.get("responseStatus") == "declined" for a in e.get("attendees") or [])


def busy_interval(e: dict[str, Any]) -> tuple[datetime, datetime, bool] | None:
    if not e or not e.get("start") or e.get("status") == "cancelled" or self_declined(e):
        return None
    if e.get("eventType") in ("workingLocation", "birthday"):
        return None
    st, en = e["start"], e.get("end") or {}
    if st.get("dateTime"):
        if e.get("transparency") == "transparent":
            return None
        s, t = parse_dt(st["dateTime"]), parse_dt(en.get("dateTime"))
        return (s, t, False) if s and t else None
    if st.get("date"):
        s = datetime.fromisoformat(st["date"]).replace(tzinfo=ZONE)
        t = datetime.fromisoformat(en.get("date") or st["date"]).replace(tzinfo=ZONE)
        if t <= s:
            t = s + timedelta(days=1)
        return s, t, True
    return None


# ---------------------------------------------------------------- drafting
def build_query(st: dict[str, str], senders: list[dict[str, Any]]) -> dict[str, Any] | None:
    scope = "inbox" if st.get("draftScope") == "inbox" else "senders"
    try:
        mx = int(st.get("maxEmails") or 25)
    except ValueError:
        mx = 25
    mx = min(50, max(1, mx))
    addr = (" " + st["addressQuery"]) if st.get("addressQuery") else ""
    if scope == "inbox":
        return {"scope": scope, "maxEmails": mx, "gmailQuery": INBOX_QUERY + addr}
    pats: list[str] = []
    for s in senders:
        if not s.get("matchPatterns") or s.get("active") is False or str(s.get("active")).lower() == "false":
            continue
        for p in str(s["matchPatterns"]).split(","):
            p = p.strip().replace('"', "")
            if p and p not in pats:
                pats.append(p)
    if not pats:
        return None
    return {"scope": scope, "maxEmails": mx,
            "gmailQuery": "{" + " ".join(f'from:"{p}"' for p in pats) + "} " + BASE_EXCLUDE + addr}


_BULK_LOCAL = re.compile(r"(^|[._+-])(no-?reply|do-?not-?reply|notifications?|newsletters?|mailer-daemon|bounces?)([._+-]|$)")


def is_bulk(item: dict[str, Any]) -> bool:
    h = item.get("headers") or {}
    if h.get("list-unsubscribe") or h.get("list-id") or re.search(r"bulk|list|junk", str(h.get("precedence", "")), re.I):
        return True
    auto = str(h.get("auto-submitted", ""))
    if auto and not re.search(r":\s*no\s*$", auto, re.I):
        return True
    addr = ((item.get("from") or {}).get("value") or [{}])[0].get("address", "").lower()
    return bool(_BULK_LOCAL.search(addr.split("@")[0]))


def sender_for(email_item: dict[str, Any], senders: list[dict[str, Any]]) -> dict[str, Any] | None:
    frm = str((email_item.get("from") or {}).get("text", "")).lower()
    for s in senders:
        pats = [p.strip().lower() for p in str(s.get("matchPatterns") or "").split(",") if p.strip()]
        if any(p in frm for p in pats):
            return s
    return None


def attach_context(email_item: dict[str, Any], processed: list[dict[str, Any]],
                   senders: list[dict[str, Any]], st: dict[str, str]) -> dict[str, Any]:
    pending = sorted([r for r in processed if r.get("threadId") and r.get("threadId") == email_item.get("threadId")
                      and r.get("proposalStatus") == "pending" and live_row(r)],
                     key=lambda r: str(r.get("processedAt") or r.get("createdAt") or ""), reverse=True)
    slots = pj(pending[0].get("proposedSlotsJson") or "[]", []) if pending else []
    s = sender_for(email_item, senders)
    return {**email_item,
            "hasPendingProposal": bool(slots),
            "pendingSlotsText": "; ".join(x.get("label") for x in slots if x and x.get("label")),
            "pendingSlotsParsed": slots, "proposalStatus": "pending" if slots else "",
            "senderName": st.get("signatureName") or "",
            "senderNotes": (s or {}).get("notes") or st.get("defaultNotes") or "",
            "contactName": (s or {}).get("senderName") or "",
            "draftScope": "inbox" if st.get("draftScope") == "inbox" else "senders",
            "senderCategory": (s or {}).get("category") or st.get("defaultCategory") or "career",
            "senderCalendarId": (s or {}).get("calendarId") or ""}


def body_text(item: dict[str, Any], limit: int = 8000) -> str:
    t = (item.get("text") or "").strip()
    if not t:
        t = re.sub(r"<style[\s\S]*?</style>", " ", item.get("html") or "", flags=re.I)
        t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()[:limit]


# ---------------------------------------------------------------- draft follow-up
REPLY_STATUSES = ("none", "pending", "awaiting_approval", "withdrawn")   # rows for which a reply draft was made
REDRAFTABLE = ("none", "pending", "withdrawn")               # a confirmation (awaiting_approval) is not re-drafted
FINAL_DRAFT = ("sent", "discarded", "superseded")
DEAD_DRAFT = ("deleted", "discarded", "superseded")
FOLLOW_DAYS = 7
DRAFT_LABEL = {"ready": "Draft ready, not sent", "sent": "Sent", "deleted": "Draft deleted, not sent",
               "discarded": "Discarded", "superseded": "Replaced by a newer draft"}


def live_row(r: dict[str, Any]) -> bool:
    """False when the reply was never sent and its draft is gone: its times are not held."""
    return str(r.get("draftStatus") or "") not in DEAD_DRAFT


def _ms(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        d = parse_dt(v)
        return int(d.timestamp() * 1000) if d else 0


def draft_check(rows: list[dict[str, Any]], drafts: list[dict[str, Any]], sent: list[dict[str, Any]],
                now: datetime | None = None, days: int = FOLLOW_DAYS) -> list[dict[str, Any]]:
    """What happened to each reply draft MailRoom made in the last `days` days.

    rows   - Processed Sender Emails rows (id, threadId, processedAt, proposalStatus, draftStatus)
    drafts - Gmail drafts now: [{threadId, subject}]  (decision notes "[Inbox Pilot] ..." are ignored)
    sent   - your sent messages: [{threadId, internalDate (ms)}]
    Returns [{id, draftStatus[, proposalStatus]}] for rows whose status changed. A proposal whose
    draft is gone without being sent becomes "withdrawn", so its times are no longer held.
    """
    now = now or datetime.now(ZONE)
    since = int((now - timedelta(days=days)).timestamp() * 1000)
    draft_threads = {d.get("threadId") for d in drafts
                     if d.get("threadId") and not str(d.get("subject") or "").startswith("[Inbox Pilot]")}
    sent_at: dict[str, list[int]] = {}
    for m in sent:
        if m.get("threadId"):
            sent_at.setdefault(m["threadId"], []).append(_ms(m.get("internalDate")))
    reply_rows = [r for r in rows if r.get("threadId") and r.get("proposalStatus") in REPLY_STATUSES]
    latest: dict[str, Any] = {}
    for r in reply_rows:                       # newest reply row per thread is the live one
        k = (_ms(r.get("processedAt")), int(r.get("id") or 0))
        if r["threadId"] not in latest or k > latest[r["threadId"]][0]:
            latest[r["threadId"]] = (k, r.get("id"))
    out = []
    for r in reply_rows:
        cur = str(r.get("draftStatus") or "")
        at = _ms(r.get("processedAt"))
        if cur in FINAL_DRAFT or at < since:
            continue
        if any(t >= at for t in sent_at.get(r["threadId"], [])):
            new = "sent"
        elif latest[r["threadId"]][1] != r.get("id"):
            new = "superseded"
        elif r["threadId"] in draft_threads:
            new = "ready"
        else:
            new = "deleted"
        if new != cur:
            change = {"id": r.get("id"), "draftStatus": new}
            if new in DEAD_DRAFT and r.get("proposalStatus") == "pending":
                change["proposalStatus"] = "withdrawn"
            out.append(change)
    return out


def handled_ids(rows: list[dict[str, Any]]) -> set[str]:
    """Emails not to draft again: every processed email except those whose newest row says the
    reply draft was deleted without being sent (those get a fresh draft)."""
    newest: dict[str, dict[str, Any]] = {}
    for r in rows:
        g = str(r.get("trackedGmailId") or "")
        if not g:
            continue
        if g not in newest or int(r.get("id") or 0) > int(newest[g].get("id") or 0):
            newest[g] = r
    return {g for g, r in newest.items()
            if not (r.get("draftStatus") == "deleted" and r.get("proposalStatus") in REDRAFTABLE)}


def run_summary(processed: list[dict[str, Any]], decisions: list[dict[str, Any]], since: Any,
                now: datetime | None = None, engine: str = "", days: int = FOLLOW_DAYS) -> dict[str, Any]:
    """What this run did (rows created after `since`) and what is still open."""
    now = now or datetime.now(ZONE)
    t0 = _ms(since) if since else 0
    new = [r for r in processed if _ms(r.get("processedAt")) >= t0]
    week = int((now - timedelta(days=days)).timestamp() * 1000)
    newest: dict[str, dict[str, Any]] = {}
    for r in processed:
        if r.get("threadId") and r.get("proposalStatus") in REPLY_STATUSES and _ms(r.get("processedAt")) >= week:
            if r["threadId"] not in newest or int(r.get("id") or 0) > int(newest[r["threadId"]].get("id") or 0):
                newest[r["threadId"]] = r
    open_rows = list(newest.values())
    dec_new = [d for d in decisions if _ms(d.get("createdAt")) >= t0]
    return {"engine": engine, "at": now.isoformat(timespec="seconds"), "since": str(since or ""), "days": days,
            "newEmails": len(new),
            "draftsCreated": sum(1 for r in new if r.get("proposalStatus") in REPLY_STATUSES),
            "meetingProposals": sum(1 for r in new if r.get("proposalStatus") in ("pending", "withdrawn")),
            "noReplyNeeded": sum(1 for r in new if r.get("proposalStatus") == "no_reply_needed"),
            "bookingNotes": sum(1 for d in dec_new if d.get("kind") == "meeting"),
            "eventNotes": sum(1 for d in dec_new if d.get("kind") == "event"),
            "draftsReady": sum(1 for r in open_rows if r.get("draftStatus") == "ready"),
            "draftsSent": sum(1 for r in open_rows if r.get("draftStatus") == "sent"),
            "draftsDeleted": sum(1 for r in open_rows if r.get("draftStatus") == "deleted"),
            "notesWaiting": sum(1 for d in decisions if d.get("status") == "pending" and d.get("kind") in ("meeting", "event"))}


def summary_lines(s: dict[str, Any]) -> list[str]:
    out = [f"New emails handled: {s['newEmails']}" + (" (nothing new since the last run)" if not s["newEmails"] else ""),
           f"Reply drafts created: {s['draftsCreated']} (with proposed times: {s['meetingProposals']}) | "
           f"no reply needed: {s['noReplyNeeded']}",
           f"Notes created: {s['bookingNotes']} BOOK? | {s['eventNotes']} event",
           f"Last {s.get('days', FOLLOW_DAYS)} days: {s['draftsReady']} drafts ready, not sent | {s['draftsSent']} sent | "
           f"{s['draftsDeleted']} deleted, not sent | {s['notesWaiting']} notes waiting for a decision"]
    return out


# ---------------------------------------------------------------- slots
def _mins(hhmm: str) -> int:
    h, _, m = str(hhmm).partition(":")
    return int(h or 0) * 60 + int(m or 0)


def _ranges(table: dict[str, Any], weekday: int) -> list[list[int]]:
    return [[_mins(a), _mins(b)] for a, b in (table or {}).get(str(weekday), []) if _mins(b) > _mins(a)]


def _subtract(rs: list[list[int]], cuts: list[list[int]]) -> list[list[int]]:
    for w in cuts:
        nxt = []
        for r in rs:
            if w[1] <= r[0] or w[0] >= r[1]:
                nxt.append(r)
                continue
            if w[0] > r[0]:
                nxt.append([r[0], w[0]])
            if w[1] < r[1]:
                nxt.append([w[1], r[1]])
        rs = nxt
    return rs


def _clip(rs: list[list[int]], a: int, b: int) -> list[list[int]]:
    return [[max(r[0], a), min(r[1], b)] for r in rs if min(r[1], b) > max(r[0], a)]


def compute_slots(reqs: list[dict[str, Any]], busy_events: list[dict[str, Any]], busy_ok: bool,
                  processed: list[dict[str, Any]], st: dict[str, str], now: datetime | None = None
                  ) -> list[dict[str, Any]]:
    """Port of n8n 'Compute Free Slots': category windows, shifts, holds, spread."""
    now = (now or datetime.now(ZONE)).astimezone(ZONE)
    hours = {"fulltime": pj(st.get("workHours"), {}), "career": pj(st.get("careerHours"), {}),
             "anytime": pj(st.get("anytimeHours"), {})}
    work_cal = st.get("workCalendar") or ""
    presence_min = float(st.get("presenceMinHours") or 4) * 60
    busy: list[dict[str, Any]] = []
    presence: list[tuple[datetime, datetime]] = []
    for e in busy_events:
        b = busy_interval(e)
        if not b:
            continue
        is_p = bool(work_cal) and e.get("__calendarId") == work_cal and not b[2] and \
            (b[1] - b[0]).total_seconds() / 60 >= presence_min
        if is_p:
            presence.append((b[0], b[1]))
        busy.append({"s": b[0], "e": b[1], "thread": "", "presence": is_p})
    for r in processed:
        if r.get("proposalStatus") != "pending" or not live_row(r):
            continue
        t = parse_dt(r.get("processedAt") or r.get("createdAt"))
        if t and t < now - timedelta(days=7):
            continue
        for x in pj(r.get("proposedSlotsJson") or "[]", []):
            s, e = parse_dt((x or {}).get("startISO")), parse_dt((x or {}).get("endISO"))
            if s and e:
                busy.append({"s": s, "e": e, "thread": r.get("threadId") or "", "presence": False})

    def presence_ranges(day: datetime) -> list[list[int]]:
        fixed = _ranges(hours["fulltime"], day.isoweekday())
        floor = min(r[0] for r in fixed) if fixed else 7 * 60 + 30
        d1 = day + timedelta(days=1)
        out = []
        for ps, pe in presence:
            if ps < d1 and pe > day:
                # wall-clock minutes, so a 09:00 shift stays 09:00 on DST-change days
                a = 0 if ps <= day else _wall(ps)
                b = 24 * 60 if pe >= d1 else _wall(pe)
                if b > max(a, floor):
                    out.append([max(a, floor), b])
        return out

    def windows(cat: str, day: datetime, part: str) -> list[list[int]]:
        if cat == "fulltime":
            rs = presence_ranges(day) or _ranges(hours["fulltime"], day.isoweekday())
        else:
            rs = _ranges(hours.get(cat) or hours["career"], day.isoweekday())
        if cat == "career":
            rs = _subtract(rs, _ranges(hours["fulltime"], day.isoweekday()))
        if part == "morning":
            rs = _clip(rs, 0, 13 * 60)
        if part == "afternoon":
            rs = _clip(rs, 13 * 60, 24 * 60)
        return sorted(rs, key=lambda r: r[0])     # stable, by start only (as in n8n)

    def is_free(s: datetime, e: datetime, thread: str, cat: str) -> bool:
        for b in busy:
            if b["presence"] and cat == "fulltime":
                continue
            if (not b["thread"] or b["thread"] != thread) and s < b["e"] and e > b["s"]:
                return False
        return True

    def earliest() -> datetime:
        t = now + timedelta(hours=2)
        if t.minute == 0 and t.second == 0:
            return t.replace(microsecond=0)
        if t.minute < 30 or (t.minute == 30 and t.second == 0):
            return t.replace(minute=30, second=0, microsecond=0)
        return (t + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

    def search(frm: datetime, to: datetime, cat: str, part: str, dur: int, thread: str,
               mx: int, per_day: int) -> list[tuple[datetime, datetime]]:
        out: list[tuple[datetime, datetime]] = []
        day = frm.astimezone(ZONE).replace(hour=0, minute=0, second=0, microsecond=0)
        k = 0
        while day <= to and len(out) < mx:
            starts = []
            for r in windows(cat, day, part):
                m = r[0]
                while m + dur <= r[1]:
                    starts.append(m)
                    m += 30
            if k % 2 == 1 and len(starts) > 2:
                mid = len(starts) // 2
                starts = starts[mid:] + starts[:mid]
            today: list[tuple[datetime, datetime]] = []
            for m in starts:
                if len(today) >= per_day or len(out) + len(today) >= mx:
                    break
                s = (day + timedelta(minutes=m))
                s = s.replace(tzinfo=None).replace(tzinfo=ZONE)      # wall-clock time in ZONE (DST-safe)
                e = s + timedelta(minutes=dur)
                spaced = all(s >= x[1] + timedelta(minutes=60) or e <= x[0] - timedelta(minutes=60) for x in today)
                if s >= frm and e <= end_of_day(to) and spaced and is_free(s, e, thread, cat):
                    today.append((s, e))
            if today:
                out += sorted(today)
                k += 1
            day = (day + timedelta(days=1, hours=2)).replace(hour=0)
        return out

    results = []
    for base in reqs:
        o = base.get("output") or {}
        cat = base.get("senderCategory") if base.get("senderCategory") in hours else \
            (st.get("defaultCategory") if st.get("defaultCategory") in hours else "career")
        try:
            dur = int(float(o.get("durationMinutes") or 30)) or 30
        except (TypeError, ValueError):
            dur = 30
        dur = min(180, max(15, -(-dur // 15) * 15))
        part = o.get("preferredPartOfDay") if o.get("preferredPartOfDay") in ("morning", "afternoon") else "any"
        mn = earliest()
        ps, pe = _date(o.get("preferredStartDate")), _date(o.get("preferredEndDate"))
        frm = ps if ps and ps > mn else mn
        if frm > now + timedelta(days=60):
            frm = mn
        to = end_of_day(pe) if pe and pe >= start_of_day(frm) else end_of_day(frm + timedelta(days=14))
        if to > now + timedelta(days=75):
            to = end_of_day(now + timedelta(days=75))
        hinted = bool(ps or pe or part != "any")
        thread = base.get("threadId") or ""
        found: list[tuple[datetime, datetime]] = []

        def merge(more: list[tuple[datetime, datetime]]) -> None:
            for f in more:
                if len(found) >= 3:
                    break
                if not any(f[0] < x[1] + timedelta(minutes=60) and f[1] > x[0] - timedelta(minutes=60) for x in found):
                    found.append(f)
            found.sort()

        if busy_ok:
            found = search(frm, to, cat, part, dur, thread, 3, 1)
            if len(found) < 2:
                merge(search(frm, to, cat, part, dur, thread, 3, 3))
            if len(found) < 2 and part != "any":
                merge(search(frm, to, cat, "any", dur, thread, 3, 3))
            if len(found) < 2:
                merge(search(mn, end_of_day(mn + timedelta(days=14)), cat, "any", dur, thread, 3, 1))
        relaxed = hinted and any(
            f[0] < frm or f[1] > end_of_day(to) or (part == "morning" and f[0].astimezone(ZONE).hour >= 13)
            or (part == "afternoon" and f[0].astimezone(ZONE).hour < 13) for f in found)
        for f in found:
            busy.append({"s": f[0], "e": f[1], "thread": "", "presence": False})
        slots = [{"startISO": iso(s), "endISO": iso(e), "label": fmt_label(s) + "-" + e.astimezone(ZONE).strftime("%H:%M")}
                 for s, e in found]
        if not busy_ok:
            text = "(Calendar temporarily unavailable -- could not confirm free slots right now.)"
        elif not slots:
            text = "No free slots found in the next two weeks."
        else:
            text = "; ".join(x["label"] for x in slots)
        results.append({**base, "freeSlots": slots, "freeSlotsText": text, "slotsRelaxed": relaxed,
                        "meetingMinutes": dur, "calendarOk": busy_ok, "schedulingCategory": cat,
                        "schedulingCategoryLabel": LABEL[cat]})
    return results


def _wall(d: datetime) -> int:
    d = d.astimezone(ZONE)
    return d.hour * 60 + d.minute


def _date(v: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v)[:10]).replace(tzinfo=ZONE) if v else None
    except ValueError:
        return None


def start_of_day(d: datetime) -> datetime:
    return d.astimezone(ZONE).replace(hour=0, minute=0, second=0, microsecond=0)


def end_of_day(d: datetime) -> datetime:
    return d.astimezone(ZONE).replace(hour=23, minute=59, second=59, microsecond=999000)


# ---------------------------------------------------------------- decisions
def decide_link(url: str, token: str, action: str) -> str:
    return f"{url}?t={quote(token, safe='')}&a={action}"


def booking_note(j: dict[str, Any], token: str, st: dict[str, str], decide_url: str) -> dict[str, Any]:
    names = {c["id"]: c.get("name") or c["id"] for c in pj(st.get("calendars"), []) if isinstance(c, dict) and c.get("id")}
    booking = pj(st.get("bookingCalendars"), {})
    addr = ((j.get("from") or {}).get("value") or [{}])[0].get("address", "")
    name = ((j.get("from") or {}).get("value") or [{}])[0].get("name") or addr or "contact"
    cat = j.get("senderCategory") or st.get("defaultCategory") or "career"
    cal_id = j.get("senderCalendarId") or booking.get(cat) or "primary"
    cal_name = names.get(cal_id) or ("Primary" if cal_id == "primary" else cal_id)
    s, e = parse_dt(j["startISO"]).astimezone(ZONE), parse_dt(j["endISO"]).astimezone(ZONE)
    when = f"{s:%A} {s.day} {s:%B %Y}, {s:%H:%M}-{e:%H:%M} ({ZONE.key})"
    title = "Meeting with " + name
    lines = [name + (f" <{addr}>" if addr and addr != name else "") + " confirmed one of the times you proposed:",
             "", "  " + when, "", "Subject: " + (j.get("subject") or "(no subject)"),
             "Original email: https://mail.google.com/mail/u/0/#all/" + str(j.get("threadId")),
             "", "Your confirmation reply is already in Drafts on that thread (not sent).", "",
             f'BOOK it in my "{cal_name}" calendar:', decide_link(decide_url, token, "book"), "",
             "Do NOT book it (releases the proposed slots):", decide_link(decide_url, token, "decline"),
             "", "--", NOTE_FOOTER]
    return {"token": token, "gmailId": j.get("id"), "threadId": j.get("threadId"), "title": title,
            "counterpartEmail": addr, "sessionsJson": js_json([{"startISO": j["startISO"], "endISO": j["endISO"]}]),
            "summary": "Confirmed by email: " + (j.get("subject") or ""), "calendarId": cal_id, "calendarName": cal_name,
            "draftSubject": f"[Inbox Pilot] BOOK? - {title} - {s:%a} {s.day} {s:%b %H:%M}", "draftBody": "\n".join(lines)}


# ---------------------------------------------------------------- events
TYPE = {"webinar": "Webinar", "course": "Course", "conference": "Conference", "workshop": "Workshop",
        "seminar": "Seminar", "meetup": "Meetup", "event": "Event"}


def merge_extraction(item: dict[str, Any], o: dict[str, Any] | None, error: bool,
                     now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(ZONE)
    o = o or {}
    sessions = []
    for s in (o.get("sessions") if isinstance(o.get("sessions"), list) else [])[:10]:
        st = parse_dt((s or {}).get("start"))
        if not st:
            continue
        en = parse_dt((s or {}).get("end"))
        if not en or en <= st:
            en = st + timedelta(minutes=60)
        if en < now:
            continue
        sessions.append({"startISO": iso(st), "endISO": iso(en)})
    frm = item.get("from") or {}
    return {"gmailId": item.get("id"), "threadId": item.get("threadId"), "subject": item.get("subject"),
            "fromText": frm.get("text", ""), "fromAddress": (frm.get("value") or [{}])[0].get("address", ""),
            "isEvent": o.get("isEvent") is True and bool(sessions), "eventType": o.get("eventType") or "event",
            "title": str(o.get("title") or item.get("subject") or "Event")[:200], "sessions": sessions,
            "link": o.get("link") or "", "organizer": o.get("organizer") or frm.get("text", ""),
            "location": o.get("location") or "", "summary": str(o.get("summary") or "")[:500],
            "notes": str(o.get("notes") or "")[:300],
            "recordStatus": "llm_error" if error else ("past_or_undated" if o.get("isEvent") is True else "not_event")}


def detect_conflicts(ev: dict[str, Any], busy_events: list[dict[str, Any]], busy_ok: bool) -> dict[str, Any]:
    cal = []
    for e in busy_events:
        b = busy_interval(e)
        if not b or (b[2] and "#holiday@" in str(e.get("__calendarId", ""))):
            continue
        cal.append((b, e))
    conflicts, seen, already = [], set(), False
    for s in ev["sessions"]:
        ss, se = parse_dt(s["startISO"]), parse_dt(s["endISO"])
        for b, e in cal:
            if not (ss < b[1] and se > b[0]):
                continue
            same = (not b[2] and abs((b[0] - ss).total_seconds()) <= 300 and abs((b[1] - se).total_seconds()) <= 300)
            if same and len(ev["sessions"]) == 1:
                already = True
                continue
            if e.get("id") in seen:
                continue
            seen.add(e.get("id"))
            org = e.get("organizer") or {}
            conflicts.append({"id": e.get("id"), "calendar": e.get("__calendarName") or e.get("__calendarId") or "",
                              "summary": e.get("summary") or "(busy)", "allDay": b[2],
                              "startISO": iso(b[0]), "endISO": iso(b[1]), "organizerSelf": bool(org.get("self")),
                              "organizerEmail": org.get("email") or "",
                              "attendees": [{"email": a.get("email"), "self": bool(a.get("self")),
                                             "resource": bool(a.get("resource"))} for a in e.get("attendees") or []]})
    return {**ev, "conflicts": conflicts, "calendarOk": busy_ok, "alreadyInCalendar": already,
            "recordStatus": "already_in_calendar" if already else ev["recordStatus"]}


def event_note(ev: dict[str, Any], token: str, decide_url: str) -> dict[str, Any]:
    kind = TYPE.get(ev.get("eventType"), "Event")
    first = parse_dt(ev["sessions"][0]["startISO"]).astimezone(ZONE)
    state = "CHECK" if not ev["calendarOk"] else ("CONFLICT" if ev["conflicts"] else "FREE")
    L = [f"{kind}: {ev['title']}"]
    for k, s in enumerate(ev["sessions"]):
        L.append((f"Session {k + 1}: " if len(ev["sessions"]) > 1 else "When: ")
                 + fmt_range(parse_dt(s["startISO"]), parse_dt(s["endISO"])) + f" ({ZONE.key})")
    if ev.get("location") or ev.get("link"):
        L.append("Where: " + " - ".join(x for x in (ev.get("location"), ev.get("link")) if x))
    if ev.get("organizer"):
        L.append("From: " + ev["organizer"])
    if ev.get("summary"):
        L += ["", ev["summary"]]
    if ev.get("notes"):
        L.append("Note: " + ev["notes"])
    if ev.get("threadId"):
        L.append("Original email: https://mail.google.com/mail/u/0/#all/" + ev["threadId"])
    L.append("")
    link = lambda a: decide_link(decide_url, token, a)  # noqa: E731
    if state == "FREE":
        L += ["YOUR CALENDAR: FREE at " + ("every session." if len(ev["sessions"]) > 1 else "this time."), "",
              "Add to my calendar:", link("add"), "", "Ignore it:", link("ignore")]
    elif state == "CONFLICT":
        L.append("YOUR CALENDAR: CONFLICT with")
        for c in ev["conflicts"]:
            who = [a["email"] for a in c.get("attendees") or [] if not a["self"] and not a["resource"] and a.get("email")]
            when = ("all day " + parse_dt(c["startISO"]).astimezone(ZONE).strftime("%a %-d %b") if c["allDay"]
                    else fmt_range(parse_dt(c["startISO"]), parse_dt(c["endISO"])))
            L.append(f"  - {c['summary']} - {when}" + (f" (with {', '.join(who[:4])}{', ...' if len(who) > 4 else ''})" if who else ""))
        L += ["", f"KEEP my existing schedule (do not add this {kind.lower()}):", link("keep"), "",
              f"SWAP to the new {kind.lower()} (add it, and draft reschedule requests for the meetings above):", link("swap")]
    else:
        L += ["YOUR CALENDAR: could not be checked right now.", "", "Add to my calendar anyway:", link("add"),
              "", "Ignore it:", link("ignore")]
    L += ["", "--", NOTE_FOOTER]
    subject = f"[Inbox Pilot] {state} - {kind}: {ev['title'][:80]} - {first:%a} {first.day} {first:%b %H:%M}"
    return {"draftSubject": subject, "draftBody": "\n".join(L), "sessionsJson": js_json(ev["sessions"]),
            "conflictsJson": js_json(ev["conflicts"]), "state": state}


# ---------------------------------------------------------------- recap
LEVELS = [(5, "CRITICAL"), (4, "HIGH"), (3, "MEDIUM"), (2, "LOW"), (1, "IGNORE")]


def recap_document(emails: list[dict[str, Any]], processed_ids: dict[str, dict[str, Any]],
                   now: datetime | None = None, summary: dict[str, Any] | None = None) -> dict[str, Any]:
    now = (now or datetime.now(ZONE)).astimezone(ZONE)
    norm = []
    for e in emails:
        row = processed_ids.get(str(e.get("gmailId") or ""))
        reply, sched = "Not tracked", ""
        if row:
            ps = row.get("proposalStatus")
            reply = "No reply needed" if ps == "no_reply_needed" else \
                DRAFT_LABEL.get(str(row.get("draftStatus") or ""), "Drafted")
            sched = {"confirmed": "Confirmed", "pending": "Proposed, awaiting confirmation",
                     "awaiting_approval": "Awaiting your BOOK"}.get(ps, "" if ps in ("no_reply_needed", "withdrawn") else "N/A")
        try:
            pr = min(5, max(1, round(float(e.get("priority") or 1))))
        except (TypeError, ValueError):
            pr = 1
        norm.append({**e, "priority": pr, "topic": str(e.get("topic") or "Uncategorized").strip() or "Uncategorized",
                     "reply": reply, "schedule": sched})
    text, bold = "", []
    text += "EMAIL RECAP\n" + f"Generated {now:%d-%m-%Y %H:%M}  |  {len(norm)} emails\n\n"
    if summary:
        head = "RUN SUMMARY" + (f"  ({summary['engine']})" if summary.get("engine") else "")
        bold.append((len(text), len(text) + len(head)))
        text += head + "\n" + "".join(f"    {line}\n" for line in summary_lines(summary)) + "\n"
    for score, label in LEVELS:
        group = [e for e in norm if e["priority"] == score]
        if not group:
            continue
        header = f"{label}  ({len(group)})"
        bold.append((len(text), len(text) + len(header)))
        text += header + "\n"
        for i, e in enumerate(group, 1):
            text += f"    {i}. {e.get('subject') or '(no subject)'}\n        Topic: {e['topic']}\n"
            text += f"        {e.get('summary') or '(no summary returned)'}\n"
            meta = f"        From: {e.get('from') or 'unknown'}  |  Date: {e.get('date') or 'unknown'}"
            if e.get("priorityReason"):
                meta += f"  |  Why {label}: {e['priorityReason']}"
            text += meta + "\n" + f"        Reply: {e['reply']}" + (f"  |  Schedule: {e['schedule']}" if e["schedule"] else "") + "\n\n"
    if not norm:
        text += "(No emails in this window.)\n"
    reqs: list[dict[str, Any]] = [{"insertText": {"location": {"index": 1}, "text": text}}]
    for a, b in bold:   # Docs indexes count UTF-16 code units
        ua = len(text[:a].encode("utf-16-le")) // 2
        ub = len(text[:b].encode("utf-16-le")) // 2
        reqs.append({"updateTextStyle": {"range": {"startIndex": 1 + ua, "endIndex": 1 + ub},
                                         "textStyle": {"bold": True}, "fields": "bold"}})
    items = [{"gmailId": str(e.get("gmailId") or ""), "threadId": str(e.get("threadId") or ""),
              "subject": e.get("subject") or "(no subject)", "from": e.get("from") or "unknown",
              "date": e.get("date") or "unknown", "summary": e.get("summary") or "(no summary returned)",
              "priorityReason": e.get("priorityReason") or "", "priority": e["priority"], "topic": e["topic"],
              "replyStatus": e["reply"], "scheduleStatus": e["schedule"]} for e in norm]
    return {"title": f"Email Recap - {now:%d-%m-%Y %H:%M}", "text": text, "requests": reqs, "count": len(norm),
            "items": items, "generatedAt": now.isoformat(timespec="seconds"), "summary": summary or {}}


def _subj_key(s: Any) -> str:
    return " ".join(str(s or "").lower().split())


def match_ranked(batch: list[dict[str, Any]], got: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
    """Pair the model's ranked emails with the emails sent.

    Exact subject match first (each answer used once); emails left over take the remaining
    answers in order only when the counts line up, so a dropped email never shifts the rest."""
    out: list[dict[str, Any] | None] = [None] * len(batch)
    free = list(range(len(got)))
    for k, m in enumerate(batch):
        key = _subj_key(m.get("subject"))
        for j in free:
            if key and _subj_key(got[j].get("subject")) == key:
                out[k] = got[j]
                free.remove(j)
                break
    left = [k for k in range(len(batch)) if out[k] is None]
    if left and len(left) == len(free):
        for k, j in zip(left, free):
            out[k] = got[j]
    return out


# ---------------------------------------------------------------- earlier documents
# "Did you read the doc I sent you?": find what the sender sent before (attachments and document
# links) so the reply can confirm it arrived, or ask for it again. Ported 1:1 to the n8n node
# "Find Earlier Items" (differential-tested), so both engines rank the same candidates.
REF_DAYS = 60
REF_LIMIT = 15
REF_TOP = 6
DOC_HOSTS = ("docs.google.com", "drive.google.com", "dropbox.com", "1drv.ms", "onedrive.live.com",
             "sharepoint.com", "wetransfer.com", "we.tl", "box.com", "icloud.com", "notion.so", "figma.com")
_URL = re.compile(r"https?://[^\s\"'<>)\]]+")
_DOC_EXT = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|odt|ods|odp|csv|zip|rar|7z|key|pages|numbers|rtf|txt)(?:[?#]|$)", re.I)
_INLINE = re.compile(r"^(image\d*|inline|outlook-|logo|signature)", re.I)
_NOT_DOC = ("text/calendar", "application/ics", "application/pkcs7-signature", "application/x-pkcs7-signature")
_TOKEN = re.compile(r"[^\W_]{3,}")


def reference_query(addr: str) -> str:
    """Gmail search for earlier mail from this sender that carries a file or a document link."""
    return (f'from:"{addr}" {{has:attachment "docs.google.com" "drive.google.com" "dropbox.com" "1drv.ms" '
            f'"sharepoint.com" "onedrive" "wetransfer" "we.tl"}} -in:chats -in:draft')


def _tokens(s: str) -> set[str]:
    return set(_TOKEN.findall((s or "").lower()))


def _ref_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, ZONE).date().isoformat() if ms else ""


def reference_candidates(msgs: list[dict[str, Any]], current: dict[str, Any],
                         keywords: list[str]) -> list[dict[str, Any]]:
    """Attachments and document links in `msgs` sent before `current`, best match first.

    Each message: id, threadId, subject, internalDate (ms), text, html, attachments[{filename, mimeType}].
    Score = 3 per keyword found in the file name/link or the email subject, +2 in the same conversation;
    ties go to the newest. Inline images, calendar files and signatures are not documents."""
    try:
        cur_ms = int(current.get("internalDate") or 0)
    except (TypeError, ValueError):
        cur_ms = 0
    kws = _tokens(" ".join(str(k) for k in (keywords or [])))
    found: list[dict[str, Any]] = []
    for m in msgs:
        if m.get("id") == current.get("id"):
            continue
        try:
            ms = int(m.get("internalDate") or 0)
        except (TypeError, ValueError):
            ms = 0
        if cur_ms and ms > cur_ms:
            continue
        subject = str(m.get("subject") or "")
        same = bool(current.get("threadId")) and m.get("threadId") == current.get("threadId")
        base = {"messageId": m.get("id") or "", "threadId": m.get("threadId") or "", "subject": subject,
                "at": _ref_day(ms), "ms": ms, "sameThread": same,
                "unread": "UNREAD" in (m.get("labelIds") or [])}
        for a in m.get("attachments") or []:
            fn = str(a.get("filename") or "").strip()
            mime = str(a.get("mimeType") or "").lower()
            if not fn or mime in _NOT_DOC or fn.lower().endswith((".ics", ".p7s")):
                continue
            if mime.startswith("image/") and _INLINE.match(fn):
                continue
            found.append({**base, "kind": "attachment", "name": fn, "url": ""})
        seen: set[str] = set()
        for u in _URL.findall(str(m.get("text") or "") + " " + str(m.get("html") or "")):
            u = u.replace("&amp;", "&").rstrip(".,;:")
            low = u.lower()
            if low in seen or not (any(h in low for h in DOC_HOSTS) or _DOC_EXT.search(low)):
                continue
            seen.add(low)
            found.append({**base, "kind": "link", "name": u[:160], "url": u})
    for c in found:
        c["score"] = 3 * len(kws & _tokens(c["name"] + " " + c["subject"])) + (2 if c["sameThread"] else 0)
    found.sort(key=lambda c: (-c["score"], -c["ms"], c["kind"], c["name"].lower()))
    out, keys = [], set()
    for c in found:
        k = (c["kind"], c["name"].lower())
        if k in keys:
            continue
        keys.add(k)
        out.append(c)
        if len(out) == REF_TOP:
            break
    return out


def reference_lines(cands: list[dict[str, Any]]) -> str:
    if not cands:
        return "(none found)"
    return "\n".join(
        f"{i}. {'Attachment' if c['kind'] == 'attachment' else 'Link'}: {c['name']} | received {c['at']} | "
        f"email subject: \"{c['subject'] or '(no subject)'}\"" + (" | same conversation" if c["sameThread"] else "")
        for i, c in enumerate(cands))


def span(hours: int) -> str:
    """24 -> '24 h', 72 -> '3 days' (for run logs and notes)."""
    return f"{hours // 24} days" if hours >= 48 and hours % 24 == 0 and hours > 48 else f"{hours} h"
