"""Which of your addresses the assistant works on.

Many addresses land in one Gmail mailbox (send-as aliases, Outlook/Hotmail or
work forwards, "check mail from other accounts"). Settings → Your addresses
scans the mailbox, lists them, and lets you untick the ones to ignore. By
default every address is tracked.

The choice becomes a Gmail search filter (`addressQuery`) stored in the shared
n8n Settings table, so the app engines and the n8n workflows apply exactly the
same rule by appending it to their Gmail searches:

* an unticked address a      -> -{to:a cc:a deliveredto:a}
  (any email addressed to it is skipped)
* the main Gmail address unticked -> {to:t cc:t deliveredto:t ...} for the
  addresses still ticked (only mail addressed to one of them is kept)

`deliveredto:` alone is not enough: Outlook/Hotmail forwards keep only the
Gmail address in Delivered-To, so To/Cc are matched too.
"""
from __future__ import annotations

import email.utils
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .connectors import n8n_tables as t

KEYS = ("primaryAddress", "ownAddresses", "excludedAddresses", "addressQuery", "addressesScannedAt")
TRACE_HEADERS = ("delivered-to", "x-forwarded-to", "x-forwarded-for", "x-original-to")
RECIPIENT_HEADERS = ("to", "cc")
SCAN_DAYS = 30
SCAN_LIMIT = 500
MIN_SEEN = 2          # an inferred address must appear on at least this many emails
_ADDR = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def norm(a: str) -> str:
    return (a or "").strip().strip("<>\"'").strip().lower()


def match(a: str) -> str:
    return f"{{to:{a} cc:{a} deliveredto:{a}}}"


def build_query(primary: str, addresses: Iterable[str], excluded: Iterable[str]) -> str:
    """Gmail search suffix for the current selection ('' = everything)."""
    primary = norm(primary)
    known = [a for a in dict.fromkeys(norm(x) for x in addresses) if a]
    ex = {norm(x) for x in excluded if norm(x)}
    tracked = [a for a in known if a not in ex]
    if not ex:
        return ""
    if not tracked:
        raise ValueError("keep at least one address ticked")
    parts = [f"-{match(a)}" for a in sorted(ex) if a != primary]
    if primary and primary in ex:
        others = [a for a in tracked if a != primary]
        parts.append("{" + " ".join(f"to:{a} cc:{a} deliveredto:{a}" for a in others) + "}")
    return " ".join(parts)


def _addrs(values: Iterable[str]) -> list[str]:
    out = []
    for _, a in email.utils.getaddresses([v for v in values if v]):
        a = norm(a)
        if _ADDR.match(a):
            out.append(a)
    for v in values:                       # X-Forwarded-For: "a@x b@y" (space separated)
        for tok in re.split(r"[\s,;]+", v or ""):
            tok = norm(tok)
            if _ADDR.match(tok) and tok not in out:
                out.append(tok)
    return out


def summarize(primary: str, send_as: list[str], messages: list[dict[str, list[str]]],
              min_seen: int = MIN_SEEN) -> list[dict[str, Any]]:
    """Addresses seen in the mailbox, with how they were seen and how many emails.

    messages: one dict per received email, header name (lowercase) -> all values.
    """
    primary = norm(primary)
    own = {primary, *map(norm, send_as)} - {""}
    info: dict[str, dict[str, Any]] = {}

    def add(a: str, kind: str, count: bool) -> None:
        r = info.setdefault(a, {"address": a, "kinds": [], "count": 0})
        if kind not in r["kinds"]:
            r["kinds"].append(kind)
        if count:
            r["count"] += 1

    for a in own:
        add(a, "main" if a == primary else "send-as", False)
    parsed = []
    for m in messages:                       # pass 1: forwarding traces name your other addresses
        traces = _addrs([v for h in TRACE_HEADERS for v in m.get(h, [])])
        rcpt = _addrs([v for h in RECIPIENT_HEADERS for v in m.get(h, [])])
        parsed.append((traces, rcpt, bool(m.get("list-id"))))
        for a in traces:
            if a not in own:
                add(a, "forwarded", False)
    mine_set = set(info)
    for traces, rcpt, is_list in parsed:     # pass 2: count emails per address
        # Delivered-To always carries the Gmail address, so it only counts via To/Cc.
        mine = {a for a in set(rcpt) | (set(traces) - {primary}) if a in mine_set}
        if not mine and not rcpt and primary:
            mine = {primary}                 # undisclosed recipients / BCC to you
        if mine:
            for a in mine:
                info[a]["count"] += 1
            continue
        # None of your known addresses is on it. Only when the email has exactly ONE To/Cc
        # recipient and is not list mail must that recipient be an address of yours (e.g. an
        # Outlook account that forwards here). With several recipients you were in BCC or got
        # it through a list, and the other names are colleagues, not you: never offer them.
        if len(rcpt) == 1 and not is_list:
            add(rcpt[0], "received-as", True)
    # Inferred addresses seen on a single email are almost always noise (e.g. a copy of a
    # message you sent from another account to a colleague): drop them.
    info = {a: r for a, r in info.items()
            if r["kinds"][0] in ("main", "send-as") or r["count"] >= min_seen}
    order = {"main": 0, "send-as": 1, "forwarded": 2, "received-as": 3}
    return sorted(info.values(), key=lambda r: (order.get(r["kinds"][0], 9), -r["count"], r["address"]))


