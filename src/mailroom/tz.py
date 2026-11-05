"""The time zone MailRoom works in: MAILROOM_TIMEZONE if set, else this Mac's own time zone."""
from __future__ import annotations

import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def local_zone_name() -> str:
    env = (os.environ.get("MAILROOM_TIMEZONE") or "").strip()
    if env:
        return env
    try:                                     # macOS: /etc/localtime -> /var/db/timezone/zoneinfo/Area/City
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            name = target.split("zoneinfo/", 1)[1]
            ZoneInfo(name)
            return name
    except (OSError, ZoneInfoNotFoundError, ValueError):
        pass
    tz = (os.environ.get("TZ") or "").lstrip(":").strip()
    try:
        if tz:
            ZoneInfo(tz)
            return tz
    except (ZoneInfoNotFoundError, ValueError):
        pass
    return "UTC"


def local_zone() -> ZoneInfo:
    return ZoneInfo(local_zone_name())
