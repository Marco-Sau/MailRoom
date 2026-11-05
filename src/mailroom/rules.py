"""Rule loading plus the deterministic half of prioritization.

The LLM proposes a priority; these rules can override it. Keeping overrides
deterministic means a P1 from your boss never depends on model temperature.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from .settings import settings

PRIORITY_ORDER = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}


@dataclass
class Rules:
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path=None) -> "Rules":
        p = path or settings.rules_path
        with open(p, "r", encoding="utf-8") as fh:
            return cls(raw=yaml.safe_load(fh) or {})

    # -- convenience accessors -------------------------------------------------
    @property
    def identity(self) -> dict[str, Any]:
        return self.raw.get("identity", {})

    @property
    def timezone(self) -> str:
        from .tz import local_zone_name
        return self.identity.get("timezone") or local_zone_name()

    @property
    def categories(self) -> list[dict[str, str]]:
        return self.raw.get("categories", [])

    @property
    def category_ids(self) -> list[str]:
        return [c["id"] for c in self.categories]

    @property
    def calendar(self) -> dict[str, Any]:
        return self.raw.get("calendar", {})

    @property
    def sheets(self) -> dict[str, Any]:
        return self.raw.get("sheets", {})

    @property
    def drafting(self) -> dict[str, Any]:
        return self.raw.get("drafting", {})

    @property
    def priority_levels(self) -> dict[str, str]:
        return self.raw.get("priority", {}).get("levels", {})

    @property
    def vip_senders(self) -> list[str]:
        return self.raw.get("priority", {}).get("vip_senders", []) or []

    # -- deterministic overrides ----------------------------------------------
    def apply_overrides(self, *, sender: str, subject: str, category: str,
                        llm_priority: str) -> tuple[str, str]:
        """Return (priority, reason). First matching override wins."""
        sender_l = (sender or "").lower()

        for vip in self.vip_senders:
            v = vip.lower()
            if sender_l == v or (v.startswith("@") and sender_l.endswith(v)):
                return "P1", f"VIP sender rule ({vip})"

        for rule in self.raw.get("priority", {}).get("overrides", []) or []:
            when = rule.get("when", {})
            if self._matches(when, sender_l, subject or "", category):
                return rule["set"], f"override rule: {when}"
        return llm_priority, ""

    @staticmethod
    def _matches(when: dict[str, Any], sender: str, subject: str, category: str) -> bool:
        if "sender_in" in when:
            vals = [v.lower() for v in when["sender_in"]]
            if not any(sender == v or (v.startswith("@") and sender.endswith(v)) for v in vals):
                return False
        if "category_in" in when and category not in when["category_in"]:
            return False
        if "subject_matches" in when and not re.search(when["subject_matches"], subject):
            return False
        return bool(when)
