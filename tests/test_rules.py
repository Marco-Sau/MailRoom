from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from mailroom.connectors.gcal import propose_slots
from mailroom.rules import Rules

RAW = {
    "identity": {"timezone": "Europe/Rome", "full_name": "Alex"},
    "categories": [{"id": "appointment", "description": "x"}, {"id": "newsletter", "description": "y"}],
    "priority": {
        "levels": {"P1": "today", "P4": "ignore"},
        "vip_senders": ["@client.example"],
        "overrides": [
            {"when": {"category_in": ["newsletter"]}, "set": "P4"},
            {"when": {"subject_matches": "(?i)urgente"}, "set": "P1"},
        ],
    },
}


@pytest.fixture
def rules():
    return Rules(raw=RAW)


def test_vip_domain_beats_model(rules):
    pri, reason = rules.apply_overrides(
        sender="ceo@client.example", subject="ciao", category="other", llm_priority="P3")
    assert pri == "P1" and "VIP" in reason


def test_category_override(rules):
    pri, _ = rules.apply_overrides(
        sender="news@vendor.com", subject="Weekly digest", category="newsletter", llm_priority="P2")
    assert pri == "P4"


def test_subject_regex_override(rules):
    pri, _ = rules.apply_overrides(
        sender="a@b.com", subject="RICHIESTA URGENTE", category="other", llm_priority="P3")
    assert pri == "P1"


def test_no_rule_keeps_model_priority(rules):
    pri, reason = rules.apply_overrides(
        sender="a@b.com", subject="hello", category="other", llm_priority="P2")
    assert pri == "P2" and reason == ""


# --------------------------------------------------------------------- slots
CAL = {
    "working_hours": {"mon_fri": ["09:00", "13:00", "14:00", "18:00"], "sat": [], "sun": []},
    "slot_minutes": 30, "buffer_minutes": 15, "min_notice_hours": 24,
    "search_days_ahead": 5, "max_proposed_slots": 3,
}
TZ = ZoneInfo("Europe/Rome")


def test_slots_respect_min_notice_and_weekend():
    now = datetime(2026, 9, 21, 10, 0, tzinfo=TZ)          # Monday
    slots = propose_slots(CAL, "Europe/Rome", now=now, busy=[])
    assert len(slots) == 3
    assert all(s.start >= now.replace(hour=10) for s in slots)
    assert all(s.start.weekday() < 5 for s in slots)       # no Sat/Sun windows configured


def test_slots_avoid_busy_blocks_with_buffer():
    now = datetime(2026, 9, 21, 8, 0, tzinfo=TZ)
    busy = [(datetime(2026, 9, 22, 9, 0, tzinfo=TZ), datetime(2026, 9, 22, 12, 0, tzinfo=TZ))]
    slots = propose_slots({**CAL, "max_proposed_slots": 1}, "Europe/Rome", now=now, busy=busy)
    s = slots[0]
    assert not (s.start < busy[0][1] and busy[0][0] < s.end)
