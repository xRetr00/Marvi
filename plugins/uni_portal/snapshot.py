"""Snapshot storage + diff logic for the uni_portal daily check — spec §1.3.

Kept deliberately pure/dependency-free (no browser, no network, no
credential access) so it is fully unit-testable with fakes: :func:`diff_snapshots`
takes two plain dicts and returns what's new, nothing more. The actual
browser-driven collection (``plugins/uni_portal/portal.py``) builds a
snapshot dict in this same shape and hands it to :func:`diff_snapshots`
against the persisted one, then :func:`save_snapshot` on completion.

Snapshot shape (all keys optional, default to empty)::

    {
        "grades": [{"course": str, "grade": str}, ...],
        "announcements": [{"id": str, "title": str, "date": str}, ...],
        "schedule": [{"day": str, "time": str, "course": str}, ...],
        "captured_at": iso8601 str,
    }

No transcripts of the portal's raw pages are stored beyond this diffed
snapshot (spec §1.3 security note).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home
from utils import atomic_replace

logger = logging.getLogger(__name__)


def snapshot_path() -> Path:
    return get_hermes_home() / "uni_portal" / "snapshot.json"


def _empty_snapshot() -> Dict[str, Any]:
    return {"grades": [], "announcements": [], "schedule": [], "captured_at": None}


def load_snapshot() -> Dict[str, Any]:
    """Load the persisted snapshot, or an empty one if none exists yet /
    it's unreadable. Never raises."""
    path = snapshot_path()
    if not path.exists():
        return _empty_snapshot()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_snapshot()
    if not isinstance(data, dict):
        return _empty_snapshot()
    empty = _empty_snapshot()
    for key, default in empty.items():
        data.setdefault(key, default)
    return data


def save_snapshot(snapshot: Dict[str, Any]) -> None:
    """Atomically persist ``snapshot``. Mirrors the tempfile+replace+chmod
    pattern used throughout ``HERMES_HOME`` stores (e.g.
    ``cron/subconscious_initiatives.py``)."""
    path = snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".snapshot_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, indent=2, ensure_ascii=False)
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


def _grade_key(row: Dict[str, Any]) -> str:
    return str(row.get("course") or "").strip().casefold()


def _announcement_key(row: Dict[str, Any]) -> str:
    # Prefer a stable id if the portal exposes one; fall back to title+date,
    # which is stable enough for dedup purposes even without a real id.
    if row.get("id"):
        return f"id:{row['id']}"
    return f"td:{str(row.get('title') or '').strip().casefold()}|{str(row.get('date') or '').strip()}"


def diff_snapshots(old: Optional[Dict[str, Any]], new: Optional[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Pure diff between two snapshot dicts (see module docstring for shape).

    Returns::

        {
            "new_grades": [...],       # course present in new, absent in old
            "changed_grades": [...],   # course present in both, grade differs
            "new_announcements": [...],# announcement present in new, absent in old
        }

    Malformed/missing input degrades to empty lists rather than raising —
    a diff failure must never block the daily check from at least saving
    the new snapshot.
    """
    old = old if isinstance(old, dict) else {}
    new = new if isinstance(new, dict) else {}

    old_grades = {
        _grade_key(row): row for row in (old.get("grades") or []) if isinstance(row, dict) and row.get("course")
    }
    new_grades = {
        _grade_key(row): row for row in (new.get("grades") or []) if isinstance(row, dict) and row.get("course")
    }
    new_grade_rows: List[Dict[str, Any]] = []
    changed_grade_rows: List[Dict[str, Any]] = []
    for key, row in new_grades.items():
        if key not in old_grades:
            new_grade_rows.append(row)
        elif str(old_grades[key].get("grade") or "") != str(row.get("grade") or ""):
            changed_grade_rows.append(
                {"course": row.get("course"), "old_grade": old_grades[key].get("grade"), "new_grade": row.get("grade")}
            )

    old_announcements = {
        _announcement_key(row) for row in (old.get("announcements") or []) if isinstance(row, dict)
    }
    new_announcement_rows: List[Dict[str, Any]] = [
        row
        for row in (new.get("announcements") or [])
        if isinstance(row, dict) and _announcement_key(row) not in old_announcements
    ]

    return {
        "new_grades": new_grade_rows,
        "changed_grades": changed_grade_rows,
        "new_announcements": new_announcement_rows,
    }


def has_changes(diff: Dict[str, List[Dict[str, Any]]]) -> bool:
    return bool(diff.get("new_grades") or diff.get("changed_grades") or diff.get("new_announcements"))


def format_diff_summary(diff: Dict[str, List[Dict[str, Any]]]) -> str:
    """Human-readable one-shot summary for the proactive message + episodic
    record. Never raises."""
    try:
        parts: List[str] = []
        for row in diff.get("new_grades") or []:
            parts.append(f"New grade: {row.get('course')} — {row.get('grade')}")
        for row in diff.get("changed_grades") or []:
            parts.append(f"Grade updated: {row.get('course')} — {row.get('old_grade')} -> {row.get('new_grade')}")
        for row in diff.get("new_announcements") or []:
            title = row.get("title") or "(untitled)"
            date = row.get("date")
            parts.append(f"New announcement: {title}" + (f" ({date})" if date else ""))
        return "\n".join(parts)
    except Exception:
        logger.debug("uni_portal snapshot: format_diff_summary failed", exc_info=True)
        return ""
