"""Atomic JSON state persistence for the smart_room runtime.

State is written to state.json atomically (write tmp + rename).
Read on startup, written on every meaningful state change.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from hermes_constants import get_hermes_home
from utils import atomic_json_write

from plugins.smart_room.runtime.models import RoomState

logger = logging.getLogger(__name__)
_events_lock = threading.Lock()
_locations_lock = threading.Lock()
_state_file_lock = threading.Lock()
_last_location_keys: Dict[str, str] = {}


def state_path() -> Path:
    return Path(get_hermes_home()) / "smart_room" / "state.json"


def load_state() -> RoomState:
    """Load state, recovering the previous atomic snapshot when necessary."""
    p = state_path()
    if not p.is_file():
        return RoomState()
    backup = p.with_suffix(".json.bak")
    for candidate in (p, backup):
        try:
            state = RoomState.from_dict(json.loads(candidate.read_text(encoding="utf-8")))
            if candidate == backup:
                shutil.copy2(backup, p)
                logger.error("Recovered corrupt Smart Room state from %s", backup)
            return state
        except Exception as exc:
            logger.warning("Failed to load state from %s: %s", candidate, exc)
    try:
        p.replace(p.with_suffix(".json.corrupt"))
    except OSError:
        pass
    return RoomState()


def save_state(state: RoomState) -> None:
    """Atomically write state to disk."""
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    backup = p.with_suffix(".json.bak")
    data = state.to_dict()
    with _state_file_lock:
        if p.is_file():
            shutil.copy2(p, backup)
        for attempt in range(5):
            try:
                atomic_json_write(p, data)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    logger.debug("State saved (event_id=%d)", state.event_id)


def load_config() -> Dict[str, Any]:
    """Load smart_room config from config.yaml smart_room section.

    Falls back to defaults if not configured.
    """
    try:
        import yaml
        from hermes_cli.config import cfg_get, load_config as load_hermes_config

        return cfg_get(load_hermes_config(), "smart_room", default={}) or {}
    except Exception:
        return {}


def events_path() -> Path:
    return Path(get_hermes_home()) / "smart_room" / "events.jsonl"


def locations_path() -> Path:
    return Path(get_hermes_home()) / "smart_room" / "locations.jsonl"


def append_location_report(topic: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Preserve one OwnTracks report as append-only local JSONL."""
    received_at = datetime.now(timezone.utc).isoformat()
    try:
        reported_at = datetime.fromtimestamp(float(payload.get("tst")), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        reported_at = received_at
    regions = payload.get("inregions")
    zone = str(payload.get("desc") or (regions[0] if isinstance(regions, list) and regions else "")).strip().lower()
    record = {
        "received_at": received_at,
        "reported_at": reported_at,
        "topic": str(topic),
        "type": str(payload.get("_type") or "unknown"),
        "event": str(payload.get("event") or ""),
        "zone": zone,
        "latitude": payload.get("lat"),
        "longitude": payload.get("lon"),
        "accuracy_m": payload.get("acc"),
        "altitude_m": payload.get("alt"),
        "velocity_kmh": payload.get("vel"),
        "course": payload.get("cog"),
        "battery_percent": payload.get("batt"),
        "trigger": payload.get("t"),
        "connection": payload.get("conn"),
        "data": payload,
    }
    path = locations_path()
    key = json.dumps([str(topic), reported_at, payload], ensure_ascii=False, sort_keys=True)
    with _locations_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        path_key = str(path)
        if path_key not in _last_location_keys and path.exists():
            last_line = ""
            with path.open("r", encoding="utf-8") as handle:
                for last_line in handle:
                    pass
            try:
                previous = json.loads(last_line)
                _last_location_keys[path_key] = json.dumps(
                    [previous.get("topic", ""), previous.get("reported_at", ""), previous.get("data", {})],
                    ensure_ascii=False,
                    sort_keys=True,
                )
            except (json.JSONDecodeError, AttributeError):
                pass
        if _last_location_keys.get(path_key) == key:
            return {**record, "duplicate": True}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        _last_location_keys[path_key] = key
    return record


def load_location_reports(
    *, limit: int = 20, since: str = "", until: str = "", zone: str = ""
) -> list[Dict[str, Any]]:
    """Read the latest matching OwnTracks reports without loading the whole log."""
    path = locations_path()
    if not path.exists():
        return []
    # ponytail: JSONL scan is O(n); move to SQLite only when this log becomes measurably slow.
    matches: deque[Dict[str, Any]] = deque(maxlen=max(1, min(int(limit), 500)))
    zone = zone.strip().lower()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            stamp = str(record.get("reported_at") or record.get("received_at") or "")
            if since and stamp < since:
                continue
            if until and stamp > until:
                continue
            if zone and str(record.get("zone") or "").lower() != zone:
                continue
            matches.append(record)
    return list(matches)


def append_transition(event: Dict[str, Any]) -> None:
    """Append one meaningful transition and mirror it to the Mind activity feed."""
    path = events_path()
    with _events_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > 500:
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(lines[-500:]) + "\n", encoding="utf-8")
            tmp.replace(path)
    try:
        from cron.scheduler import record_subconscious_activity

        record_subconscious_activity(
            source="world",
            outcome="diff_silent",
            summary=str(event.get("summary") or event.get("type") or "Room changed"),
            diff=json.dumps(event, ensure_ascii=False),
        )
    except Exception:
        logger.debug("Failed to append smart-room activity", exc_info=True)


def publish_welcome(message: str) -> None:
    """Send one room greeting through Marvi's existing proactive delivery lane."""
    try:
        from cron.scheduler import record_subconscious_activity

        record_subconscious_activity(
            source="smart_room_welcome",
            outcome="message",
            summary="Smart Room welcome",
            thought=message,
        )
    except Exception:
        logger.debug("Failed to publish smart-room welcome", exc_info=True)


def publish_alarm(alarm_id: str, message: str, *, active: bool) -> None:
    """Surface alarm speech/session lifecycle through Desktop's proactive lane."""
    try:
        from cron.scheduler import record_subconscious_activity

        record_subconscious_activity(
            source="smart_room_alarm",
            job_id=alarm_id,
            outcome="message" if active else "diff_silent",
            summary="Smart Room alarm" if active else "Alarm acknowledged",
            thought=message if active else None,
        )
    except Exception:
        logger.debug("Failed to publish smart-room alarm", exc_info=True)


def publish_cognition(message: str, *, correlation_id: str) -> None:
    """Deliver a deliberate Smart Room cognition message through Marvi."""
    try:
        from cron.scheduler import record_subconscious_activity

        record_subconscious_activity(
            source="smart_room_cognition",
            job_id=correlation_id,
            outcome="message",
            summary="Smart Room noticed something",
            thought=message,
        )
    except Exception:
        logger.debug("Failed to publish Smart Room cognition", exc_info=True)


def publish_gesture_command(command: str) -> None:
    """Bridge a reviewed hand command to the active Desktop surface."""
    try:
        from cron.scheduler import record_subconscious_activity

        record_subconscious_activity(
            source="smart_room_gesture",
            outcome="message",
            summary="Smart Room gesture",
            thought=f"__gesture__:{command}",
        )
    except Exception:
        logger.debug("Failed to publish Smart Room gesture", exc_info=True)


def load_transition_events(after_id: int = 0) -> list[Dict[str, Any]]:
    path = events_path()
    if not path.exists():
        return []
    events: list[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and int(event.get("id", 0)) > after_id:
            events.append(event)
    return events
