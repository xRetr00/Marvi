"""Meaningful smart-room transition deltas for the subconscious tick."""

from __future__ import annotations

from typing import Optional

from cron.scripts.subconscious.snapshot_store import SurfaceStore
from plugins.smart_room.runtime.state_store import load_transition_events

APP = "smart_room"


def fetch_delta(store: SurfaceStore) -> Optional[str]:
    from hermes_time import format_timestamp
    after_id = int(store.cursor.get("event_id", 0))
    events = load_transition_events(after_id)
    if store.is_first_run():
        store.set_cursor({"event_id": max((int(e.get("id", 0)) for e in events), default=0)})
        return None
    if not events:
        return None
    store.set_cursor({"event_id": max(int(e.get("id", 0)) for e in events)})
    # Sensor-only, uncertain, visual, and cognition transitions are valuable
    # evidence now that the subconscious can correlate them as a sequence.
    if all(
        event.get("type") in {"he20_occupied", "he20_cleared"}
        and event.get("mode") == "sleep"
        for event in events
    ):
        return None
    lines = []
    for event in events[-20:]:
        detail = ", ".join(
            f"{key}={value}"
            for key, value in event.items()
            if key not in {"id", "at", "type", "summary"} and value is not None
        )
        lines.append(f"- {format_timestamp(event.get('at'))}: {event.get('summary') or event.get('type')}" + (f" ({detail})" if detail else ""))
    return "\n".join(lines)
