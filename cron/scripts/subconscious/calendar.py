"""Google Calendar delta fetcher -- Composio smart sync for Marvi.

Uses the Calendar API's ``syncToken`` mechanism (via Composio) instead of
listing every event: the cursor is an opaque ``syncToken``, and each fetch
asks "what changed since this token" rather than re-scanning the calendar.

First run establishes the baseline ``syncToken`` and reports nothing changed
-- per Contract 1 / Workstream C task 3, a fresh surface must never dump the
whole calendar on its first tick.

Google's sync-token contract can invalidate a token server-side (expired /
too old / calendar reset), which the API surfaces as an HTTP 410 GONE.
``composio_client`` normalizes that into :class:`ComposioSyncTokenExpired`;
this module catches it specifically and re-baselines exactly like a first
run -- it must NOT dump the full event list just because the token died.

Diff scope is deliberately narrow: only events starting in the next 7 days,
and only what *changed* about them (added, rescheduled, renamed, cancelled).
Today's full agenda is the morning briefing's job, not this diff's -- this
module only ever reports deltas.
"""

from __future__ import annotations

from datetime import date as _date_cls, datetime, timedelta
from typing import Any, Dict, List, Optional

from cron.scripts.subconscious.composio_client import (
    ComposioSyncTokenExpired,
    get_client,
    unwrap_payload,
)
from cron.scripts.subconscious.snapshot_store import SurfaceStore
from hermes_time import now as _hermes_now

APP = "calendar"

# Composio Google Calendar toolkit action slug, kept as a module constant so
# a future SDK rename is a one-line change instead of a find-and-replace.
# Assumed request shape mirrors the underlying Calendar API's
# ``events.list``: ``sync_token`` (omitted on the very first/re-baseline
# call) plus ``single_events`` (expand recurring events into individual
# occurrences, required for per-occurrence start times). Assumed response
# shape: ``{"items": [...], "nextSyncToken": "...", "nextPageToken": "..."}``
# -- each item shaped like a Calendar API ``Event`` resource (``id``,
# ``summary``, ``status``, ``start``: {``dateTime``|``date``}).
ACTION_LIST_EVENTS = "GOOGLECALENDAR_EVENTS_LIST"

# Only report on events landing in this window. Deliberately excludes
# "today's agenda" -- that's the morning briefing's job, not this diff's.
REPORT_WINDOW_DAYS = 7

# Cap on how many changed-event lines land in one diff -- a bulk
# reschedule/import shouldn't turn one tick into a wall of text.
MAX_SUMMARIZED_EVENTS = 15

# How many event ids to remember across runs (title/when at last sight), so
# a rescheduled/renamed/cancelled event can be diffed correctly. Bounded so
# the snapshot file doesn't grow without limit.
MAX_REMEMBERED_EVENTS = 500

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _extract_sync_token(payload: Any) -> Optional[str]:
    body = unwrap_payload(payload)
    if isinstance(body, dict):
        token = body.get("nextSyncToken") or body.get("next_sync_token")
        if token:
            return str(token)
    return None


def _extract_events(payload: Any) -> List[Dict[str, Any]]:
    body = unwrap_payload(payload)
    if isinstance(body, dict):
        items = body.get("items") or body.get("events")
        if isinstance(items, list):
            return [e for e in items if isinstance(e, dict)]
    if isinstance(body, list):
        return [e for e in body if isinstance(e, dict)]
    return []


def _parse_event_start(start: Any) -> Optional[datetime]:
    """Parse a Calendar API ``start`` object into a tz-aware datetime.

    Handles both timed events (``dateTime``) and all-day events (``date``,
    normalized to midnight in the configured timezone for window comparison).
    """
    if not isinstance(start, dict):
        return None
    date_time = start.get("dateTime")
    if date_time:
        text = str(date_time)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_hermes_now().tzinfo)
        return dt
    date_only = start.get("date")
    if date_only:
        try:
            d = _date_cls.fromisoformat(str(date_only))
        except ValueError:
            return None
        return datetime(d.year, d.month, d.day, tzinfo=_hermes_now().tzinfo)
    return None


