""""Goblin mode" -- opt-in proactive presence features (Contract 3:
``presence.goblin.*``, default OFF).

Two independent pieces:

  - **Shoulder taps**: :func:`check_stuck` is a pure heuristic over a
    window-event history that flags "the user has probably been stuck for
    a while". :func:`check_stuck_and_notify` wraps it with the AW query,
    the config gate, and a debounced (>= 2h) proactive nudge delivered via
    the existing cron delivery path.
  - **Session priming**: :func:`session_priming_summary` renders a
    one-paragraph plain-English summary of the last hour of presence, for
    injection at the start of a new conversation session (zero-cold-start).

Both are safe no-ops when ActivityWatch is unavailable or the relevant
``presence.goblin.*`` flag is off.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# --- Shoulder-tap heuristic -------------------------------------------------

# "Stuck" requires the SAME window to have been in the foreground for at
# least this long (design spec: "same window title >45 min").
STUCK_MIN_DURATION_SECONDS = 45 * 60

# How many window events (walking back from the moment the stuck window
# was first entered) to scan for corroborating signals.
_LOOKBACK_EVENT_COUNT = 30

# How many window events to fetch from AW for one check_stuck_and_notify pass.
STUCK_SCAN_LIMIT = 200

# Debounce: at most one shoulder-tap notification every 2 hours.
DEBOUNCE_SECONDS = 2 * 60 * 60

_ERROR_KEYWORDS = (
    "error", "exception", "traceback", "stack trace", "stacktrace",
    "failed", "failure", "fatal", "undefined is not", "cannot find",
    "not found", "denied", "panic:", "segfault", "null pointer",
    "unhandled", "crash",
)

_SEARCH_KEYWORDS = (
    "stack overflow", "stackoverflow", "google search", "google.com/search",
    "bing.com", "duckduckgo", "- google", "- bing", "- duckduckgo",
)


def _has_error_keyword(title: Optional[str]) -> bool:
    if not title:
        return False
    lowered = title.lower()
    return any(kw in lowered for kw in _ERROR_KEYWORDS)


def _looks_like_search_tab(app: Optional[str], title: Optional[str]) -> bool:
    blob = f"{app or ''} {title or ''}".lower()
    return any(kw in blob for kw in _SEARCH_KEYWORDS)


def check_stuck(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Heuristic stuck-detector over AW window events (newest first).

    Each event is shaped ``{"timestamp": iso, "duration": seconds,
    "data": {"app": ..., "title": ...}}`` -- the shape returned by
    :meth:`tools.presence.aw_client.AWClient.get_events`.

    Triggers only when BOTH hold:
      1. The current foreground window (same app + title) has held focus
         for at least :data:`STUCK_MIN_DURATION_SECONDS`.
      2. Within the events leading up to that window, either an
         error-looking title was seen, or the user rapidly bounced through
         several search/Stack Overflow tabs.

    Returns a finding dict, or None (including on any malformed input --
    this must never raise from the media-watcher poll loop).
    """
    if not events:
        return None

    try:
        current = events[0]
        cur_data = current.get("data") or {}
        app, title = cur_data.get("app"), cur_data.get("title")
        if not title:
            return None

        same_window_seconds = 0.0
        settled_index = 0
        for event in events:
            data = event.get("data") or {}
            if data.get("app") == app and data.get("title") == title:
                try:
                    same_window_seconds += float(event.get("duration") or 0.0)
                except (TypeError, ValueError):
                    pass
                settled_index += 1
            else:
                break

        if same_window_seconds < STUCK_MIN_DURATION_SECONDS:
            return None

        lookback = events[settled_index:settled_index + _LOOKBACK_EVENT_COUNT]

        error_hit = _has_error_keyword(title) or any(
            _has_error_keyword((ev.get("data") or {}).get("title")) for ev in lookback
        )

        search_hits = sum(
            1 for ev in lookback
            if _looks_like_search_tab((ev.get("data") or {}).get("app"), (ev.get("data") or {}).get("title"))
        )
        rapid_switch_hit = search_hits >= 3

        if not (error_hit or rapid_switch_hit):
            return None

        return {
            "stuck": True,
            "app": app,
            "title": title,
            "duration_seconds": round(same_window_seconds),
            "signal": "error_keyword" if error_hit else "rapid_search_switching",
        }
    except Exception:
        logger.debug("goblin.check_stuck: unexpected input shape", exc_info=True)
        return None


def _debounce_state_path():
    from hermes_cli.config import get_hermes_home

    return get_hermes_home() / "presence" / "goblin_state.json"


def _last_notified_at() -> float:
    path = _debounce_state_path()
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        return float(data.get("last_notified_at", 0.0))
    except Exception:
        return 0.0


def _mark_notified() -> None:
    path = _debounce_state_path()
    try:
        import json

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_notified_at": time.time()}), encoding="utf-8")
    except OSError:
        logger.debug("goblin: failed to persist debounce state", exc_info=True)


def should_notify_now() -> bool:
    """True when the last shoulder-tap notification was >= 2h ago (or never)."""
    return (time.time() - _last_notified_at()) >= DEBOUNCE_SECONDS


