"""World trigger — fires an immediate subconscious tick on a meaningful
smart-room event, instead of waiting for the next scheduled tick (default
every 20 minutes, ``subconscious.interval``).

Modeled on ``gateway/idle_trigger.py``'s shape: started as an
``asyncio.create_task`` from ``gateway/run.py`` alongside the other
best-effort background watchers, right next to the idle-trigger startup.
Reuses the SAME built-in subconscious tick job (``cron.subconscious.
trigger_tick`` — no second engine), tagged with ``reason="world"`` so the
activity feed can attribute the run (see
``cron.scheduler._consume_pending_trigger_reason``), exactly like
``idle_trigger.py`` does with ``reason="idle"``.

Reads transition events from the smart_room plugin's local, cursor-based
event log (``plugins.smart_room.runtime.state_store.load_transition_events``)
— guarded import: if the plugin package isn't installed (or one of its pip
extras, e.g. ``ai-edge-litert``, is missing), :func:`watch` returns
immediately without ever polling. Every install that never enabled the
plugin behaves exactly as before this feature existed.

Also nudges ``gateway/flow_gate.py``'s held-delivery poll
(:func:`gateway.flow_gate.request_flush_check`) on an owner-arrival-in-room
event — walking into the room is a natural moment to receive what Marvi has
been holding (config ``smart_room.trigger.flush_on_arrival``, default true).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# How often the watcher polls the smart_room event log. Coarser than a tight
# loop but fine-grained enough that a real-time event doesn't sit for the
# rest of a 20-minute scheduled-tick window. Config: smart_room.trigger.poll_seconds.
DEFAULT_POLL_SECONDS = 2.0

# At most one world-triggered tick per this many seconds, regardless of how
# many wake-worthy events land in between. Config: smart_room.trigger.debounce_seconds.
DEFAULT_DEBOUNCE_SECONDS = 10 * 60

# Config: smart_room.trigger.flush_on_arrival.
DEFAULT_FLUSH_ON_ARRIVAL = True

# A presence_detected within this long of the preceding presence_cleared
# reads as a routine occupancy flicker (stood up for a minute, mmWave lost
# the signal briefly) — not worth an out-of-band tick. At or above it, the
# room was genuinely empty for a while and someone returning is meaningful.
LONG_VACANCY_SECONDS = 2 * 60 * 60  # 2h


# ---------------------------------------------------------------------------
# Pure predicates — no I/O, no clock reads, fully unit-testable.
# ---------------------------------------------------------------------------


def is_wake_worthy(event: Dict[str, Any]) -> bool:
    """True when a smart-room transition event deserves an immediate
    subconscious tick rather than waiting for the next scheduled one.

    Operates only on the event dict as produced by
    ``plugins.smart_room.runtime.state_store.load_transition_events``
    (fields: ``id``, ``at``, ``type``, ``summary``, plus per-type payload —
    see ``plugins/smart_room/runtime/app.py::_emit_event``'s
    append_transition allow-list). ``vacancy_seconds`` is NOT one of the
    runtime's own event fields — it's stamped onto a copy of a
    ``presence_detected`` event by :func:`_enrich_with_vacancy` below,
    since how long the room was empty beforehand isn't something a single
    event's own type/payload encodes; the watcher tracks it by pairing
    consecutive ``presence_cleared`` / ``presence_detected`` events across
    polls.

    Meaningful (wake-worthy):
      - owner arrival home: ``phone_location_changed``, transition=arrive,
        zone=home (OwnTracks — the only source that ever emits this type).
      - owner leaves home: ``phone_location_changed``, transition=leave,
        zone=home.
      - room becomes occupied after >=2h vacancy: ``presence_detected``
        with ``vacancy_seconds >= LONG_VACANCY_SECONDS``.
      - device offline transitions: ``device_offline``.

    NOT wake-worthy (explicitly excluded):
      - light on/off, mode changes (``mode_changed``).
      - routine occupancy flicker: ``presence_detected``/``presence_cleared``
        without a long-enough preceding vacancy gap.
      - everything else (``sleep_cancelled``, ``alarm_acknowledged``, ...).
    """
    if not isinstance(event, dict):
        return False
    event_type = str(event.get("type") or "")

    if event_type == "phone_location_changed":
        transition = str(event.get("transition") or "").strip().lower()
        zone = str(event.get("zone") or "").strip().lower()
        return zone == "home" and transition in {"arrive", "leave"}

    if event_type == "device_offline":
        return True

    if event_type in {
        "room_entry", "room_presence_unverified", "vision_camera_offline",
        "sensor_vision_conflict", "gesture_voice_mode_requested",
    }:
        return True

    if event_type == "vision_identity_state":
        return str(event.get("identity_state") or "") == "unknown_person"

    if event_type == "presence_detected":
        vacancy_seconds = event.get("vacancy_seconds")
        if vacancy_seconds is None:
            return False
        try:
            return float(vacancy_seconds) >= LONG_VACANCY_SECONDS
        except (TypeError, ValueError):
            return False

    return False


def is_arrival_event(event: Dict[str, Any]) -> bool:
    """True when an event represents the owner arriving in the room.

    Broader than :func:`is_wake_worthy` on purpose — no vacancy-length
    filter, since walking in after a short absence is still a perfectly
    natural moment to receive a held delivery, even when it isn't itself
    worth an out-of-band subconscious tick. Used only to decide whether to
    nudge ``gateway/flow_gate.py``.

      - ``presence_detected``: a BLE/mmWave occupied transition. The
        runtime only ever emits this on a genuine absence -> presence
        transition (never while already present — see
        ``plugins/smart_room/runtime/app.py``'s ``_on_ble_presence``/
        ``_poll_devices``, both guarded by ``if not was_present and
        present``), so every occurrence is a real arrival, not a flicker.
      - ``phone_location_changed`` arrive/home: OwnTracks arrival at home.
    """
    if not isinstance(event, dict):
        return False
    event_type = str(event.get("type") or "")
    if event_type == "presence_detected":
        return True
    if event_type == "phone_location_changed":
        return (
            str(event.get("transition") or "").strip().lower() == "arrive"
            and str(event.get("zone") or "").strip().lower() == "home"
        )
    return False


def should_trigger(
    *,
    last_triggered_monotonic: Optional[float],
    now_monotonic: float,
    debounce_seconds: float,
) -> bool:
    """Pure debounce predicate — at most one world-triggered tick per
    ``debounce_seconds`` window. Mirrors ``gateway.idle_trigger.should_fire``'s
    shape (a plain function over primitives, no gateway/event loop needed)."""
    if last_triggered_monotonic is None:
        return True
    return (now_monotonic - last_triggered_monotonic) >= debounce_seconds


def _parse_at(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


class _WorldWatchState:
    """Mutable bookkeeping carried between poll iterations.

    In-memory only, like ``idle_trigger``'s ``last_fired_for_inbound_at`` —
    resets on gateway restart. Losing at most one in-flight vacancy-duration
    computation across a restart (the watcher just re-establishes its
    baseline cursor silently, same as any subconscious fetcher's first-run
    contract) is an acceptable trade for not needing a second on-disk cursor
    file alongside the smart_room fetcher's own snapshot store.
    """

    def __init__(self) -> None:
        self.after_cursor = 0
        self.vacant_since: Optional[datetime] = None
        self.last_triggered_monotonic: Optional[float] = None
        self.first_poll = True


def _enrich_with_vacancy(event: Dict[str, Any], state: "_WorldWatchState") -> Dict[str, Any]:
    """Track ``presence_cleared`` / ``presence_detected`` pairs across polls
    to compute how long the room was empty before a given
    ``presence_detected``, stamping it onto a COPY of the event as
    ``vacancy_seconds`` for :func:`is_wake_worthy` to consult. Every other
    event type passes through unchanged (returns the same object).
    """
    event_type = event.get("type")
    if event_type == "presence_cleared":
        parsed = _parse_at(event.get("at"))
        if parsed is not None:
            state.vacant_since = parsed
        return event
    if event_type != "presence_detected":
        return event
    if state.vacant_since is None:
        return event
    at = _parse_at(event.get("at"))
    vacant_since = state.vacant_since
    state.vacant_since = None
    if at is None:
        return event
    vacancy_seconds = (at - vacant_since).total_seconds()
    if vacancy_seconds < 0:
        return event
    enriched = dict(event)
    enriched["vacancy_seconds"] = vacancy_seconds
    return enriched


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _poll_seconds() -> float:
    from hermes_cli.config import cfg_get, load_config

    value = cfg_get(load_config(), "smart_room", "trigger", "poll_seconds", default=DEFAULT_POLL_SECONDS)
    try:
        parsed = float(value)
        return parsed if parsed > 0 else DEFAULT_POLL_SECONDS
    except (TypeError, ValueError):
        return DEFAULT_POLL_SECONDS


def _debounce_seconds() -> float:
    from hermes_cli.config import cfg_get, load_config

    value = cfg_get(
        load_config(), "smart_room", "trigger", "debounce_seconds", default=DEFAULT_DEBOUNCE_SECONDS
    )
    try:
        parsed = float(value)
        return parsed if parsed >= 0 else DEFAULT_DEBOUNCE_SECONDS
    except (TypeError, ValueError):
        return DEFAULT_DEBOUNCE_SECONDS


def _flush_on_arrival_enabled() -> bool:
    from hermes_cli.config import cfg_get, load_config

    return bool(
        cfg_get(
            load_config(), "smart_room", "trigger", "flush_on_arrival", default=DEFAULT_FLUSH_ON_ARRIVAL
        )
    )


def _nudge_flow_gate() -> None:
    """Best-effort: ask gateway/flow_gate.py to re-check any held delivery
    right now. Guarded import — flow_gate is always present in this repo,
    but failure here must never take down the world-trigger watcher."""
    try:
        from gateway.flow_gate import request_flush_check

        request_flush_check()
    except ImportError:
        pass
    except Exception:
        logger.debug("world-trigger: flow-gate nudge failed", exc_info=True)


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


async def watch(gateway, *, interval: Optional[float] = None) -> None:
    """Background watcher: poll the smart_room plugin's transition-event log
    and fire an immediate subconscious tick on the first wake-worthy event
    seen in a batch (debounced), plus nudge the flow gate on any
    owner-arrival event. Never raises out of the loop — mirrors
    ``gateway.idle_trigger.watch``.

    Returns immediately, without ever polling, when the smart_room plugin
    package (or one of its pip extras) isn't importable — the common case
    for an install that never enabled the plugin.
    """
    try:
        from plugins.smart_room.runtime.state_store import load_transition_events
    except ImportError:
        logger.debug("world-trigger: smart_room plugin not installed; watcher not starting")
        return

    state = _WorldWatchState()
    poll_interval = interval if interval is not None else _poll_seconds()
    await asyncio.sleep(min(poll_interval, 30.0))  # let startup settle
    while getattr(gateway, "_running", True):
        try:
            await asyncio.sleep(poll_interval)
            if not getattr(gateway, "_running", True):
                return

            events = await asyncio.to_thread(load_transition_events, state.after_cursor)
            if events:
                state.after_cursor = max(
                    state.after_cursor, *(int(e.get("id", 0)) for e in events)
                )

            if state.first_poll:
                # Establish the baseline cursor silently -- never wake the
                # tick or nudge the flow gate for history that predates this
                # gateway process (mirrors every subconscious fetcher's
                # first-run contract: never dump the backlog on startup).
                state.first_poll = False
                for event in events:
                    _enrich_with_vacancy(event, state)
                continue

            fire_tick = False
            arrival_seen = False
            for event in events:
                enriched = _enrich_with_vacancy(event, state)
                if not fire_tick and is_wake_worthy(enriched):
                    fire_tick = True
                if not arrival_seen and is_arrival_event(enriched):
                    arrival_seen = True

            if arrival_seen and _flush_on_arrival_enabled():
                _nudge_flow_gate()

            if not fire_tick:
                continue

            now_monotonic = time.monotonic()
            if not should_trigger(
                last_triggered_monotonic=state.last_triggered_monotonic,
                now_monotonic=now_monotonic,
                debounce_seconds=_debounce_seconds(),
            ):
                logger.debug("world-trigger: wake-worthy event debounced")
                continue

            from cron.subconscious import trigger_tick

            if trigger_tick(reason="world"):
                state.last_triggered_monotonic = now_monotonic
                logger.info("world-trigger: fired subconscious tick on a smart-room event")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the watcher must never crash the gateway
            logger.debug("world-trigger watcher iteration error", exc_info=True)
