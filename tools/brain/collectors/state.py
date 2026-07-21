"""Tiny on-disk cursor/state store for Brain collectors (email, github, ...).

One small JSON file per collector under
``HERMES_HOME/brain/collectors/<name>.json``, holding whatever cursor shape
that collector needs (a delta timestamp, a ``{repo+path: sha}`` map, a list
of already-seen message ids, ...). Atomic tempfile + ``os.replace``, mirroring
``tools/brain/indexer.py``'s ``last_run.json`` bookkeeping style.

Deliberately separate from
``cron/scripts/subconscious/snapshot_store.py``'s ``SurfaceStore``: that
module is a read-only import boundary for this workstream (owned by the
subconscious's own delta-notification fetchers), and its throttle/backoff
bookkeeping is specific to per-tick polling cadence -- Brain collectors run
once per "Brain indexer" cron pass (every ``brain.schedule``, default 6h),
not on their own independently-throttled schedule.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict

from hermes_constants import get_hermes_home

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _validate_name(name: str) -> str:
    text = str(name or "").strip().lower()
    if not _NAME_RE.match(text):
        raise ValueError(f"Invalid Brain collector name: {name!r}")
    return text


def _state_dir() -> Path:
    d = get_hermes_home() / "brain" / "collectors"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path(name: str) -> Path:
    return _state_dir() / f"{_validate_name(name)}.json"


def load_collector_state(name: str) -> Dict[str, Any]:
    """Return a collector's persisted state, or ``{}`` if absent/corrupt --
    corruption is treated as "first run", never raised."""
    path = _state_path(name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_collector_state(name: str, state: Dict[str, Any]) -> None:
    path = _state_path(name)
    d = path.parent
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=f".{_validate_name(name)}_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