def _pick_delivery_target() -> Optional[str]:
    """Best-effort: the first connected platform with a configured home
    channel, as a bare "<platform>" delivery target (routes to that
    platform's home channel per gateway.delivery.DeliveryTarget.parse)."""
    try:
        from gateway.config import load_gateway_config

        cfg = load_gateway_config()
        for platform in cfg.get_connected_platforms():
            if cfg.get_home_channel(platform):
                return platform.value
    except Exception:
        logger.debug("goblin: could not resolve a delivery target", exc_info=True)
    return None


def notify_stuck(finding: Dict[str, Any]) -> bool:
    """Best-effort: schedule a one-shot cron job that gently offers to help.

    Delivered through the existing cron delivery path (gateway/delivery.py),
    so it automatically passes through the flow gate like any other
    proactive/cron-originated message. Debounced to once per
    :data:`DEBOUNCE_SECONDS`. Returns True iff a notification job was
    created.
    """
    if not should_notify_now():
        return False
    target = _pick_delivery_target()
    if not target:
        logger.debug("goblin: no configured delivery target; skipping shoulder tap")
        return False

    minutes = round(finding.get("duration_seconds", 0) / 60)
    signal = finding.get("signal")
    signal_desc = (
        "there's error-looking text in the window title"
        if signal == "error_keyword"
        else "they keep bouncing between search / Stack Overflow tabs"
    )
    prompt = (
        "You are Marvi, keeping half an eye on the user's desktop presence "
        "(local ActivityWatch data only). The user appears to have been "
        f"stuck: \"{finding.get('title')}\" ({finding.get('app')}) has been "
        f"in the foreground for about {minutes} minute(s), and {signal_desc}. "
        "Send ONE short, warm, low-pressure message offering to help -- easy "
        "to ignore, no guilt-tripping. If reaching out doesn't actually seem "
        "appropriate right now, reply exactly [SILENT] and nothing else."
    )
    try:
        from cron.jobs import create_job

        create_job(
            prompt=prompt,
            schedule="1m",
            name="presence-goblin-shoulder-tap",
            repeat=1,
            deliver=target,
        )
    except Exception:
        logger.warning("goblin: failed to create shoulder-tap job", exc_info=True)
        return False
    _mark_notified()
    return True


def check_stuck_and_notify() -> Optional[Dict[str, Any]]:
    """Poll AW window history, run :func:`check_stuck`, and fire a debounced
    shoulder-tap when triggered. Safe no-op when AW is unreachable or
    ``presence.goblin.shoulder_taps`` is disabled. Called periodically
    (~every 5 min) from the media watcher's poll loop.
    """
    from tools.presence.common import get_presence_config

    cfg = get_presence_config()
    if not cfg.get("goblin", {}).get("shoulder_taps"):
        return None

    from tools.presence.aw_client import AWUnavailableError, aw_client

    if not aw_client.is_available():
        return None
    bucket_id = aw_client.find_bucket_id("aw-watcher-window")
    if not bucket_id:
        return None
    try:
        events = aw_client.get_events(bucket_id, limit=STUCK_SCAN_LIMIT)
    except AWUnavailableError:
        return None

    finding = check_stuck(events)
    if finding:
        notify_stuck(finding)
    return finding


# --- Session priming ---------------------------------------------------


def session_priming_summary() -> Optional[str]:
    """One-paragraph plain-English summary of the last hour of presence.

    Returns None when ``presence.goblin.session_priming`` is off, AW is
    unavailable, or there's nothing notable to say -- so a caller can
    invoke this unconditionally (``summary = session_priming_summary();
    if summary: ...``) without re-checking the config flag itself.

    Injection-point handoff: the natural call site is gateway/run.py's
    ``session:start`` emit (or wherever the new session's context is
    assembled) -- files owned by Workstream A, so the one-line wiring
    lives outside this module. Everything else (config gate, AW probe,
    denylist-respecting summary text) is complete here.
    """
    from tools.presence.common import get_presence_config

    cfg = get_presence_config()
    if not cfg.get("goblin", {}).get("session_priming"):
        return None

    try:
        from tools.presence.context import desktop_context

        data = desktop_context("now")
    except Exception:
        logger.debug("goblin: session priming context failed", exc_info=True)
        return None

    if not data.get("available"):
        return None

    parts: List[str] = []
    window = data.get("window") or {}
    if not window.get("redacted"):
        if window.get("workspace"):
            file_note = f" ({window['file']})" if window.get("file") else ""
            parts.append(f"coding in {window['workspace']}{file_note}")
        elif window.get("cwd"):
            parts.append(f"working in a terminal at {window['cwd']}")
        elif window.get("app"):
            parts.append(f"using {window['app']}")

    now_playing = data.get("now_playing") or {}
    if now_playing.get("title") and not now_playing.get("redacted"):
        artist_note = f" by {now_playing['artist']}" if now_playing.get("artist") else ""
        parts.append(f"listening to \"{now_playing['title']}\"{artist_note}")

    if data.get("afk") == "afk":
        parts.append("currently away from the keyboard")

    if not parts:
        return None

    return "In the last hour, the user has been " + ", ".join(parts) + "."