# ------------------------------------------------------------- persistence
def _j(text: Any, fallback: Any) -> Any:
    try:
        v = json.loads(text) if isinstance(text, str) else text
        return fallback if v is None else v
    except (TypeError, ValueError):
        return fallback


def _settings(base: str) -> dict[str, dict[str, Any]]:
    return {r.get("key"): r for r in t.rows("settings", base) if r.get("key")}


def _put(base: str, existing: dict[str, dict[str, Any]], key: str, value: str) -> None:
    if key in existing:
        t.update("settings", base, existing[key]["id"], {"value": value})
    else:
        t.insert("settings", base, {"key": key, "value": value})


def load() -> dict[str, Any]:
    base = t._base()
    s = {k: (r.get("value") or "") for k, r in _settings(base).items()}
    primary = s.get("primaryAddress", "")
    own = [a for a in _j(s.get("ownAddresses"), []) if isinstance(a, dict) and a.get("address")]
    excluded = [norm(x) for x in _j(s.get("excludedAddresses"), [])]
    for a in own:
        a["tracked"] = norm(a["address"]) not in excluded
    return {"primary": primary, "addresses": own, "excluded": excluded,
            "query": s.get("addressQuery", ""), "scannedAt": s.get("addressesScannedAt", ""),
            "trackingAll": not excluded}


def save_excluded(excluded: list[str]) -> dict[str, Any]:
    base = t._base()
    existing = _settings(base)
    cur = load()
    known = [a["address"] for a in cur["addresses"]]
    ex = sorted({norm(x) for x in excluded if norm(x)} & {norm(a) for a in known})
    q = build_query(cur["primary"], known, ex)              # raises if nothing is left ticked
    _put(base, existing, "excludedAddresses", json.dumps(ex))
    _put(base, existing, "addressQuery", q)
    return load()


def scan(days: int | None = None, limit: int | None = None) -> dict[str, Any]:
    from . import app_settings
    from .assistant import google_io
    days = days or app_settings.local("scanDays")
    limit = limit or app_settings.local("scanMaxEmails")
    primary = google_io.profile_email()
    send_as = google_io.send_as_addresses()
    after = datetime.now(timezone.utc) - timedelta(days=days)
    msgs = google_io.recipient_headers("-in:sent -in:draft -in:chats", after, limit)
    found = summarize(primary, send_as, msgs, app_settings.local("scanMinSeen"))
    base = t._base()
    existing = _settings(base)
    excluded = [norm(x) for x in _j((existing.get("excludedAddresses") or {}).get("value"), [])]
    excluded = [x for x in excluded if x in {r["address"] for r in found}]
    _put(base, existing, "primaryAddress", norm(primary))
    _put(base, existing, "ownAddresses", json.dumps(found, ensure_ascii=False))
    _put(base, existing, "excludedAddresses", json.dumps(excluded))
    _put(base, existing, "addressQuery", build_query(primary, [r["address"] for r in found], excluded))
    _put(base, existing, "addressesScannedAt", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    out = load()
    out["scanned"] = len(msgs)
    return out
