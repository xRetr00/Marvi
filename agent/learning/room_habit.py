"""Rolling histogram of explicit Smart Room actions."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, Optional

from hermes_constants import get_hermes_home
from utils import atomic_replace

MAX_OBSERVATIONS = 1_000
_state_lock = threading.RLock()


def state_path() -> Path:
    return get_hermes_home().resolve() / "learning" / "room_habits.json"


def load_state() -> Dict[str, Any]:
    with _state_lock:
        try:
            data = json.loads(state_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"last_event_id": 0, "observations": []}
        except (OSError, json.JSONDecodeError):
            return {"last_event_id": 0, "observations": []}


def save_state(state: Dict[str, Any]) -> None:
    with _state_lock:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".room_habits_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            atomic_replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _observation(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if event.get("source") != "manual":
        return None
    kind = str(event.get("type") or "")
    if kind == "mode_changed":
        action, arguments = "set_mode", {"mode": event.get("mode")}
    elif kind == "light_changed" and event.get("success") is not False:
        arguments = {key: event.get(key) for key in ("on", "brightness", "color_temp", "rgb") if event.get(key) is not None}
        action = "set_light"
    elif kind == "sleep_cancelled":
        action, arguments = "cancel_sleep", {"reason": event.get("reason")}
    else:
        return None
    try:
        stamp = datetime.fromisoformat(str(event.get("at") or "").replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None
    return {
        "event_id": int(event.get("id") or 0),
        "at": stamp.isoformat(),
        "weekday": stamp.weekday(),
        "minute": stamp.hour * 60 + stamp.minute,
        "action": action,
        "arguments": arguments,
    }


def accumulate(events: Iterable[Dict[str, Any]], state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    result = dict(state or {"last_event_id": 0, "observations": []})
    last_id = int(result.get("last_event_id") or 0)
    observations = list(result.get("observations") or [])
    for event in sorted(events, key=lambda item: int(item.get("id") or 0)):
        event_id = int(event.get("id") or 0)
        if event_id <= last_id:
            continue
        observation = _observation(event)
        if observation:
            observations.append(observation)
        last_id = max(last_id, event_id)
    result["last_event_id"] = last_id
    result["observations"] = observations[-MAX_OBSERVATIONS:]
    histogram: Dict[str, Dict[str, Any]] = {}
    for row in result["observations"]:
        bucket = int(row.get("minute") or 0) // 60
        key = f"{row.get('weekday')}|{bucket}|{row.get('action')}|{json.dumps(row.get('arguments') or {}, sort_keys=True, separators=(',', ':'))}"
        entry = histogram.setdefault(
            key,
            {
                "weekday": row.get("weekday"),
                "hour": bucket,
                "action": row.get("action"),
                "arguments": row.get("arguments") or {},
                "count": 0,
                "minutes": [],
            },
        )
        entry["count"] += 1
        entry["minutes"].append(row.get("minute"))
    result["histogram"] = histogram
    return result


def propose(state: Dict[str, Any], *, minimum_occurrences: int = 4, variance_minutes: int = 30) -> list[Dict[str, Any]]:
    groups: Dict[tuple[str, str, int], list[Dict[str, Any]]] = defaultdict(list)
    for row in state.get("observations") or []:
        args_key = json.dumps(row.get("arguments") or {}, sort_keys=True, separators=(",", ":"))
        groups[(str(row.get("action")), args_key, int(row.get("weekday", -1)))].append(row)
    proposals = []
    for (action, args_key, weekday), rows in groups.items():
        if len(rows) < minimum_occurrences or weekday not in range(7):
            continue
        minutes = [int(row.get("minute") or 0) for row in rows]
        center = int(round(mean(minutes)))
        if max(abs(value - center) for value in minutes) > variance_minutes:
            continue
        hour, minute = divmod(center, 60)
        arguments = json.loads(args_key)
        schedule = f"{minute} {hour} * * {weekday}"
        if action == "set_mode":
            prompt = f"Call smart_room_set_mode with mode={arguments.get('mode')!r}. Return [SILENT] after it succeeds."
            title = f"Set room to {arguments.get('mode')} each {['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'][weekday]}"
        elif action == "set_light":
            prompt = f"Call smart_room_set_light with exactly these arguments: {json.dumps(arguments, sort_keys=True)}. Return [SILENT] after it succeeds."
            title = f"Repeat your room lighting each {['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'][weekday]}"
        else:
            prompt = "Call smart_room_cancel_sleep. Return [SILENT] after it succeeds."
            title = f"Cancel that sleep window each {['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'][weekday]}"
        proposals.append({
            "title": title,
            "description": f"You performed this room action {len(rows)} times around {hour:02d}:{minute:02d}.",
            "job_spec": {"prompt": prompt, "schedule": schedule, "name": title, "enabled_toolsets": ["smart_room"]},
            "dedup_key": f"learning:room:{action}:{weekday}:{hour:02d}:{minute // 15}",
            "samples": len(rows),
        })
    return proposals
