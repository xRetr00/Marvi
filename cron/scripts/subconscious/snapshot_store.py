"""Per-surface snapshot storage for Marvi's Composio smart sync.

Each configured surface (``gmail``, ``github``, ...) gets one JSON file under
``~/.hermes/subconscious/snapshots/<surface>.json`` holding:

* ``cursor``  -- the opaque delta-fetch cursor for that surface (Gmail
  ``historyId``, GitHub notifications ``since`` timestamp/ETag, etc). This is
  the thing that makes sync *smart*: every fetch asks the provider "what
  changed since `cursor`" instead of pulling the whole inbox/notification
  list every tick.
* ``state``   -- the last summarized state (e.g. the set of message/
  notification ids already surfaced), used so a diff never repeats an item
  the user has already been told about.
* throttle/backoff bookkeeping -- ``last_fetch_at``, ``consecutive_failures``,
  ``next_retry_at``, ``quiet_streak`` -- so a surface that starts erroring
  backs off exponentially instead of hammering the API (or the local disk)
  every tick, and so a surface simply never gets fetched more often than its
  configured minimum interval even when the cron tick runs more frequently.
  On top of that floor, ``quiet_streak`` counts consecutive no-change fetches
  and scales the effective interval up (doubling per quiet tick, capped by
  ``composio.quiet_backoff_max``) so a surface that's been quiet for a while
  gets checked less often -- any detected change resets it to 0 and snaps
  the cadence back to the base interval immediately.

Storage style mirrors ``cron/jobs.py``: atomic tempfile + ``os.replace`` via
``utils.atomic_replace``, owner-only permissions (0600 file / 0700 dir,
no-op on Windows).

Nothing here imports the Composio SDK -- that stays lazy, in
``composio_client.py``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now
from utils import atomic_replace

logger = logging.getLogger(__name__)

# Bump if the on-disk shape changes in an incompatible way. Old snapshots
# with a lower/missing version are treated as absent (fresh start) rather
# than crashing the tick -- a corrupt/stale snapshot must never take the
# subconscious tick down (see error-handling section of the design spec).
SNAPSHOT_SCHEMA_VERSION = 1

# Floor between successive fetches of the SAME surface, regardless of how
# often the cron tick itself runs. This is the anti-goal guardrail from the
# design spec ("never OpenHuman-style blind polling that burns API limits")
# expressed as a hard minimum even if the tick interval config is set low.
DEFAULT_MIN_INTERVAL_SECONDS = 180  # 3 minutes

# Exponential backoff parameters for a surface that starts failing (auth
# error, 429, 5xx, network blip). Base doubles per consecutive failure,
# capped so a long-broken surface still gets retried roughly hourly instead
# of being abandoned forever.
BACKOFF_BASE_SECONDS = 60
BACKOFF_MAX_SECONDS = 3600
# After this many consecutive failures we stop bumping the exponent (it's
# already at the cap) but keep the surface alive -- never permanently give up.
BACKOFF_MAX_FAILURES_FOR_DISPLAY = 6

# Quiet-streak cadence scaling: every consecutive no-change fetch doubles a
# surface's effective min-fetch-interval (see ``SurfaceStore.
# effective_min_interval_seconds``), up to this multiplier. A surface that's
# been quiet for a while (e.g. a Gmail with no new mail in hours) gets
# checked less and less often for zero extra API spend -- and any detected
# change resets the streak immediately, snapping back to the base cadence
# on the very next tick. Configurable via ``composio.quiet_backoff_max``;
# a value of 1 disables scaling entirely (multiplier pinned at 1x).
DEFAULT_QUIET_BACKOFF_MAX = 8

_SURFACE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class InvalidSurfaceName(ValueError):
    """Raised when a surface name isn't a safe filesystem path component."""


def _validate_surface_name(surface: str) -> str:
    """Return a normalized surface name, rejecting anything path-unsafe.

    Surface names become filenames under the snapshot directory; a
    crafted/typo'd value like ``"../../etc/passwd"`` must never reach
    ``open()``. Mirrors the defensive intent of ``cron/jobs.py``'s
    ``_job_output_dir`` path-escape guard.
    """
    text = str(surface or "").strip().lower()
    if not _SURFACE_NAME_RE.match(text):
        raise InvalidSurfaceName(
            f"Invalid Composio surface name: {surface!r}. "
            "Surface names must be lowercase letters/digits/underscore/hyphen "
            "(e.g. 'gmail', 'github')."
        )
    return text


