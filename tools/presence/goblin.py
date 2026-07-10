""""Goblin mode" -- opt-in proactive presence features (Contract 3:
``presence.goblin.*``, default OFF).

Two independent pieces:

  - **Shoulder taps**: :func:`check_stuck` is a pure heuristic over a
    window-event history that flags "the user has probably been stuck for
    a while". :func:`check_stuck_and_notify` wraps it with the AW query,
    the config gate, and a debounced (>= 2h) proactive nudge delivered via
    the existing cron delivery path. By default (``presence.goblin.
    investigation``) the nudge job investigates first -- reading the
    identified file/workspace and/or searching the web for the error text --
    before sending a short diagnosis + suggestion, replying ``[SILENT]``
    when it finds nothing useful; set ``investigation: false`` to fall back
    to the old message-only "want help?" offer.
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


# Fixed job name: a repeated shoulder tap re-uses this name, and
# ``_has_pending_shoulder_tap_job`` uses it to detect an in-flight job (one
# that hasn't fired -- and self-deleted, per the one-shot repeat=1 semantics
# in cron/jobs.py -- yet) so repeated taps skip rather than pile up on top of
# each other. Shared by both the investigation and static-offer prompt paths.
SHOULDER_TAP_JOB_NAME = "presence-goblin-shoulder-tap"

# Toolsets handed to the investigation job: file (read the identified
# workspace/file), web (search + fetch pages for the error text). Deliberately
# NOT "cronjob" (the scheduler force-disables it for every cron-spawned agent
# anyway, see cron/scheduler.py::_resolve_cron_disabled_toolsets) and not
# "terminal" (investigation should read, not execute).
INVESTIGATION_TOOLSETS = ["file", "web"]


def _has_pending_shoulder_tap_job() -> bool:
    """True when a previously-created shoulder-tap job hasn't fired yet.

    One-shot jobs (repeat=1) are removed from storage the moment they
    complete (cron/jobs.py's mark_job_run pops the job once its repeat limit
    is hit), so "a job with this name still exists and is enabled" is
    exactly "still pending". Fails open (False) on any lookup error so a
    transient cron-storage hiccup doesn't wedge the shoulder tap forever --
    the 2h debounce is the backstop against pile-up either way.
    """
    try:
        from cron.jobs import list_jobs

        return any(
            job.get("name") == SHOULDER_TAP_JOB_NAME and job.get("enabled", True)
            for job in list_jobs()
        )
    except Exception:
        logger.debug("goblin: could not check for a pending shoulder-tap job", exc_info=True)
        return False


def _signal_description(finding: Dict[str, Any]) -> str:
    return (
        "there's error-looking text in the window title"
        if finding.get("signal") == "error_keyword"
        else "they keep bouncing between search / Stack Overflow tabs"
    )


def _build_static_prompt(finding: Dict[str, Any]) -> str:
    """The original message-only prompt -- offer to help, no investigation.

    Kept byte-for-byte as the fallback for ``presence.goblin.investigation:
    false``.
    """
    minutes = round(finding.get("duration_seconds", 0) / 60)
    signal_desc = _signal_description(finding)
    return (
        "You are Marvi, keeping half an eye on the user's desktop presence "
        "(local ActivityWatch data only). The user appears to have been "
        f"stuck: \"{finding.get('title')}\" ({finding.get('app')}) has been "
        f"in the foreground for about {minutes} minute(s), and {signal_desc}. "
        "Send ONE short, warm, low-pressure message offering to help -- easy "
        "to ignore, no guilt-tripping. If reaching out doesn't actually seem "
        "appropriate right now, reply exactly [SILENT] and nothing else."
    )


def _build_investigation_prompt(finding: Dict[str, Any]) -> str:
    """"Investigate first, then message findings" prompt.

    Hands the agent everything :func:`check_stuck` already knows (window
    title, app, duration, error/search signal) plus a best-effort VS
    Code/terminal parse of the title (workspace, file, cwd), and instructs it
    to actually look before it sends anything -- reading the identified
    file/workspace and/or searching the web for the error text -- then send a
    SHORT diagnosis + one concrete suggestion + an offer to go deeper.
    Explicitly directs a silent [SILENT] reply over a vague "you seem stuck"
    message when investigation turns up nothing useful.
    """
    minutes = round(finding.get("duration_seconds", 0) / 60)
    signal = finding.get("signal")
    signal_desc = _signal_description(finding)
    title = finding.get("title")
    app = finding.get("app")

    context_lines = [
        f'- Window title: "{title}" (app: {app})',
        f"- Same window in the foreground for about {minutes} minute(s)",
        f"- Why this looks stuck: {signal_desc}",
    ]

    try:
        from tools.presence.title_parsing import parse_window

        parsed = parse_window(app, title)
    except Exception:
        logger.debug("goblin: title parsing failed while building investigation prompt", exc_info=True)
        parsed = {}

    if parsed.get("workspace"):
        context_lines.append(f"- VS Code workspace: {parsed['workspace']}")
    if parsed.get("file"):
        context_lines.append(f"- VS Code file open: {parsed['file']}")
    if parsed.get("cwd"):
        context_lines.append(f"- Terminal working directory: {parsed['cwd']}")

    if signal == "error_keyword":
        context_lines.append(
            f'- The error-looking text is in the window title itself: "{title}"'
        )

    context_block = "\n".join(context_lines)

    return (
        "You are Marvi, keeping half an eye on the user's desktop presence "
        "(local ActivityWatch data only). The user appears to have been "
        "stuck on this:\n\n"
        f"{context_block}\n\n"
        "Before saying anything, INVESTIGATE using your available tools:\n"
        "- If a workspace and/or file is identifiable above, read it (and "
        "any obviously-related file nearby) to see what's actually going on.\n"
        "- If the window title (or anything you read) contains error-looking "
        "text, search the web for that error text to find the likely cause "
        "or fix.\n"
        "- Keep this brief -- a couple of targeted tool calls, not a full "
        "debugging session.\n\n"
        "Then send AT MOST ONE short, warm, low-pressure message: briefly say "
        "what you think the problem is, give ONE concrete suggestion to try, "
        "and offer to go deeper if they want help. Easy to ignore, no "
        "guilt-tripping.\n\n"
        "If after investigating you don't have anything useful to say -- "
        "nothing identifiable to read, no clear error, nothing the web "
        "search turns up -- reply exactly [SILENT] and nothing else. Do NOT "
        "send a vague \"you seem stuck\" message when you found nothing."
    )


def notify_stuck(finding: Dict[str, Any]) -> bool:
    """Best-effort: schedule a one-shot cron job that investigates the stuck
    signature and messages the user with what it found (or gently offers to
    help, if ``presence.goblin.investigation`` is off).

    Delivered through the existing cron delivery path (gateway/delivery.py),
    so it automatically passes through the flow gate like any other
    proactive/cron-originated message. Debounced to once per
    :data:`DEBOUNCE_SECONDS`, and skipped outright if a previously-created
    shoulder-tap job is still pending (hasn't fired/self-deleted yet) -- see
    :func:`_has_pending_shoulder_tap_job`. Returns True iff a notification
    job was created.
    """
    if not should_notify_now():
        return False
    if _has_pending_shoulder_tap_job():
        logger.debug("goblin: a shoulder-tap job is already pending; skipping")
        return False
    target = _pick_delivery_target()
    if not target:
        logger.debug("goblin: no configured delivery target; skipping shoulder tap")
        return False

    from tools.presence.common import get_presence_config

    cfg = get_presence_config()
    investigate = cfg.get("goblin", {}).get("investigation", True)

    if investigate:
        prompt = _build_investigation_prompt(finding)
        enabled_toolsets = INVESTIGATION_TOOLSETS
    else:
        prompt = _build_static_prompt(finding)
        enabled_toolsets = None

    try:
        from cron.jobs import create_job

        create_job(
            prompt=prompt,
            schedule="1m",
            name=SHOULDER_TAP_JOB_NAME,
            repeat=1,
            deliver=target,
            enabled_toolsets=enabled_toolsets,
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
