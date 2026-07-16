"""Derive repeated long-focus applications from ActivityWatch events."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional


def derive(events: Iterable[Dict[str, Any]], existing: Iterable[str], *, minimum_minutes: int = 25,
           minimum_occurrences: int = 5) -> Optional[Dict[str, Any]]:
    existing_list = list(existing)
    needles = [str(value).casefold() for value in existing_list]
    counts: Counter[str] = Counter()
    display: Dict[str, str] = {}
    intervals: list[tuple[datetime, datetime, str]] = []
    standalone: list[tuple[float, str]] = []
    for event in events:
        try:
            duration = float(event.get("duration") or 0)
        except (TypeError, ValueError):
            continue
        data = event.get("data") or {}
        app = str(data.get("app") or "").strip()
        if not app:
            continue
        key = app.casefold()
        if any(needle in key for needle in needles):
            continue
        display.setdefault(key, app)
        try:
            start = datetime.fromisoformat(str(event.get("timestamp") or "").replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            intervals.append((start, start + timedelta(seconds=max(0.0, duration)), key))
        except ValueError:
            standalone.append((duration, key))

    merged: list[tuple[datetime, datetime, str]] = []
    for start, end, key in sorted(intervals, key=lambda item: item[0]):
        if merged and merged[-1][2] == key and (start - merged[-1][1]).total_seconds() <= 60:
            old_start, old_end, _ = merged[-1]
            merged[-1] = (old_start, max(old_end, end), key)
        else:
            merged.append((start, end, key))
    for start, end, key in merged:
        if (end - start).total_seconds() >= minimum_minutes * 60:
            counts[key] += 1
    for duration, key in standalone:
        if duration >= minimum_minutes * 60:
            counts[key] += 1
    additions = [display[key] for key, count in counts.most_common() if count >= minimum_occurrences]
    if not additions:
        return None
    return {
        "path": "presence.heavy_apps",
        "value": existing_list + additions,
        "current": existing_list,
        "rationale": ", ".join(f"{display[key]} had {counts[key]} sessions of at least {minimum_minutes} minutes" for key in counts if counts[key] >= minimum_occurrences),
        "samples": sum(counts[key] for key in counts if counts[key] >= minimum_occurrences),
    }
