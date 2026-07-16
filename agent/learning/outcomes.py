"""Bounded local outcome ledger shared by the learning loops.

The ledger is deliberately boring: JSONL, one profile-local file, atomic
replacement, advisory locking, and best-effort reads/writes.  Learning must
never be able to break the agent's primary path.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home
from utils import atomic_replace

logger = logging.getLogger(__name__)

VALID_LOOPS = frozenset({"trust", "voice_threshold", "focus_apps", "escalation", "timing", "room_habit"})
VALID_EVENTS = frozenset({"accepted", "dismissed", "corrected", "ignored", "observed", "delivered", "engaged"})
MAX_OUTCOMES = 2_000
_lock = threading.RLock()

try:  # pragma: no cover - platform-specific import
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None
try:  # pragma: no cover - platform-specific import
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None


def _paths() -> tuple[Path, Path]:
    root = get_hermes_home().resolve() / "learning"
    return root / "outcomes.jsonl", root / ".outcomes.lock"


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    """Use a process lock plus a profile-local advisory lock when available."""
    with _lock:
        path, lock_path = _paths()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = None
        try:
            try:
                handle = open(lock_path, "a+b")
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                elif msvcrt is not None:
                    handle.seek(0)
                    if handle.read(1) == b"":
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            except OSError:
                logger.debug("learning outcome file lock unavailable", exc_info=True)
            yield
        finally:
            if handle is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    elif msvcrt is not None:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
                handle.close()


def _read_unlocked() -> List[Dict[str, Any]]:
    path, _ = _paths()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: List[Dict[str, Any]] = []
    for line in lines[-MAX_OUTCOMES:]:
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _write_unlocked(rows: List[Dict[str, Any]]) -> None:
    path, _ = _paths()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".outcomes_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for row in rows[-MAX_OUTCOMES:]:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
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


def record(loop: str, *args: str, category: Optional[str] = None, event: Optional[str] = None,
           ref: Optional[str] = None, detail: Optional[Dict[str, Any]] = None,
           at: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Append an outcome and return it. Invalid or failed writes return ``None``."""
    # Public contract is record(loop, category, event); keyword-friendly and
    # legacy record(loop, event, category=...) forms are both accepted.
    if len(args) == 1 and event is None:
        event = args[0]
    elif len(args) == 2 and event is None and category is None:
        category, event = args
    elif args:
        logger.warning("Ignoring invalid learning outcome arguments: %r", args)
        return None
    if loop not in VALID_LOOPS or event not in VALID_EVENTS:
        logger.warning("Ignoring invalid learning outcome loop=%r event=%r", loop, event)
        return None
    row: Dict[str, Any] = {
        "at": at or datetime.now(timezone.utc).isoformat(),
        "loop": loop,
        "event": event,
    }
    if category:
        row["category"] = str(category)
    if ref:
        row["ref"] = str(ref)
    if detail:
        row["detail"] = detail
    try:
        with _locked():
            rows = _read_unlocked()
            rows.append(row)
            _write_unlocked(rows)
        return row
    except Exception:  # noqa: BLE001 - learning is strictly best effort
        logger.warning("Could not record learning outcome", exc_info=True)
        return None


def recent(loop: Optional[str] = None, category: Optional[str] = None,
           since: Optional[str] = None, limit: Optional[int] = 500, *,
           days: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return matching outcomes newest first, silently skipping malformed rows."""
    if limit is not None and limit <= 0:
        return []
    try:
        with _locked():
            rows = _read_unlocked()
    except Exception:  # noqa: BLE001
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, days)) if days is not None else None
    if since:
        try:
            cutoff = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)
        except ValueError:
            return []
    matched: List[Dict[str, Any]] = []
    for row in reversed(rows):
        if loop and row.get("loop") != loop:
            continue
        if category and row.get("category") != category:
            continue
        if cutoff is not None:
            try:
                stamp = datetime.fromisoformat(str(row.get("at", "")).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                if stamp < cutoff:
                    continue
            except ValueError:
                continue
        matched.append(row)
        if limit is not None and len(matched) >= max(0, limit):
            break
    return matched


def counts(loop: Optional[str] = None, category: Optional[str] = None,
           window_days: Optional[int] = None, *, days: Optional[int] = None) -> Dict[str, int]:
    """Count event names for a filtered slice of the ledger."""
    counted = Counter(str(row.get("event")) for row in recent(loop=loop, category=category, days=days if days is not None else window_days))
    return {event: int(counted.get(event, 0)) for event in VALID_EVENTS}
