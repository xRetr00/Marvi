"""Rhythm model -- Marvi's sense of the user's typical daily schedule.

Learns, from the last 14 days of ActivityWatch afk-bucket data, when the
user is typically active on each weekday and where their longest typical
uninterrupted work stretches ("deep work windows") fall. The result is a
small JSON file (``~/.hermes/presence/rhythm.json``) that other presence
consumers read cheaply:

* ``gateway/flow_gate.py`` -- when *now* is outside the user's typical
  active hours for today, proactive deliveries skip flow gating entirely
  (the user isn't in deep work at 2am, by definition of their own history).
* ``tools/presence/distill.py`` -- appends a one-line rhythm summary to the
  nightly digest so the schedule enters distilled memory.

Design constraints (mirroring the rest of the presence package):

* :func:`compute_rhythm` is a **pure function** over a list of AW afk
  events -- no I/O, no clock reads beyond the events themselves -- so it is
  trivially testable with synthetic events.
* :func:`update_rhythm` is guarded: ActivityWatch down / no afk bucket /
  insufficient data never raises and never clobbers a previously written
  rhythm file.
* :func:`get_rhythm` never raises; missing/corrupt/stale (>14 days old)
  files read as ``None``.

``rhythm.json`` schema (version 1)::

    {
      "schema_version": 1,
      "generated_at": "<ISO timestamp>",
      "days_analyzed": 9,
      "weekdays": {
        "0": {                              # Monday (date.weekday() int, as str)
          "active_start": "08:45",          # median first non-afk time
          "active_end": "23:10",            # median last non-afk time
          "deep_work_windows": [            # 1-2 longest typical stretches
            ["09:00", "12:30"],
            ["14:00", "17:00"]
          ]
        },
        ...                                 # only weekdays with observed data
      }
    }
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

RHYTHM_SCHEMA_VERSION = 1

# Lookback window for both the AW fetch and the pure computation, and the
# staleness ceiling for reads: a rhythm older than this no longer reflects
# the user's current schedule and reads as "no rhythm".
LOOKBACK_DAYS = 14

# Fewer distinct calendar days with activity than this and we don't have a
# rhythm -- compute_rhythm returns None, update_rhythm writes nothing.
MIN_DAYS_WITH_ACTIVITY = 3

# Deep-work window detection: minutes-of-day are quantized into bins this
# wide; a bin is "typical" for a weekday when it was active on at least half
# of that weekday's observed days; contiguous typical bins shorter than the
# minimum are ignored as noise.
_BIN_MINUTES = 30
_MIN_DEEP_WORK_MINUTES = 60
_MAX_DEEP_WORK_WINDOWS = 2

# Grace margin around the typical active window used by
# :func:`is_outside_active_hours` -- "now" must be clearly outside (not just
# a few minutes early/late) before the flow gate stands down.
ACTIVE_HOURS_MARGIN_MINUTES = 30

_MINUTES_PER_DAY = 24 * 60


# ---------------------------------------------------------------------------
# Small time helpers
# ---------------------------------------------------------------------------


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse an AW event timestamp (ISO string or datetime) to a local-wall
    datetime. Aware timestamps are converted to the machine's local zone so
    "minutes since midnight" means the user's wall clock, not UTC."""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is not None:
        try:
            dt = dt.astimezone()
        except (OSError, OverflowError, ValueError):
            return None
    return dt


def _fmt_minutes(minutes: float) -> str:
    total = max(0, min(int(round(minutes)), _MINUTES_PER_DAY - 1))
    return f"{total // 60:02d}:{total % 60:02d}"


def _parse_hhmm(text: Any) -> Optional[int]:
    """``"08:45"`` -> 525 (minutes since midnight), or None on garbage."""
    try:
        hours_s, minutes_s = str(text).split(":", 1)
        hours, minutes = int(hours_s), int(minutes_s)
    except (ValueError, AttributeError):
        return None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        return None
    return hours * 60 + minutes


# ---------------------------------------------------------------------------
# Pure computation
# ---------------------------------------------------------------------------


