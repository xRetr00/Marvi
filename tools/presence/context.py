"""``desktop_context`` tool implementation.

Modes:
  now    -- foreground app, window title, parsed VS Code/terminal context,
            now-playing media, afk state, current-app session length.
  today  -- aggregates since local midnight: top apps by time, coding time
            by workspace, media highlights.
  week   -- same aggregates over the trailing 7 days.

Output is a compact dict (JSON-serialized by the tool shim) sized for an
LLM prompt, not a raw AW event dump. Every entry point degrades to
``{"available": False, "error": ...}`` when ActivityWatch is unreachable --
never raises into the tool-dispatch layer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from tools.presence.aw_client import (
    AWClient,
    AWUnavailableError,
    UNAVAILABLE_MESSAGE,
    aw_client,
)
from tools.presence.common import (
    filter_denylisted_events,
    get_denylist,
    get_presence_config,
    is_vscode_app,
    matches_denylist,
    redact_if_denylisted,
)
from tools.presence.title_parsing import parse_window

logger = logging.getLogger(__name__)

VALID_MODES = ("now", "today", "week")

# How many consecutive same-window events to scan back when computing
# current-app session length. Bounded so a very long unbroken session
# doesn't turn into an unbounded AW query.
_SESSION_LENGTH_SCAN_LIMIT = 500
_MEDIA_HIGHLIGHT_LIMIT = 10
_TOP_N = 10


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _duration_seconds(event: Dict[str, Any]) -> float:
    try:
        return float(event.get("duration") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _current_session_length_seconds(client: AWClient, app: Optional[str],
                                     title: Optional[str]) -> Optional[float]:
    """Sum durations of the consecutive most-recent window events that
    share the current (app, title) -- "how long on this exact window"."""
    bucket_id = client.find_bucket_id("aw-watcher-window")
    if not bucket_id:
        return None
    try:
        events = client.get_events(bucket_id, limit=_SESSION_LENGTH_SCAN_LIMIT)
    except AWUnavailableError:
        return None
    total = 0.0
    matched_any = False
    for event in events:
        data = event.get("data") or {}
        if data.get("app") == app and data.get("title") == title:
            total += _duration_seconds(event)
            matched_any = True
        else:
            break
    return total if matched_any else None


def _now_context(client: AWClient) -> Dict[str, Any]:
    denylist = get_denylist()
    result: Dict[str, Any] = {"afk": client.get_afk_state() or "unknown"}

    window_event = client.get_current_window()
    if window_event:
        data = window_event.get("data") or {}
        app, title = data.get("app"), data.get("title")
        reason = redact_if_denylisted(app, title, denylist)
        if reason:
            result["window"] = {"app": app, "redacted": True, "reason": reason}
        else:
            result["window"] = parse_window(app, title)
            session_length = _current_session_length_seconds(client, app, title)
            if session_length is not None:
                result["session_length_seconds"] = round(session_length)

    media_event = client.get_current_media()
    if media_event:
        data = media_event.get("data") or {}
        title, artist = data.get("title"), data.get("artist")
        reason = redact_if_denylisted(data.get("app"), f"{title or ''} {artist or ''}", denylist)
        if reason:
            result["now_playing"] = {"redacted": True, "reason": reason}
        elif title:
            result["now_playing"] = {
                "title": title,
                "artist": artist,
                "status": data.get("status"),
            }

    return result


def _range_start(mode: str) -> datetime:
    now = datetime.now(timezone.utc).astimezone()
    if mode == "today":
        start_local = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start_local.astimezone(timezone.utc)
    return (now - timedelta(days=7)).astimezone(timezone.utc)


def _aggregate(client: AWClient, mode: str) -> Dict[str, Any]:
    denylist = get_denylist()
    start_iso = _iso(_range_start(mode))

    app_totals: Dict[str, float] = {}
    workspace_totals: Dict[str, float] = {}
    window_bucket = client.find_bucket_id("aw-watcher-window")
    if window_bucket:
        try:
            events = client.get_events(window_bucket, start=start_iso, limit=5000)
        except AWUnavailableError:
            events = []
        for event in filter_denylisted_events(events, denylist):
            data = event.get("data") or {}
            app = data.get("app") or "unknown"
            dur = _duration_seconds(event)
            app_totals[app] = app_totals.get(app, 0.0) + dur
            if is_vscode_app(app, data.get("title")):
                workspace = parse_window(app, data.get("title")).get("workspace")
                if workspace:
                    workspace_totals[workspace] = workspace_totals.get(workspace, 0.0) + dur

    media_highlights: List[Dict[str, Any]] = []
    media_bucket = client.find_bucket_id("aw-watcher-media")
    if media_bucket:
        try:
            media_events = client.get_events(media_bucket, start=start_iso, limit=500)
        except AWUnavailableError:
            media_events = []
        seen: set = set()
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
            media_highlights.append({"title": title, "artist": artist})
            if len(media_highlights) >= _MEDIA_HIGHLIGHT_LIMIT:
                break

    top_apps = sorted(app_totals.items(), key=lambda kv: kv[1], reverse=True)[:_TOP_N]
    top_workspaces = sorted(workspace_totals.items(), key=lambda kv: kv[1], reverse=True)[:_TOP_N]

    return {
        "range": mode,
        "since": start_iso,
        "top_apps": [{"app": app, "seconds": round(secs)} for app, secs in top_apps],
        "coding_time_by_workspace": [
            {"workspace": ws, "seconds": round(secs)} for ws, secs in top_workspaces
        ],
        "media_highlights": media_highlights,
    }


def desktop_context(mode: str = "now") -> Dict[str, Any]:
    """Return the desktop_context payload for ``mode`` ("now"/"today"/"week")."""
    mode = (mode or "now").strip().lower()
    if mode not in VALID_MODES:
        return {"available": False, "error": f"invalid mode {mode!r}; expected one of {VALID_MODES}"}
    if not get_presence_config().get("enabled"):
        return {"available": False, "error": "desktop presence is paused"}

    client = aw_client
    if not client.is_available():
        return {"available": False, "error": UNAVAILABLE_MESSAGE}

    try:
        data = _now_context(client) if mode == "now" else _aggregate(client, mode)
    except AWUnavailableError as exc:
        return {"available": False, "error": str(exc)}
    data["available"] = True
    data.setdefault("mode", mode)
    return data


def check_desktop_context_requirements() -> bool:
    """Always expose the tool -- unavailability is reported at call time
    with a clear message rather than hiding the tool entirely (spec: never
    crash, always degrade with a clear message)."""
    return True


def handle_desktop_context(args: Dict[str, Any], **kwargs) -> str:
    from tools.registry import tool_result

    mode = (args or {}).get("mode", "now")
    try:
        data = desktop_context(mode)
    except Exception as exc:  # defensive -- tool handlers must never raise
        logger.exception("desktop_context failed: %s", exc)
        return tool_result({"available": False, "error": f"desktop_context failed: {exc}"})
    return tool_result(data)
