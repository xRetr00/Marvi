"""Trust calibration from explicit accept/dismiss outcomes."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Optional

NEVER_AUTO_DEFAULT = frozenset({"goal", "goals", "security", "credentials", "payments", "destructive"})
_SECURITY_RE = re.compile(r"(?:security|auth|credential|secret|payment|delete|destructive|approval)", re.I)


def evaluate_trust(counts: Dict[str, Any], current_tier: str) -> Optional[Dict[str, str]]:
    """Return a tier proposal from an already-windowed category summary."""
    category = str(counts.get("category") or "general")
    never_auto = set(counts.get("never_auto") or NEVER_AUTO_DEFAULT)
    promotion_accepts = int(counts.get("promotion_accepts", 8))
    demotion_dismissals = int(counts.get("demotion_dismissals", 3))
    accepted = int(counts.get("accepted", 0))
    dismissed = int(counts.get("dismissed", 0))
    last_ten = list(counts.get("last_ten") or [])[:10]

    if current_tier == "propose" and accepted >= promotion_accepts:
        safe = category not in never_auto and _SECURITY_RE.search(category) is None
        if safe and "dismissed" not in last_ten:
            return {"category": category, "value": "auto", "direction": "promote"}
    if current_tier in {"propose", "auto"} and dismissed >= demotion_dismissals:
        return {"category": category, "value": "notify", "direction": "demote"}
    return None


def proposals(events: Iterable[Dict[str, Any]], tiers: Dict[str, str], *, never_auto: Iterable[str] = (),
              promotion_accepts: int = 8, demotion_dismissals: int = 3) -> list[Dict[str, str]]:
    grouped: Dict[str, list[Dict[str, Any]]] = {}
    for event in events:
        if event.get("event") not in {"accepted", "dismissed"}:
            continue
        if (event.get("detail") or {}).get("accepted_by", "user") != "user":
            continue
        grouped.setdefault(str(event.get("category") or "general"), []).append(event)
    result = []
    for category, rows in grouped.items():
        summary = {
            "category": category,
            "accepted": sum(row.get("event") == "accepted" for row in rows),
            "dismissed": sum(row.get("event") == "dismissed" for row in rows),
            # Outcomes arrive newest first. Keep that ordering for the exact
            # "last ten decisions" contract.
            "last_ten": [str(row.get("event")) for row in rows[:10]],
            "never_auto": set(never_auto) | set(NEVER_AUTO_DEFAULT),
            "promotion_accepts": promotion_accepts,
            "demotion_dismissals": demotion_dismissals,
        }
        proposal = evaluate_trust(summary, str(tiers.get(category) or "propose"))
        if proposal:
            result.append(proposal)
    return result