def compute_rhythm(events: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Compute the per-weekday rhythm from raw AW afk-bucket events.

    ``events`` are AW event dicts shaped ``{"timestamp": ..., "duration":
    <seconds>, "data": {"status": "not-afk" | "afk"}}`` (order irrelevant).
    Only ``not-afk`` events count as activity. Events older than
    :data:`LOOKBACK_DAYS` before the newest event are ignored, so callers
    can pass a raw 14-day fetch without pre-filtering.

    Returns the ``weekdays``/``days_analyzed`` payload (see the module
    docstring schema, minus ``generated_at``), or ``None`` when fewer than
    :data:`MIN_DAYS_WITH_ACTIVITY` distinct calendar days show any activity.
    Pure: no I/O, no clock reads.
    """
    # day (date) -> list of (start_minute, end_minute) active intervals
    day_intervals: Dict[Any, List[Tuple[float, float]]] = {}
    parsed: List[Tuple[datetime, float]] = []

    for event in events or []:
        if not isinstance(event, dict):
            continue
        data = event.get("data") or {}
        if data.get("status") != "not-afk":
            continue
        ts = _parse_ts(event.get("timestamp"))
        if ts is None:
            continue
        try:
            duration = float(event.get("duration") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        if duration <= 0:
            continue
        parsed.append((ts, duration))

    if not parsed:
        return None

    newest = max(ts for ts, _ in parsed)
    cutoff = newest - timedelta(days=LOOKBACK_DAYS)

    for ts, duration in parsed:
        if ts < cutoff:
            continue
        start_min = ts.hour * 60 + ts.minute + ts.second / 60.0
        # Clamp events spanning midnight to their start day -- good enough
        # for a schedule model, and keeps every interval within one day.
        end_min = min(start_min + duration / 60.0, float(_MINUTES_PER_DAY))
        day_intervals.setdefault(ts.date(), []).append((start_min, end_min))

    if len(day_intervals) < MIN_DAYS_WITH_ACTIVITY:
        return None

    # Group day summaries by weekday.
    n_bins = _MINUTES_PER_DAY // _BIN_MINUTES
    by_weekday: Dict[int, Dict[str, Any]] = {}
    for day, intervals in day_intervals.items():
        weekday = day.weekday()
        bucket = by_weekday.setdefault(
            weekday, {"starts": [], "ends": [], "bin_days": []}
        )
        bucket["starts"].append(min(s for s, _ in intervals))
        bucket["ends"].append(max(e for _, e in intervals))
        active_bins = [False] * n_bins
        for start_min, end_min in intervals:
            first_bin = int(start_min // _BIN_MINUTES)
            # An interval touching any part of a bin marks it active.
            last_bin = min(int(max(end_min - 1e-9, start_min) // _BIN_MINUTES), n_bins - 1)
            for b in range(first_bin, last_bin + 1):
                active_bins[b] = True
        bucket["bin_days"].append(active_bins)

    weekdays_out: Dict[str, Any] = {}
    for weekday, bucket in sorted(by_weekday.items()):
        n_days = len(bucket["bin_days"])
        threshold = max(1, (n_days + 1) // 2)  # active on at least half the days
        bin_counts = [
            sum(day_bins[b] for day_bins in bucket["bin_days"]) for b in range(n_bins)
        ]

        # Contiguous runs of "typical" bins -> candidate deep-work windows.
        windows: List[Tuple[int, int]] = []  # (start_bin, end_bin_exclusive)
        run_start: Optional[int] = None
        for b in range(n_bins + 1):
            typical = b < n_bins and bin_counts[b] >= threshold
            if typical and run_start is None:
                run_start = b
            elif not typical and run_start is not None:
                windows.append((run_start, b))
                run_start = None

        min_bins = _MIN_DEEP_WORK_MINUTES // _BIN_MINUTES
        windows = [w for w in windows if (w[1] - w[0]) >= min_bins]
        # Longest 1-2 windows, reported in chronological order.
        windows = sorted(
            sorted(windows, key=lambda w: (w[1] - w[0]), reverse=True)[:_MAX_DEEP_WORK_WINDOWS]
        )

        weekdays_out[str(weekday)] = {
            "active_start": _fmt_minutes(median(bucket["starts"])),
            "active_end": _fmt_minutes(median(bucket["ends"])),
            "deep_work_windows": [
                [_fmt_minutes(s * _BIN_MINUTES), _fmt_minutes(e * _BIN_MINUTES)]
                for s, e in windows
            ],
        }

    return {"weekdays": weekdays_out, "days_analyzed": len(day_intervals)}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _rhythm_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "presence" / "rhythm.json"


def _write_rhythm_file(payload: Dict[str, Any]) -> None:
    """Atomic tempfile + replace write, mirroring the rest of the codebase."""
    path = _rhythm_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".rhythm_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        try:
            from utils import atomic_replace

            atomic_replace(tmp_path, path)
        except ImportError:  # pragma: no cover - utils is a core module
            os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def update_rhythm() -> bool:
    """Fetch the last 14 days of afk events and refresh ``rhythm.json``.

    Returns True when a new rhythm was written. Guarded end-to-end: AW
    unreachable, missing afk bucket, insufficient data, or a write error all
    return False and leave any previously written rhythm file untouched.
    """
    try:
        from tools.presence.aw_client import AWUnavailableError, aw_client

        if not aw_client.is_available():
            logger.debug("rhythm: ActivityWatch unavailable; keeping previous rhythm")
            return False
        bucket_id = aw_client.find_bucket_id("aw-watcher-afk")
        if not bucket_id:
            logger.debug("rhythm: no afk bucket found; keeping previous rhythm")
            return False
        from datetime import timezone

        start = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
        try:
            events = aw_client.get_events(bucket_id, start=start, limit=20000)
        except AWUnavailableError:
            logger.debug("rhythm: afk event fetch failed; keeping previous rhythm")
            return False

        rhythm = compute_rhythm(events)
        if rhythm is None:
            logger.debug(
                "rhythm: insufficient data (<%d days with activity); not writing",
                MIN_DAYS_WITH_ACTIVITY,
            )
            return False

        payload = {
            "schema_version": RHYTHM_SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            **rhythm,
        }
        _write_rhythm_file(payload)
        return True
    except Exception:
        logger.debug("rhythm: update failed; keeping previous rhythm", exc_info=True)
        return False


def get_rhythm() -> Optional[Dict[str, Any]]:
    """Read ``rhythm.json``. Returns None when absent, corrupt, from a
    different schema version, or stale (generated more than 14 days ago).
    Never raises."""
    try:
        path = _rhythm_path()
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        if int(data.get("schema_version") or 0) != RHYTHM_SCHEMA_VERSION:
            return None
        generated = _parse_ts(data.get("generated_at"))
        if generated is None:
            return None
        now = datetime.now(generated.tzinfo) if generated.tzinfo else datetime.now()
        if (now - generated) > timedelta(days=LOOKBACK_DAYS):
            return None
        if not isinstance(data.get("weekdays"), dict):
            return None
        return data
    except Exception:
        logger.debug("rhythm: read failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Consumers
# ---------------------------------------------------------------------------


def is_outside_active_hours(now: Optional[datetime] = None) -> bool:
    """True when a rhythm exists and ``now`` falls clearly outside the
    user's typical active hours for today's weekday.

    Used by the flow gate to stand down entirely off-hours: if the user's
    own 14-day history says they're never active at this hour, they aren't
    in deep work now, so proactive deliveries flow freely. Conservative in
    every unknown case -- no rhythm, no data for today's weekday, or
    unparseable times all return False (gate behaves exactly as before).
    """
    rhythm = get_rhythm()
    if not rhythm:
        return False
    if now is None:
        try:
            from hermes_time import now as _hermes_now

            now = _hermes_now()
        except Exception:
            now = datetime.now()
    entry = (rhythm.get("weekdays") or {}).get(str(now.weekday()))
    if not isinstance(entry, dict):
        return False
    start = _parse_hhmm(entry.get("active_start"))
    end = _parse_hhmm(entry.get("active_end"))
    if start is None or end is None or start > end:
        return False
    now_min = now.hour * 60 + now.minute
    return (
        now_min < start - ACTIVE_HOURS_MARGIN_MINUTES
        or now_min > end + ACTIVE_HOURS_MARGIN_MINUTES
    )


_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def rhythm_summary_line(now: Optional[datetime] = None) -> Optional[str]:
    """One-line summary of today's typical rhythm for the nightly digest,
    or None when no rhythm (or no data for today's weekday) is available."""
    rhythm = get_rhythm()
    if not rhythm:
        return None
    if now is None:
        try:
            from hermes_time import now as _hermes_now

            now = _hermes_now()
        except Exception:
            now = datetime.now()
    weekday = now.weekday()
    entry = (rhythm.get("weekdays") or {}).get(str(weekday))
    if not isinstance(entry, dict):
        return None
    parts = [
        f"Typical rhythm for {_WEEKDAY_NAMES[weekday]}: "
        f"active {entry.get('active_start')}-{entry.get('active_end')}"
    ]
    windows = entry.get("deep_work_windows") or []
    if windows:
        formatted = ", ".join(f"{w[0]}-{w[1]}" for w in windows if len(w) == 2)
        if formatted:
            parts.append(f"deep work {formatted}")
    return "; ".join(parts) + f" (from {rhythm.get('days_analyzed', '?')} days of history)"
