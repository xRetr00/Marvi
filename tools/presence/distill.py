"""Helper for the nightly presence-distiller cron job.

`hermes presence setup` creates a cron job (schedule =
``presence.distill_schedule``, default ``"0 3 * * *"``) that reads
ActivityWatch data collected since its last run and asks the agent to
write durable observations about the user into memory, replying
``[SILENT]`` when nothing meaningful turns up. This module supplies the
raw digest text via :func:`build_digest`; the job's cron `script=` entry
(a tiny stub written to ``~/.hermes/scripts/`` at setup time, see
``hermes_cli/presence_cmd.py``) calls :func:`print_digest_for_cron`, and
its stdout is injected into the agent's prompt each run per the cron
``script`` contract (see ``cron/jobs.py::create_job``).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK_HOURS = 24
_TOP_N = 8
_MEDIA_HIGHLIGHT_LIMIT = 10

# System-prompt note for the distiller job (used by hermes_cli/presence_cmd.py
# when creating the cron job).
DISTILL_SYSTEM_NOTE = (
    "You are Marvi's presence distiller. You receive a digest of the "
    "user's local desktop activity (apps, coding workspaces, media) since "
    "your last run, sourced from ActivityWatch running on their own "
    "machine. Extract only DURABLE observations worth remembering "
    "long-term about the user -- recurring projects, tools, working "
    "habits, interests -- never moment-to-moment noise. Use the memory "
    "tool (target='user') to record anything genuinely new and durable. "
    "If there is nothing meaningful and new to record, reply with exactly "
    "[SILENT] and nothing else."
)


def _state_path() -> Path:
    from hermes_cli.config import get_hermes_home

    return get_hermes_home() / "presence" / "distill_state.json"


def get_last_run_iso() -> Optional[str]:
    """Return the ISO timestamp of the last distiller run, or None."""
    path = _state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        value = data.get("last_run_iso")
        return str(value) if value else None
    except Exception:
        return None


def mark_run(when: Optional[datetime] = None) -> None:
    """Persist ``when`` (default: now) as the last distiller run time."""
    path = _state_path()
    when = when or datetime.now(timezone.utc)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"last_run_iso": when.astimezone(timezone.utc).isoformat()}),
            encoding="utf-8",
        )
    except OSError:
        logger.debug("distill: failed to persist last-run state", exc_info=True)


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def build_digest(*, since_iso: Optional[str] = None) -> str:
    """Build a compact, denylist-filtered text digest of presence data
    since ``since_iso`` (defaults to the persisted last-run time, or
    :data:`DEFAULT_LOOKBACK_HOURS` ago on a first run).

    Returns ``""`` when ActivityWatch is unavailable or there's genuinely
    nothing worth reporting -- callers should treat empty output as "no
    new context", not an error.
    """
    from tools.presence.aw_client import AWUnavailableError, aw_client
    from tools.presence.common import (
        filter_denylisted_events,
        get_denylist,
        is_vscode_app,
        matches_denylist,
    )
    from tools.presence.title_parsing import parse_window

    if not aw_client.is_available():
        return ""

    since = since_iso or get_last_run_iso()
    if not since:
        since = (
            datetime.now(timezone.utc) - timedelta(hours=DEFAULT_LOOKBACK_HOURS)
        ).isoformat()

    denylist = get_denylist()
    lines: List[str] = []

    window_bucket = aw_client.find_bucket_id("aw-watcher-window")
    if window_bucket:
        try:
            events = aw_client.get_events(window_bucket, start=since, limit=5000)
        except AWUnavailableError:
            events = []
        events = filter_denylisted_events(events, denylist)

        app_totals: Dict[str, float] = {}
        workspace_totals: Dict[str, float] = {}
        for event in events:
            data = event.get("data") or {}
            app = data.get("app") or "unknown"
            try:
                dur = float(event.get("duration") or 0.0)
            except (TypeError, ValueError):
                dur = 0.0
            app_totals[app] = app_totals.get(app, 0.0) + dur
            if is_vscode_app(app, data.get("title")):
                workspace = parse_window(app, data.get("title")).get("workspace")
                if workspace:
                    workspace_totals[workspace] = workspace_totals.get(workspace, 0.0) + dur

        top_apps = [
            (app, secs) for app, secs in
            sorted(app_totals.items(), key=lambda kv: kv[1], reverse=True)[:_TOP_N]
            if secs >= 60
        ]
        if top_apps:
            lines.append("App usage since last check:")
            lines.extend(f"  - {app}: {_format_duration(secs)}" for app, secs in top_apps)

        if workspace_totals:
            lines.append("Coding time by workspace:")
            for ws, secs in sorted(workspace_totals.items(), key=lambda kv: kv[1], reverse=True)[:_TOP_N]:
                lines.append(f"  - {ws}: {_format_duration(secs)}")

    media_bucket = aw_client.find_bucket_id("aw-watcher-media")
    if media_bucket:
        try:
            media_events = aw_client.get_events(media_bucket, start=since, limit=200)
        except AWUnavailableError:
            media_events = []
        seen: set = set()
        highlights: List[str] = []
        for event in media_events:
            data = event.get("data") or {}
            title, artist = data.get("title"), data.get("artist")
            if not title:
                continue
            if denylist and matches_denylist(f"{artist or ''} {title}", denylist):
                continue
            key = (title, artist)
            if key in seen:
                continue
            seen.add(key)
            highlights.append(title + (f" by {artist}" if artist else ""))
            if len(highlights) >= _MEDIA_HIGHLIGHT_LIMIT:
                break
        if highlights:
            lines.append("Media played:")
            lines.extend(f"  - {h}" for h in highlights)

    if not lines:
        return ""

    # One-line rhythm summary (when a rhythm model exists) so the user's
    # typical schedule enters distilled memory alongside the activity digest.
    # Guarded: a rhythm read failure must never break digest building.
    try:
        from tools.presence.rhythm import rhythm_summary_line

        rhythm_line = rhythm_summary_line()
        if rhythm_line:
            lines.append(rhythm_line)
    except Exception:
        logger.debug("distill: rhythm summary failed", exc_info=True)

    return f"Presence digest since {since}:\n" + "\n".join(lines)


def print_digest_for_cron() -> None:
    """Entry point for the cron job's `script=` data-collection step.

    Prints the digest to stdout (which `cron/scheduler.py` injects into
    the agent's prompt for this run) and records this run's timestamp.
    Prints nothing when there's no new context -- an empty digest still
    lets the agent's [SILENT] instinct (from DISTILL_SYSTEM_NOTE / the
    cron system prompt's built-in SILENT instruction) suppress delivery.
    """
    # Refresh the rhythm model first so tonight's digest (and tomorrow's
    # flow gating) see today's data. Guarded: a rhythm failure must never
    # break distillation.
    try:
        from tools.presence.rhythm import update_rhythm

        update_rhythm()
    except Exception:
        logger.debug("distill: rhythm update failed", exc_info=True)

    try:
        digest = build_digest()
    except Exception as exc:  # never let the cron script itself crash the job
        logger.exception("distill: build_digest failed: %s", exc)
        digest = ""
    if digest:
        print(digest)
    mark_run()


if __name__ == "__main__":
    print_digest_for_cron()
