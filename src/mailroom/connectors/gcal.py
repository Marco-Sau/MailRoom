"""Google Calendar: free/busy lookup and slot proposal.

Slot generation is deterministic (no LLM): working hours minus busy blocks minus
buffers, filtered by minimum notice. The model only decides *whether* a meeting
is being requested; the calendar decides *when* it could happen.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..models import TimeSlot
from . import google_auth

_DAY_KEYS = {0: "mon_fri", 1: "mon_fri", 2: "mon_fri", 3: "mon_fri", 4: "mon_fri",
             5: "sat", 6: "sun"}


def _windows_for(day: datetime, cfg: dict[str, Any], tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    spec = (cfg.get("working_hours") or {}).get(_DAY_KEYS[day.weekday()]) or []
    out = []
    for i in range(0, len(spec) - 1, 2):
        h1, m1 = (int(x) for x in spec[i].split(":"))
        h2, m2 = (int(x) for x in spec[i + 1].split(":"))
        out.append((
            datetime.combine(day.date(), time(h1, m1), tzinfo=tz),
            datetime.combine(day.date(), time(h2, m2), tzinfo=tz),
        ))
    return out


def busy_blocks(calendar_id: str, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    svc = google_auth.service("calendar", "v3")
    resp = svc.freebusy().query(body={
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "items": [{"id": calendar_id}],
    }).execute()
    blocks = []
    for b in resp["calendars"][calendar_id].get("busy", []):
        blocks.append((datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])))
    return sorted(blocks)


def propose_slots(cfg: dict[str, Any], tz_name: str, now: datetime | None = None,
                  busy: list[tuple[datetime, datetime]] | None = None) -> list[TimeSlot]:
    """Pure enough to unit-test: pass `busy` to skip the network call."""
    tz = ZoneInfo(tz_name)
    now = (now or datetime.now(tz)).astimezone(tz)
    slot_len = timedelta(minutes=int(cfg.get("slot_minutes", 30)))
    buffer = timedelta(minutes=int(cfg.get("buffer_minutes", 15)))
    earliest = now + timedelta(hours=int(cfg.get("min_notice_hours", 24)))
    days = int(cfg.get("search_days_ahead", 10))
    want = int(cfg.get("max_proposed_slots", 3))
    horizon_end = now + timedelta(days=days)

    if busy is None:
        busy = busy_blocks(cfg.get("calendar_id", "primary"), now, horizon_end)
    busy = [(s.astimezone(tz), e.astimezone(tz)) for s, e in busy]

    slots: list[TimeSlot] = []
    for d in range(days + 1):
        day = now + timedelta(days=d)
        for win_start, win_end in _windows_for(day, cfg, tz):
            cursor = max(win_start, earliest)
            while cursor + slot_len <= win_end:
                cand_s, cand_e = cursor, cursor + slot_len
                clash = next(((bs, be) for bs, be in busy
                              if cand_s < be + buffer and bs - buffer < cand_e), None)
                if clash:
                    cursor = clash[1] + buffer
                    continue
                slots.append(TimeSlot(start=cand_s, end=cand_e))
                if len(slots) >= want:
                    return slots
                # Spread proposals out: at most one per window per day.
                break
    return slots


def list_calendars() -> list[dict[str, str]]:
    """Calendars in the connected Google account: [{id, name}], primary first."""
    svc = google_auth.service("calendar", "v3")
    out, token = [], None
    while True:
        resp = svc.calendarList().list(pageToken=token, maxResults=250).execute()
        for c in resp.get("items", []):
            out.append({"id": c["id"], "name": c.get("summaryOverride") or c.get("summary") or c["id"],
                        "primary": bool(c.get("primary"))})
        token = resp.get("nextPageToken")
        if not token:
            break
    out.sort(key=lambda c: (not c["primary"], c["name"].lower()))
    return [{"id": c["id"], "name": c["name"]} for c in out]