def _secure_dir(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except (OSError, NotImplementedError):
        pass  # Windows or unsupported platform


def _secure_file(path: Path) -> None:
    try:
        if path.exists():
            os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def snapshots_dir() -> Path:
    """Return ``~/.hermes/subconscious/snapshots`` (created if missing)."""
    d = get_hermes_home() / "subconscious" / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    # Also secure the parent 'subconscious' dir -- cheap, idempotent.
    _secure_dir(d.parent)
    _secure_dir(d)
    return d


def _snapshot_path(surface: str) -> Path:
    name = _validate_surface_name(surface)
    return snapshots_dir() / f"{name}.json"


@dataclass
class SurfaceSnapshot:
    """In-memory representation of one surface's snapshot file."""

    surface: str
    cursor: Dict[str, Any] = field(default_factory=dict)
    state: Dict[str, Any] = field(default_factory=dict)
    last_fetch_at: Optional[str] = None
    last_success_at: Optional[str] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    next_retry_at: Optional[str] = None
    # Consecutive successful fetches in a row that found nothing new. Drives
    # the quiet-streak cadence multiplier in ``SurfaceStore``; reset to 0 the
    # instant a fetch reports a change. Independent of ``consecutive_failures``
    # -- a failure never touches this counter, and vice versa.
    quiet_streak: int = 0
    schema_version: int = SNAPSHOT_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "surface": self.surface,
            "cursor": self.cursor,
            "state": self.state,
            "last_fetch_at": self.last_fetch_at,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "next_retry_at": self.next_retry_at,
            "quiet_streak": self.quiet_streak,
        }

    @classmethod
    def fresh(cls, surface: str) -> "SurfaceSnapshot":
        return cls(surface=_validate_surface_name(surface))

    @classmethod
    def from_dict(cls, surface: str, data: Dict[str, Any]) -> "SurfaceSnapshot":
        surface = _validate_surface_name(surface)
        if not isinstance(data, dict):
            return cls.fresh(surface)
        cursor = data.get("cursor")
        state = data.get("state")
        return cls(
            surface=surface,
            cursor=cursor if isinstance(cursor, dict) else {},
            state=state if isinstance(state, dict) else {},
            last_fetch_at=data.get("last_fetch_at"),
            last_success_at=data.get("last_success_at"),
            last_error=data.get("last_error"),
            consecutive_failures=int(data.get("consecutive_failures") or 0),
            next_retry_at=data.get("next_retry_at"),
            quiet_streak=max(0, int(data.get("quiet_streak") or 0)),
            schema_version=int(data.get("schema_version") or SNAPSHOT_SCHEMA_VERSION),
        )


def load_snapshot(surface: str) -> SurfaceSnapshot:
    """Load a surface's snapshot, or return a fresh empty one.

    Never raises on a missing or corrupt file -- a broken/partial snapshot
    must not crash the tick (design spec error-handling section). Corruption
    is logged and treated as "first run" for that surface, which is safe:
    the fetcher will re-establish its cursor and report nothing changed
    rather than dumping the whole inbox.
    """
    surface = _validate_surface_name(surface)
    path = _snapshot_path(surface)
    if not path.exists():
        return SurfaceSnapshot.fresh(surface)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(
            "subconscious snapshot for surface %r is unreadable/corrupt (%s); "
            "starting fresh", surface, e,
        )
        return SurfaceSnapshot.fresh(surface)
    return SurfaceSnapshot.from_dict(surface, data)


def save_snapshot(snapshot: SurfaceSnapshot) -> None:
    """Atomically persist a snapshot. Owner-only permissions on POSIX."""
    path = _snapshot_path(snapshot.surface)
    d = path.parent
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(d), suffix=".tmp", prefix=".snap_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(snapshot.to_dict(), f, indent=2, ensure_ascii=False)
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


