"""Delivery/engagement matching and conservative quiet-window proposals."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from .outcomes import recent, record


def _local_time(value: datetime) -> datetime:
    from hermes_time import get_timezone

    return value.astimezone(get_timezone() or datetime.now().astimezone().tzinfo)


def _settings() -> tuple[bool, int]:
    try:
        from hermes_cli.config import cfg_get, load_config

        cfg = load_config()
        return (
            bool(cfg_get(cfg, "learning", "timing", "enabled", default=False)),
            int(cfg_get(cfg, "learning", "timing", "engagement_window_minutes", default=60)),
        )
    except Exception:
        return False, 60


def record_delivery(*, platform: str, chat_id: str, thread_id: str = "", ref: str = "", at: Optional[str] = None) -> None:
    if not _settings()[0]:
        return
    record("timing", "delivered", category=platform, ref=ref or None, at=at,
           detail={"platform": platform, "chat_id": str(chat_id), "thread_id": str(thread_id or "")})


def record_engagement(*, platform: str, chat_id: str, thread_id: str = "", at: Optional[str] = None,
                      window_minutes: Optional[int] = None) -> bool:
    enabled, configured_window = _settings()
    if not enabled:
        return False
    window_minutes = configured_window if window_minutes is None else window_minutes
    now = datetime.fromisoformat(at.replace("Z", "+00:00")) if at else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    for row in recent(loop="timing", days=2):
        if row.get("event") != "delivered":
            continue
        detail = row.get("detail") or {}
        if (str(detail.get("platform")) != str(platform) or str(detail.get("chat_id")) != str(chat_id)
                or str(detail.get("thread_id") or "") != str(thread_id or "")):
            continue
        try:
            delivered = datetime.fromisoformat(str(row["at"]).replace("Z", "+00:00"))
            if delivered.tzinfo is None:
                delivered = delivered.replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        minutes = (now - delivered).total_seconds() / 60
        if not 0 <= minutes <= window_minutes:
            continue
        ref = str(row.get("ref") or row.get("at"))
        if any(item.get("event") == "engaged" and item.get("ref") == ref for item in recent(loop="timing", days=2)):
            return False
        record("timing", "engaged", category=platform, ref=ref, at=at,
               detail={"platform": platform, "chat_id": str(chat_id), "thread_id": str(thread_id or ""), "minutes": round(minutes, 1)})
        return True
    return False


def propose_windows(events: Iterable[Dict[str, Any]], *, minimum_deliveries: int = 100) -> Optional[Dict[str, Any]]:
    deliveries: Dict[str, Dict[str, Any]] = {}
    engaged = set()
    for row in events:
        if row.get("event") == "delivered":
            deliveries[str(row.get("ref") or row.get("at"))] = row
        elif row.get("event") == "engaged":
            engaged.add(str(row.get("ref") or ""))
    # The threshold is deliberately applied to matched engagement events, not
    # raw sends. Delivery-only telemetry must never create a proposal.
    if len(engaged) < minimum_deliveries:
        return None
    by_hour: Counter[int] = Counter()
    total_by_hour: Counter[int] = Counter()
    for ref, row in deliveries.items():
        try:
            hour = _local_time(datetime.fromisoformat(str(row["at"]).replace("Z", "+00:00"))).hour
        except (KeyError, ValueError):
            continue
        total_by_hour[hour] += 1
        if ref in engaged:
            by_hour[hour] += 1
    # Quiet only hours with substantial volume and engagement under 10%.
    quiet = [f"{hour:02d}:00-{(hour + 1) % 24:02d}:00" for hour, total in sorted(total_by_hour.items()) if total >= 10 and by_hour[hour] / total < .10]
    if not quiet:
        return None
    return {
        "path": "learning.timing.quiet_hours",
        "value": quiet,
        "current": [],
        "rationale": f"Matched {len(engaged)} replies to {len(deliveries)} proactive deliveries; these hours had under 10% engagement.",
        "samples": len(deliveries),
    }


def mark_ignored(*, window_minutes: int = 60, now: Optional[datetime] = None) -> int:
    """Materialize expired unmatched deliveries as ignored outcomes."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    events = recent(loop="timing", days=30)
    resolved = {str(row.get("ref") or "") for row in events if row.get("event") in {"engaged", "ignored"}}
    added = 0
    for row in events:
        if row.get("event") != "delivered":
            continue
        ref = str(row.get("ref") or row.get("at") or "")
        if ref in resolved:
            continue
        try:
            stamp = datetime.fromisoformat(str(row.get("at") or "").replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if (current - stamp).total_seconds() < window_minutes * 60:
            continue
        if record("timing", event="ignored", category=str(row.get("category") or ""), ref=ref,
                  detail={"delivery_only": False}) is not None:
            added += 1
            resolved.add(ref)
    return added


def is_quiet_now(windows: Iterable[str], when: Optional[datetime] = None) -> bool:
    if when is None:
        from hermes_time import now as local_now

        when = local_now()
    hour = _local_time(when).hour
    return any(str(window).startswith(f"{hour:02d}:00-") for window in windows)