def _format_when(dt: datetime, all_day: bool) -> str:
    """Portable human-readable formatting -- avoids ``%-d``/``%-I`` strftime
    extensions, which aren't available on Windows' CRT."""
    weekday = _WEEKDAYS[dt.weekday()]
    month = _MONTHS[dt.month - 1]
    if all_day:
        return f"{weekday} {month} {dt.day} (all day)"
    hour12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{weekday} {month} {dt.day}, {hour12}:{dt.minute:02d} {ampm}"


def _fetch_baseline_sync_token(client: Any, store: SurfaceStore) -> None:
    """Establish (or re-establish, after a 410) the baseline ``syncToken``
    only. Never report a diff here -- that would mean summarizing the whole
    calendar as "new", exactly the anti-pattern this sync avoids."""
    payload = client.execute_action(ACTION_LIST_EVENTS, {"single_events": True})
    new_token = _extract_sync_token(payload)
    if new_token:
        store.set_cursor({"sync_token": new_token})


def fetch_delta(store: SurfaceStore) -> Optional[str]:
    """Fetch Calendar's delta since the stored ``syncToken`` and summarize
    changes landing in the next :data:`REPORT_WINDOW_DAYS` days.

    Returns ``None`` when nothing reportable changed (or on first run /
    re-baseline), else a compact multi-line summary of added/rescheduled/
    renamed/cancelled events.
    """
    client = get_client()

    sync_token = store.cursor.get("sync_token")
    if store.is_first_run() or not sync_token:
        _fetch_baseline_sync_token(client, store)
        return None

    try:
        payload = client.execute_action(
            ACTION_LIST_EVENTS, {"sync_token": sync_token, "single_events": True}
        )
    except ComposioSyncTokenExpired:
        # The syncToken is no longer valid server-side (410 GONE). Treat
        # exactly like a first run: reset the cursor and re-baseline
        # WITHOUT dumping every event as a "diff".
        store.set_cursor({})
        _fetch_baseline_sync_token(client, store)
        return None

    events = _extract_events(payload)
    new_token = _extract_sync_token(payload) or sync_token
    store.update_cursor(sync_token=new_token)

    if not events:
        return None

    now = _hermes_now()
    window_end = now + timedelta(days=REPORT_WINDOW_DAYS)
    known_events: Dict[str, Dict[str, Any]] = dict(store.state.get("known_events") or {})

    lines: List[str] = []
    for event in events:
        event_id = event.get("id")
        if not event_id:
            continue
        status = str(event.get("status") or "confirmed").lower()
        title = event.get("summary") or "(untitled event)"
        start = event.get("start")
        dt = _parse_event_start(start)
        all_day = bool(isinstance(start, dict) and start.get("date") and not start.get("dateTime"))
        prev = known_events.get(event_id)

        if status == "cancelled":
            if prev is not None:
                title_out = prev.get("title") or title
                when_out = prev.get("when") or "unknown time"
                if len(lines) < MAX_SUMMARIZED_EVENTS:
                    lines.append(f"{title_out} — {when_out} (cancelled)")
            elif dt is not None and now <= dt <= window_end:
                if len(lines) < MAX_SUMMARIZED_EVENTS:
                    lines.append(f"{title} — {_format_when(dt, all_day)} (cancelled)")
            known_events.pop(event_id, None)
            continue

        if dt is None or not (now <= dt <= window_end):
            # Outside the reportable window (or unparseable) -- remember its
            # last-known shape so a later change/cancellation still diffs
            # correctly, but don't surface a line for it now.
            known_events[event_id] = {"title": title, "when": None}
            continue

        when_str = _format_when(dt, all_day)
        if len(lines) < MAX_SUMMARIZED_EVENTS:
            if prev is None:
                lines.append(f"{title} — {when_str} (new)")
            else:
                changes = []
                if prev.get("title") != title:
                    changes.append("renamed")
                if prev.get("when") != when_str:
                    changes.append("rescheduled")
                if not changes:
                    changes.append("updated")
                lines.append(f"{title} — {when_str} ({', '.join(changes)})")
        known_events[event_id] = {"title": title, "when": when_str}

    if len(known_events) > MAX_REMEMBERED_EVENTS:
        known_events = dict(list(known_events.items())[-MAX_REMEMBERED_EVENTS:])
    store.update_state(known_events=known_events)

    if not lines:
        return None

    header = f"Calendar: {len(lines)} change(s) in the next {REPORT_WINDOW_DAYS} days"
    return header + "\n" + "\n".join(f"  - {line}" for line in lines)