def _parse_iso(value: Optional[str]):
    if not value:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _now_iso() -> str:
    return _hermes_now().isoformat()


def _seconds_since(iso_value: Optional[str]) -> Optional[float]:
    dt = _parse_iso(iso_value)
    if dt is None:
        return None
    now = _hermes_now()
    try:
        return max(0.0, (now - dt).total_seconds())
    except TypeError:
        # Naive/aware mismatch -- treat as "unknown", never crash the tick.
        return None


class SurfaceStore:
    """The object handed to each fetcher's ``fetch_delta(store)``.

    Wraps a :class:`SurfaceSnapshot` with the throttle/backoff bookkeeping
    and persistence so fetcher modules never touch the JSON file directly.
    """

    def __init__(
        self,
        surface: str,
        *,
        min_interval_seconds: int = DEFAULT_MIN_INTERVAL_SECONDS,
        quiet_backoff_max: int = DEFAULT_QUIET_BACKOFF_MAX,
    ) -> None:
        self.surface = _validate_surface_name(surface)
        self.min_interval_seconds = min_interval_seconds
        # >=1 always -- a cap below 1x would make the effective interval
        # *shrink* below the base, which isn't a thing; 1 is the "disabled"
        # value (multiplier pinned at 1x, no scaling).
        self.quiet_backoff_max = max(1, int(quiet_backoff_max or 1))
        self._snapshot = load_snapshot(self.surface)
        self._dirty = False

    # ── cursor / state access ───────────────────────────────────────────

    @property
    def cursor(self) -> Dict[str, Any]:
        """The fetcher's opaque delta cursor (history id, since-timestamp, etag...)."""
        return self._snapshot.cursor

    @property
    def state(self) -> Dict[str, Any]:
        """The last summarized state (e.g. ids already surfaced)."""
        return self._snapshot.state

    def is_first_run(self) -> bool:
        """True when this surface has never established a cursor.

        Fetchers use this to establish a baseline cursor WITHOUT reporting a
        diff -- the "never dump the whole inbox" rule from Contract 1 /
        Workstream C task 3.
        """
        return not self._snapshot.cursor

    def set_cursor(self, cursor: Dict[str, Any]) -> None:
        self._snapshot.cursor = dict(cursor or {})
        self._dirty = True

    def update_cursor(self, **kwargs: Any) -> None:
        self._snapshot.cursor.update(kwargs)
        self._dirty = True

    def set_state(self, state: Dict[str, Any]) -> None:
        self._snapshot.state = dict(state or {})
        self._dirty = True

    def update_state(self, **kwargs: Any) -> None:
        self._snapshot.state.update(kwargs)
        self._dirty = True

    # ── throttle / backoff ──────────────────────────────────────────────

    def seconds_since_last_fetch(self) -> Optional[float]:
        return _seconds_since(self._snapshot.last_fetch_at)

    @property
    def quiet_streak(self) -> int:
        """Consecutive successful fetches in a row that found nothing new."""
        return self._snapshot.quiet_streak

    def effective_min_interval_seconds(self) -> int:
        """Base min-interval scaled up by the quiet-streak multiplier.

        Every consecutive no-change fetch doubles the effective interval
        (``base * 2**quiet_streak``), capped at ``quiet_backoff_max`` times
        the base. A surface that's been quiet for a while (e.g. a Gmail with
        no new mail in hours) gets polled less and less often for zero extra
        API spend. ``record_success(changed=True)`` resets the streak to 0,
        so any detected change snaps the cadence back to the base interval
        on the very next tick.
        """
        multiplier = min(2 ** self._snapshot.quiet_streak, self.quiet_backoff_max)
        return int(self.min_interval_seconds * multiplier)

    def is_throttled(self) -> bool:
        """True if this surface was fetched too recently to fetch again."""
        elapsed = self.seconds_since_last_fetch()
        if elapsed is None:
            return False
        return elapsed < self.effective_min_interval_seconds()

    def is_backoff_active(self) -> bool:
        """True if a previous failure put this surface into a cooldown window."""
        retry_at = self._snapshot.next_retry_at
        if not retry_at:
            return False
        dt = _parse_iso(retry_at)
        if dt is None:
            return False
        try:
            return _hermes_now() < dt
        except TypeError:
            return False

    def should_skip(self) -> bool:
        """Combined throttle + backoff check fetchers/the entry script use
        to decide whether to even attempt a fetch this tick."""
        return self.is_throttled() or self.is_backoff_active()

    def skip_reason(self) -> Optional[str]:
        if self.is_backoff_active():
            return f"backing off until {self._snapshot.next_retry_at}"
        if self.is_throttled():
            elapsed = self.seconds_since_last_fetch() or 0.0
            effective = self.effective_min_interval_seconds()
            suffix = (
                f" (quiet streak {self._snapshot.quiet_streak}, base {self.min_interval_seconds}s)"
                if effective != self.min_interval_seconds
                else ""
            )
            return (
                f"throttled ({elapsed:.0f}s since last fetch, "
                f"min interval {effective}s{suffix})"
            )
        return None

    def mark_attempt(self) -> None:
        """Call at the start of a fetch attempt (drives throttle bookkeeping)."""
        self._snapshot.last_fetch_at = _now_iso()
        self._dirty = True

    def record_success(self, changed: bool = True) -> None:
        """Clear failure/backoff state after a successful fetch, and update
        the quiet-streak counter that drives cadence scaling.

        ``changed`` should be True whenever the fetch surfaced a diff (or the
        caller can't tell) and False when it positively confirmed nothing
        new happened. Defaulting to True is the conservative choice: a
        caller that doesn't pass it never silently drifts into a slower
        cadence it didn't ask for. A quiet (``changed=False``) fetch bumps
        the streak by one; any changed fetch resets it to 0 immediately, so
        the very next tick is back to the base interval.
        """
        self._snapshot.consecutive_failures = 0
        self._snapshot.next_retry_at = None
        self._snapshot.last_error = None
        self._snapshot.last_success_at = _now_iso()
        if changed:
            self._snapshot.quiet_streak = 0
        else:
            self._snapshot.quiet_streak += 1
        self._dirty = True

    def record_failure(self, error: str) -> None:
        """Record a fetch failure and compute the next exponential-backoff
        retry time. Never raises -- a failing surface must be skippable,
        not fatal, to the caller (design spec error-handling section)."""
        self._snapshot.consecutive_failures += 1
        self._snapshot.last_error = str(error)[:2000]
        exponent = min(
            self._snapshot.consecutive_failures - 1,
            BACKOFF_MAX_FAILURES_FOR_DISPLAY,
        )
        delay = min(BACKOFF_BASE_SECONDS * (2 ** exponent), BACKOFF_MAX_SECONDS)
        from datetime import timedelta

        self._snapshot.next_retry_at = (_hermes_now() + timedelta(seconds=delay)).isoformat()
        self._dirty = True

    # ── persistence ──────────────────────────────────────────────────────

    def save(self) -> None:
        """Persist the snapshot if anything changed since load."""
        if self._dirty:
            save_snapshot(self._snapshot)
            self._dirty = False

    def status_dict(self) -> Dict[str, Any]:
        """Read-only summary for CLI status display (`hermes composio list`)."""
        return {
            "surface": self.surface,
            "has_cursor": bool(self._snapshot.cursor),
            "last_fetch_at": self._snapshot.last_fetch_at,
            "last_success_at": self._snapshot.last_success_at,
            "last_error": self._snapshot.last_error,
            "consecutive_failures": self._snapshot.consecutive_failures,
            "next_retry_at": self._snapshot.next_retry_at,
            "seconds_since_last_fetch": self.seconds_since_last_fetch(),
            "quiet_streak": self._snapshot.quiet_streak,
            "effective_min_interval_seconds": self.effective_min_interval_seconds(),
        }


def open_store(
    surface: str,
    *,
    min_interval_seconds: int = DEFAULT_MIN_INTERVAL_SECONDS,
    quiet_backoff_max: int = DEFAULT_QUIET_BACKOFF_MAX,
) -> SurfaceStore:
    """Convenience constructor -- what most callers should use."""
    return SurfaceStore(
        surface,
        min_interval_seconds=min_interval_seconds,
        quiet_backoff_max=quiet_backoff_max,
    )
