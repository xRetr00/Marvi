"""Durable, bounded follow-up initiatives for subconscious reasoning."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now
from utils import atomic_replace

MAX_NEW_PER_RUN = 5
MAX_EXECUTIONS_PER_DAY = 3
VALID_TRIGGERS = frozenset({"next_tick", "at_time", "on_rhythm", "on_presence"})
VALID_STATUSES = frozenset({"pending", "done", "cancelled", "expired"})


def initiatives_path() -> Path:
    return get_hermes_home() / "subconscious" / "initiatives.json"


def _empty() -> Dict[str, Any]:
    return {"initiatives": [], "budget": {"date": date.today().isoformat(), "used": 0}}


def load_state() -> Dict[str, Any]:
    path = initiatives_path()
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("initiatives"), list):
        return _empty()
    budget = data.get("budget") if isinstance(data.get("budget"), dict) else {}
    today = date.today().isoformat()
    if budget.get("date") != today:
        budget = {"date": today, "used": 0}
    data["budget"] = budget
    return data


def _save(state: Dict[str, Any]) -> None:
    path = initiatives_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".initiatives_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
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


def list_initiatives(*, status: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = load_state()["initiatives"]
    return [row for row in rows if status is None or row.get("status") == status]


def add_initiatives(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    state = load_state()
    existing = state["initiatives"]
    created: List[Dict[str, Any]] = []
    for raw in items[:MAX_NEW_PER_RUN]:
        trigger = str(raw.get("trigger") or "next_tick")
        detail = str(raw.get("detail") or "").strip()
        if not detail or trigger not in VALID_TRIGGERS:
            continue
        dedup = str(raw.get("dedup_key") or detail.casefold())
        if any(row.get("dedup_key") == dedup and row.get("status") == "pending" for row in existing):
            continue
        now = _hermes_now().isoformat()
        row = {
            "id": uuid.uuid4().hex[:12],
            "detail": detail,
            "trigger": trigger,
            "trigger_value": raw.get("trigger_value"),
            "expires_at": raw.get("expires_at"),
            "dedup_key": dedup,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
        }
        existing.append(row)
        created.append(row)
    if created:
        _save(state)
    return created


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def due_initiatives(*, rhythm: Optional[str] = None, presence: Optional[str] = None) -> List[Dict[str, Any]]:
    state = load_state()
    budget = state["budget"]
    remaining = max(0, MAX_EXECUTIONS_PER_DAY - int(budget.get("used") or 0))
    now = _hermes_now()
    due: List[Dict[str, Any]] = []
    changed = False
    for row in state["initiatives"]:
        if row.get("status") != "pending":
            continue
        expires = _parse_iso(row.get("expires_at"))
        if expires and now >= expires:
            row["status"] = "expired"
            row["updated_at"] = now.isoformat()
            changed = True
            continue
        trigger = row.get("trigger")
        value = row.get("trigger_value")
        is_due = trigger == "next_tick"
        if trigger == "at_time":
            target = _parse_iso(value)
            is_due = bool(target and now >= target)
        elif trigger == "on_rhythm":
            is_due = bool(rhythm and rhythm == value)
        elif trigger == "on_presence":
            is_due = bool(presence and presence == value)
        if is_due and len(due) < remaining:
            due.append(row)
    if changed:
        _save(state)
    return due


def apply_results(results: List[Dict[str, Any]]) -> None:
    state = load_state()
    by_id = {row.get("id"): row for row in state["initiatives"]}
    completed = 0
    for result in results:
        row = by_id.get(result.get("id"))
        if not row or row.get("status") != "pending":
            continue
        outcome = result.get("outcome")
        if outcome in {"done", "skip"}:
            row["status"] = "done"
            completed += 1
        elif outcome == "cancel":
            row["status"] = "cancelled"
        elif outcome != "retry":
            continue
        row["result"] = str(result.get("result") or "")[:2000]
        row["updated_at"] = _hermes_now().isoformat()
    if completed:
        state["budget"]["used"] = min(
            MAX_EXECUTIONS_PER_DAY,
            int(state["budget"].get("used") or 0) + completed,
        )
    if results:
        _save(state)


def cancel_initiative(ref: str) -> bool:
    state = load_state()
    for row in state["initiatives"]:
        if row.get("id") == ref:
            row["status"] = "cancelled"
            row["updated_at"] = _hermes_now().isoformat()
            _save(state)
            return True
    return False
