"""Persistent goal store — the steering input for Marvi's subconscious.

A *goal* here is a standing, cross-session objective the user wants Marvi to
keep in mind (e.g. "ship the Q3 report", "learn Spanish this year") — NOT the
per-session ``/goal`` Ralph loop in ``hermes_cli/goals.py`` (that's a
turn-by-turn judge loop scoped to one conversation). Goals stored here are
injected into every session's system prompt (see
``agent/system_prompt.py``) and read by the subconscious tick
(``cron/subconscious.py``) so proactive reasoning has something to steer by.

Storage: ``~/.hermes/goals.json``. Mirrors ``cron/jobs.py``'s storage style —
atomic writes (tempfile + ``atomic_replace``), an in-process lock, and 0600
file permissions.

Fields per goal record: ``id``, ``title``, ``detail``, ``status``
(active/paused/done), ``horizon`` (short/long), ``origin`` (user/inferred),
``created``, ``updated``.

``origin`` distinguishes a goal the user wrote themselves from one Marvi
inferred and added on its own (tools/goal_tools.py::_handle_suggest_goal,
subject to a concurrent-inferred-goal cap and title-similarity dedup —
see that module). Backward-compatible: a goal record on disk from before
this field existed reads back as ``origin="user"`` (see :func:`load_goals`).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now
from utils import atomic_replace

logger = logging.getLogger(__name__)

GOALS_FILE = get_hermes_home().resolve() / "goals.json"
_INITIAL_GOALS_FILE = GOALS_FILE

# In-process lock protecting load->modify->save cycles.
_goals_lock = threading.Lock()

VALID_STATUSES = frozenset({"active", "paused", "done"})
VALID_HORIZONS = frozenset({"short", "long"})
VALID_ORIGINS = frozenset({"user", "inferred"})
DEFAULT_STATUS = "active"
DEFAULT_HORIZON = "short"
DEFAULT_ORIGIN = "user"

# Small, editable starters rather than a second goal schema. The UI copies
# one of these through the same add_goal path as a hand-written goal.
GOAL_TEMPLATES = (
    {
        "id": "ship-project",
        "title": "Ship a project",
        "detail": "Keep the next milestone moving, surface blockers early, and notice concrete progress.",
        "horizon": "short",
    },
    {
        "id": "learn-skill",
        "title": "Build a new skill",
        "detail": "Turn repeated practice into a sustainable routine and track meaningful improvement.",
        "horizon": "long",
    },
    {
        "id": "health-routine",
        "title": "Protect a healthy routine",
        "detail": "Notice schedule pressure and help preserve the routine without nagging.",
        "horizon": "long",
    },
    {
        "id": "weekly-review",
        "title": "Run a weekly review",
        "detail": "Review wins, loose ends, priorities, and one realistic next step each week.",
        "horizon": "short",
    },
)


def list_goal_templates() -> List[Dict[str, Any]]:
    return [dict(item) for item in GOAL_TEMPLATES]


def _secure_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def goals_file() -> Path:
    """Resolve the active profile at call time, while keeping test overrides."""
    if GOALS_FILE != _INITIAL_GOALS_FILE:
        return GOALS_FILE
    return get_hermes_home().resolve() / "goals.json"


def _ensure_dir() -> None:
    goals_file().parent.mkdir(parents=True, exist_ok=True)


def _load_raw() -> Dict[str, Any]:
    path = goals_file()
    if not path.exists():
        return {"goals": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("goals.json unreadable (%s); starting empty", e)
        return {"goals": []}
    if isinstance(data, dict) and isinstance(data.get("goals"), list):
        return data
    if isinstance(data, list):
        return {"goals": data}
    logger.warning("goals.json malformed; starting empty")
    return {"goals": []}


def _save_raw(goals: List[Dict[str, Any]]) -> None:
    _ensure_dir()
    path = goals_file()
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".goals_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                {"goals": goals, "updated_at": _hermes_now().isoformat()},
                f,
                indent=2,
            )
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp_path, path)
        _secure_file(path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _normalize_goal(goal: Dict[str, Any]) -> Dict[str, Any]:
    """Backfill fields that may be absent on a goal record written before
    they existed. Currently just ``origin`` (added alongside auto-goals) —
    an old record with no ``origin`` key reads as ``"user"``, never
    ``"inferred"``, so nothing pre-existing is ever mistaken for something
    Marvi added on its own. Read-only: does NOT rewrite the record on disk,
    it only affects what callers see.
    """
    if goal.get("origin") not in VALID_ORIGINS:
        goal = {**goal, "origin": DEFAULT_ORIGIN}
    return goal


def load_goals() -> List[Dict[str, Any]]:
    """Return all goal records (any status)."""
    return [_normalize_goal(g) for g in _load_raw().get("goals", [])]


def list_goals(*, status: Optional[str] = None, horizon: Optional[str] = None) -> List[Dict[str, Any]]:
    """List goals, optionally filtered by status and/or horizon."""
    goals = load_goals()
    if status:
        goals = [g for g in goals if g.get("status") == status]
    if horizon:
        goals = [g for g in goals if g.get("horizon") == horizon]
    return goals


def active_goals() -> List[Dict[str, Any]]:
    """Convenience: goals with status "active" — what the system prompt injects."""
    return list_goals(status="active")


def get_goal(ref: str) -> Optional[Dict[str, Any]]:
    """Resolve a goal by id, 1-based index (in load order), or exact title."""
    goals = load_goals()
    for g in goals:
        if g.get("id") == ref:
            return g
    if ref.isdigit():
        idx = int(ref) - 1
        if 0 <= idx < len(goals):
            return goals[idx]
    for g in goals:
        if str(g.get("title", "")).lower() == ref.lower():
            return g
    return None


def add_goal(
    *,
    title: str,
    detail: str = "",
    horizon: str = DEFAULT_HORIZON,
    status: str = DEFAULT_STATUS,
    origin: str = DEFAULT_ORIGIN,
) -> Dict[str, Any]:
    """Create and persist a new goal. Returns the created record.

    ``origin`` is ``"user"`` for anything a person wrote (manual add, a
    template, or the desktop UI's "Add goal") and ``"inferred"`` for a goal
    Marvi created on its own from the reflection/dreaming inference path
    (tools/goal_tools.py::_handle_suggest_goal) — never set by hand elsewhere.

    Raises ``ValueError`` for an empty title or an invalid status/horizon/origin.
    """
    title = (title or "").strip()
    if not title:
        raise ValueError("title is required")
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}, got {status!r}")
    if horizon not in VALID_HORIZONS:
        raise ValueError(f"horizon must be one of {sorted(VALID_HORIZONS)}, got {horizon!r}")
    if origin not in VALID_ORIGINS:
        raise ValueError(f"origin must be one of {sorted(VALID_ORIGINS)}, got {origin!r}")

    now = _hermes_now().isoformat()
    record = {
        "id": uuid.uuid4().hex[:12],
        "title": title,
        "detail": (detail or "").strip(),
        "status": status,
        "horizon": horizon,
        "origin": origin,
        "created": now,
        "updated": now,
    }
    with _goals_lock:
        goals = _load_raw().get("goals", [])
        goals.append(record)
        _save_raw(goals)
    return record


def update_goal(ref: str, **updates: Any) -> Optional[Dict[str, Any]]:
    """Update a goal's mutable fields (title/detail/status/horizon/origin).

    ``origin`` is normally only set by :func:`add_goal`, but is updatable
    here for the desktop Goals panel's "Keep" action on an inferred goal —
    a one-click flip from ``origin="inferred"`` to ``"user"`` that adopts
    the goal as the user's own, same shape as any other field edit.

    Returns the updated record, or ``None`` if ``ref`` doesn't resolve.
    Raises ``ValueError`` for an invalid status/horizon/origin value.
    """
    if "status" in updates and updates["status"] not in VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}, got {updates['status']!r}")
    if "horizon" in updates and updates["horizon"] not in VALID_HORIZONS:
        raise ValueError(f"horizon must be one of {sorted(VALID_HORIZONS)}, got {updates['horizon']!r}")
    if "origin" in updates and updates["origin"] not in VALID_ORIGINS:
        raise ValueError(f"origin must be one of {sorted(VALID_ORIGINS)}, got {updates['origin']!r}")

    with _goals_lock:
        goals = _load_raw().get("goals", [])
        target = None
        for g in goals:
            if g.get("id") == ref:
                target = g
                break
        if target is None and ref.isdigit():
            idx = int(ref) - 1
            if 0 <= idx < len(goals):
                target = goals[idx]
        if target is None:
            ref_lower = ref.lower()
            for g in goals:
                if str(g.get("title", "")).lower() == ref_lower:
                    target = g
                    break
        if target is None:
            return None

        for field in ("title", "detail", "status", "horizon", "origin"):
            if field in updates and updates[field] is not None:
                value = updates[field]
                if field in ("title", "detail"):
                    value = str(value).strip()
                target[field] = value
        target["updated"] = _hermes_now().isoformat()
        _save_raw(goals)
        return target


def remove_goal(ref: str) -> bool:
    """Delete a goal by id/index/title. Returns True if one was removed."""
    with _goals_lock:
        goals = _load_raw().get("goals", [])
        goal = None
        for g in goals:
            if g.get("id") == ref:
                goal = g
                break
        if goal is None and ref.isdigit():
            idx = int(ref) - 1
            if 0 <= idx < len(goals):
                goal = goals[idx]
        if goal is None:
            ref_lower = ref.lower()
            for g in goals:
                if str(g.get("title", "")).lower() == ref_lower:
                    goal = g
                    break
        if goal is None:
            return False
        goals = [g for g in goals if g.get("id") != goal.get("id")]
        _save_raw(goals)
        return True


def format_active_goals_for_prompt(*, max_goals: int = 10) -> str:
    """Render active goals as a compact system-prompt block.

    Returns "" when there are no active goals so callers can skip the
    section entirely. Each goal is one line: "- <title>: <detail>" (detail
    omitted when empty). Capped to ``max_goals`` so a long-lived goal list
    doesn't blow up prompt size; the cap keeps the most recently updated
    goals.
    """
    goals = active_goals()
    if not goals:
        return ""
    goals = sorted(goals, key=lambda g: g.get("updated") or "", reverse=True)[:max_goals]
    lines = ["## Active goals (steering input, not a task list)"]
    for g in goals:
        title = str(g.get("title") or "").strip()
        if not title:
            continue
        detail = str(g.get("detail") or "").strip()
        horizon = g.get("horizon") or DEFAULT_HORIZON
        line = f"- [{horizon}] {title}"
        if g.get("origin") == "inferred":
            line += " (inferred)"
        if detail:
            line += f" — {detail}"
        lines.append(line)
    if len(lines) == 1:
        return ""
    return "\n".join(lines)
