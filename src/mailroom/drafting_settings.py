"""What the n8n drafting workflow replies to, edited from Settings.

Two modes, chosen by the `draftScope` setting:

* ``senders`` - only emails from the tracked senders listed here.
* ``inbox``   - every email in the inbox (last 48h), minus Gmail's
  promotions/social/updates/forums tabs, no-reply and bulk mail; Claude then
  decides which ones actually need an answer.

Either way nothing is sent: replies land in Gmail Drafts.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .connectors import n8n_tables as t

SCOPES = ("senders", "inbox")
CATEGORIES = {"fulltime": "Full-time job", "career": "Professional career", "anytime": "Any time"}
DEFAULTS = {"draftScope": "senders", "signatureName": "", "defaultNotes": "",
            "maxEmails": "25", "defaultCategory": "career", "workCalendar": "",
            "presenceMinHours": "4", "calendars": "[]", "busyCalendars": "[]",
            "bookingCalendars": "{}", "workHours": "{}", "careerHours": "{}", "anytimeHours": "{}"}
HOUR_KEYS = {"workHours": "fulltime", "careerHours": "career", "anytimeHours": "anytime"}
JSON_KEYS = ("calendars", "busyCalendars", "bookingCalendars")

# ------------------------------------------------------------- weekly hours
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DAY_IDX = {d.lower(): i + 1 for i, d in enumerate(DAYS)}
_TIME = r"([01]?\d|2[0-4]):([0-5]\d)"


def _norm_time(h: str, m: str) -> str:
    return f"{int(h):02d}:{m}"


def parse_hours(text: str) -> dict[str, list[list[str]]]:
    """'Mon-Fri 09:00-17:00; Wed 18:00-20:00' -> {"1": [["09:00","17:00"]], ...}.

    Clauses are separated by ';' or newlines. A clause is a day spec (Mon,
    Mon-Fri, Sat/Sun, Daily) followed by one or more HH:MM-HH:MM ranges.
    Clauses add up, so 'Mon-Fri 09:00-17:00; Wed 18:00-20:00' gives Wednesday both.
    """
    out: dict[str, list[list[str]]] = {}
    for clause in re.split(r"[;\n]+", text or ""):
        clause = clause.strip()
        if not clause:
            continue
        m = re.match(r"^([A-Za-z/,\- ]+?)\s+(\d.*)$", clause)
        if not m:
            raise ValueError(f"cannot read {clause!r}: expected e.g. 'Mon-Fri 09:00-13:00'")
        days_txt, ranges_txt = m.group(1).strip().lower(), m.group(2)
        days: list[int] = []
        if days_txt in ("daily", "every day", "everyday", "all"):
            days = list(range(1, 8))
        else:
            for part in re.split(r"[/,]+", days_txt):
                part = part.strip()
                if "-" in part:
                    a, b = [x.strip()[:3] for x in part.split("-", 1)]
                    if a not in _DAY_IDX or b not in _DAY_IDX:
                        raise ValueError(f"unknown day in {part!r}")
                    i, j = _DAY_IDX[a], _DAY_IDX[b]
                    days += list(range(i, j + 1)) if i <= j else list(range(i, 8)) + list(range(1, j + 1))
                elif part[:3] in _DAY_IDX:
                    days.append(_DAY_IDX[part[:3]])
                else:
                    raise ValueError(f"unknown day {part!r}")
        rngs = re.findall(_TIME + r"\s*-\s*" + _TIME, ranges_txt)
        if not rngs:
            raise ValueError(f"no HH:MM-HH:MM range in {clause!r}")
        for h1, m1, h2, m2 in rngs:
            a, b = _norm_time(h1, m1), _norm_time(h2, m2)
            if b <= a:
                raise ValueError(f"range {a}-{b} ends before it starts")
            for d in days:
                lst = out.setdefault(str(d), [])
                if [a, b] not in lst:
                    lst.append([a, b])
    for v in out.values():
        v.sort()
    return out


def format_hours(table: dict[str, Any]) -> str:
    """Inverse of parse_hours, grouping consecutive days with identical ranges."""
    per_day = [tuple(tuple(r) for r in (table or {}).get(str(d), [])) for d in range(1, 8)]
    parts, d = [], 0
    while d < 7:
        if not per_day[d]:
            d += 1
            continue
        e = d
        while e + 1 < 7 and per_day[e + 1] == per_day[d]:
            e += 1
        span = "Daily" if (d, e) == (0, 6) else (DAYS[d] if d == e else f"{DAYS[d]}-{DAYS[e]}")
        parts.append(span + " " + " ".join(f"{a}-{b}" for a, b in per_day[d]))
        d = e + 1
    return "; ".join(parts)
_ADDR = re.compile(r"^[^@\s,\"]+@[^@\s,\"]+\.[^@\s,\"]+$")
_DOMAIN = re.compile(r"^@?[a-z0-9.-]+\.[a-z]{2,}$", re.I)


def normalize_patterns(raw: str) -> str:
    """'Anna@Acme.it ; @client.com' -> 'anna@acme.it, @client.com' (validated)."""
    parts = [p.strip().lower() for p in re.split(r"[,;\s]+", raw or "") if p.strip()]
    if not parts:
        raise ValueError("enter at least one email address or @domain")
    bad = [p for p in parts if not (_ADDR.match(p) or _DOMAIN.match(p))]
    if bad:
        raise ValueError(f"not an email address or @domain: {', '.join(bad)}")
    out: list[str] = []
    for p in parts:
        if not p.startswith("@") and "@" not in p:
            p = "@" + p                                   # bare domain -> @domain
        if p not in out:
            out.append(p)
    return ", ".join(out)


def _as_bool(v: Any) -> bool:
    return v is True or str(v).strip().lower() in ("true", "1", "yes")


def _j(text: str, fallback: Any) -> Any:
    try:
        v = json.loads(text or "")
        return fallback if v is None else v
    except ValueError:
        return fallback


def load() -> dict[str, Any]:
    base = t._base()
    settings = dict(DEFAULTS)
    for r in t.rows("settings", base):
        if r.get("key"):
            settings[str(r["key"])] = "" if r.get("value") is None else str(r["value"])
    senders = [{"id": r["id"], "matchPatterns": r.get("matchPatterns") or "",
                "senderName": r.get("senderName") or "", "notes": r.get("notes") or "",
                "category": r.get("category") or "", "calendarId": r.get("calendarId") or "",
                "active": _as_bool(r.get("active"))} for r in t.rows("senders", base)]
    if settings["draftScope"] not in SCOPES:
        settings["draftScope"] = "senders"
    out: dict[str, Any] = {k: v for k, v in settings.items() if k not in JSON_KEYS and k not in HOUR_KEYS}
    out["calendars"] = [c for c in _j(settings["calendars"], []) if isinstance(c, dict) and c.get("id")]
    out["busyCalendars"] = [str(x) for x in _j(settings["busyCalendars"], [])]
    out["bookingCalendars"] = {k: str(v) for k, v in _j(settings["bookingCalendars"], {}).items()}
    out["hours"] = {cat: format_hours(_j(settings[key], {})) for key, cat in HOUR_KEYS.items()}
    out["categories"] = CATEGORIES
    return {**out, "senders": senders, "active_senders": sum(1 for s in senders if s["active"])}


def _valid_calendar(cal_id: str, known: set[str]) -> str:
    cal_id = (cal_id or "").strip()
    if cal_id and known and cal_id not in known:
        raise ValueError(f"unknown calendar {cal_id!r}; refresh the calendar list first")
    return cal_id


def save_settings(changes: dict[str, Any]) -> dict[str, Any]:
    base = t._base()
    existing = {r.get("key"): r for r in t.rows("settings", base)}
    known = {c.get("id") for c in _j((existing.get("calendars") or {}).get("value") or "[]", [])
             if isinstance(c, dict)}
    clean: dict[str, str] = {}
    for k, v in changes.items():
        if v is None:
            continue
        if k == "hours":                                   # {"fulltime": "Mon-Fri 09:00-17:00", ...}
            for key, cat in HOUR_KEYS.items():
                if v.get(cat) is not None:
                    clean[key] = json.dumps(parse_hours(str(v[cat])))
            continue
        if k == "busyCalendars":
            ids = [_valid_calendar(str(x), known) for x in v if str(x).strip()]
            if not ids:
                raise ValueError("choose at least one busy calendar")
            clean[k] = json.dumps(ids)
            continue
        if k == "bookingCalendars":
            clean[k] = json.dumps({c: _valid_calendar(str(x), known) for c, x in v.items()
                                   if c in CATEGORIES and str(x).strip()})
            continue
        if k not in DEFAULTS or k in JSON_KEYS or k in HOUR_KEYS:
            continue
        v = str(v).strip()
        if k == "draftScope" and v not in SCOPES:
            raise ValueError(f"draftScope must be one of {SCOPES}")
        if k == "defaultCategory" and v not in CATEGORIES:
            raise ValueError(f"defaultCategory must be one of {tuple(CATEGORIES)}")
        if k == "workCalendar":
            v = _valid_calendar(v, known)
        if k == "maxEmails":
            n = int(v)
            if not 1 <= n <= 50:
                raise ValueError("maxEmails must be between 1 and 50")
            v = str(n)
        if k == "presenceMinHours":
            n = float(v)
            if not 1 <= n <= 12:
                raise ValueError("presence threshold must be between 1 and 12 hours")
            v = str(n).rstrip("0").rstrip(".")
        if k == "signatureName" and not v:
            raise ValueError("signature name cannot be empty")
        clean[k] = v
    for k, v in clean.items():
        if k in existing:
            t.update("settings", base, existing[k]["id"], {"value": v})
        else:
            t.insert("settings", base, {"key": k, "value": v})
    return load()


def set_calendar_catalog(calendars: list[dict[str, str]]) -> dict[str, Any]:
    """Replace the list of selectable calendars (from Google's calendarList)."""
    cats = [{"id": c["id"], "name": c.get("name") or c["id"]} for c in calendars if c.get("id")]
    if not cats:
        raise ValueError("Google returned no calendars")
    base = t._base()
    existing = {r.get("key"): r for r in t.rows("settings", base)}
    value = json.dumps(cats, ensure_ascii=False)
    if "calendars" in existing:
        t.update("settings", base, existing["calendars"]["id"], {"value": value})
    else:
        t.insert("settings", base, {"key": "calendars", "value": value})
    return load()


def _sender_fields(changes: dict[str, Any], known: set[str]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if changes.get("matchPatterns") is not None:
        data["matchPatterns"] = normalize_patterns(changes["matchPatterns"])
    for k in ("senderName", "notes"):
        if changes.get(k) is not None:
            data[k] = str(changes[k]).strip()
    if changes.get("category") is not None:
        c = str(changes["category"]).strip()
        if c and c not in CATEGORIES:
            raise ValueError(f"category must be one of {tuple(CATEGORIES)}")
        data["category"] = c
    if changes.get("calendarId") is not None:
        data["calendarId"] = _valid_calendar(str(changes["calendarId"]), known)
    if changes.get("active") is not None:
        data["active"] = bool(changes["active"])
    return data


def _known_calendars(base: str) -> set[str]:
    for r in t.rows("settings", base):
        if r.get("key") == "calendars":
            return {c.get("id") for c in _j(r.get("value") or "[]", []) if isinstance(c, dict)}
    return set()


def add_sender(match: str, name: str = "", notes: str = "", active: bool = True,
               category: str = "", calendar_id: str = "") -> dict[str, Any]:
    base = t._base()
    data = _sender_fields({"matchPatterns": match, "senderName": name, "notes": notes,
                           "category": category, "calendarId": calendar_id, "active": active},
                          _known_calendars(base))
    taken = {p.strip() for r in t.rows("senders", base)
             for p in str(r.get("matchPatterns") or "").lower().split(",") if p.strip()}
    dup = [p for p in data["matchPatterns"].split(", ") if p in taken]
    if dup:
        raise ValueError(f"already tracked: {', '.join(dup)}")
    t.insert("senders", base, data)
    return load()


def update_sender(row_id: int, changes: dict[str, Any]) -> dict[str, Any]:
    base = t._base()
    data = _sender_fields(changes, _known_calendars(base))
    if data:
        t.update("senders", base, row_id, data)
    return load()


def delete_sender(row_id: int) -> dict[str, Any]:
    t.delete("senders", t._base(), row_id)
    return load()
