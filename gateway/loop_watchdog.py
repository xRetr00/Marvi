"""Event-loop lag watchdog — detects backend freezes and names the blocker.

The desktop backend (``hermes_cli/web_server.py``'s FastAPI app) and the
gateway (``gateway/run.py``) are both long-lived single-event-loop
processes. A synchronous blocking call anywhere in an ``async def`` handler
(a `requests.get()`, a `time.sleep()`, an expensive file read, ...) stalls
*every* concurrent WebSocket connection, HTTP request, and background
watcher — the user experiences this as "the backend froze."

This module answers two questions when that happens:

  1. **How bad was it?** — a background thread maintains an independent
     wall-clock heartbeat that the event loop is expected to touch every
     ``interval_seconds`` (default 0.5s, via ``loop.call_later``). If the
     watcher notices the heartbeat go stale by more than ``threshold_ms``
     (default 250ms), that means the loop thread has been off doing
     something else for at least that long.
  2. **What was running?** — critically, the stack sample is taken by the
     *watcher thread*, independently, **while the loop thread is still
     stalled** (not after the delayed heartbeat callback finally fires).
     By the time a delayed ``call_later`` callback runs, the blocking call
     that caused the delay has already returned — sampling from inside it
     would just show the heartbeat's own frame, not the offender. Sampling
     from a separate thread via ``sys._current_frames()[loop_thread_id]``
     mid-stall captures the actual blocking function.

Emits a single grep-friendly ``[LOOP-LAG] ...`` line per stall, throttled to
once per 5s so a long stall doesn't spam the log.

Config: ``logging.loop_watchdog.enabled`` (default true) and
``logging.loop_watchdog.threshold_ms`` (default 250) in ``config.yaml`` —
see ``hermes_cli/config.py``'s ``DEFAULT_CONFIG["logging"]["loop_watchdog"]``.

Mirrors ``gateway.memory_monitor``'s shape (module-level state guarded by a
lock, ``start_*`` / ``stop_*`` / ``is_running`` API, safe to call from either
process) so both processes wire it up the same way.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback
from typing import Optional

logger = logging.getLogger(__name__)

# "schedule a callback every 500ms" — the on-loop heartbeat cadence.
_DEFAULT_INTERVAL_SECONDS = 0.5
# "if observed drift exceeds 250ms" — the default staleness threshold.
_DEFAULT_THRESHOLD_MS = 250.0
# "Throttle to once per 5s".
_LOG_THROTTLE_SECONDS = 5.0
# "top ~5 frames".
_STACK_FRAME_LIMIT = 5
# How often the watcher thread polls the heartbeat. Capped well below the
# heartbeat interval so a stall is detected promptly (not just once the next
# scheduled heartbeat would have fired anyway).
_POLL_INTERVAL_SECONDS = 0.1

_lock = threading.Lock()
_watch_thread: Optional[threading.Thread] = None
_stop_event: Optional[threading.Event] = None
_loop_thread_id: Optional[int] = None
_interval_seconds: float = _DEFAULT_INTERVAL_SECONDS

_heartbeat_lock = threading.Lock()
_last_heartbeat: Optional[float] = None  # time.monotonic() of the last on-loop tick

_log_lock = threading.Lock()
_last_log_time: Optional[float] = None  # time.monotonic() of the last [LOOP-LAG] emit


def _read_config() -> tuple[bool, float]:
    """Return ``(enabled, threshold_ms)`` from ``logging.loop_watchdog.*``.

    Re-read on every check (not cached) so a live config edit takes effect
    without a restart — matches the lightweight read-cost of
    ``load_config_readonly()`` (cached on file mtime/size internally).
    """
    try:
        from hermes_cli.config import cfg_get, load_config_readonly

        cfg = load_config_readonly()
        enabled = cfg_get(cfg, "logging", "loop_watchdog", "enabled", default=True)
        threshold_ms = cfg_get(
            cfg, "logging", "loop_watchdog", "threshold_ms", default=_DEFAULT_THRESHOLD_MS
        )
        return bool(enabled), float(threshold_ms or _DEFAULT_THRESHOLD_MS)
    except Exception:
        return True, _DEFAULT_THRESHOLD_MS


def _format_stack(thread_id: int, limit: int = _STACK_FRAME_LIMIT) -> str:
    """Faulthandler-style one-line stack for ``thread_id``, innermost frame first.

    Uses ``sys._current_frames()`` (the same primitive ``faulthandler`` and
    ``threading`` introspection tools use) rather than an actual
    ``faulthandler.dump_traceback`` call, because we want a single
    grep-friendly line, not faulthandler's multi-line stderr dump.
    """
    try:
        frame = sys._current_frames().get(thread_id)
    except Exception:
        frame = None
    if frame is None:
        return "<no frame available>"
    try:
        extracted = traceback.extract_stack(frame, limit=limit)
    except Exception:
        return "<stack extraction failed>"
    parts = []
    for filename, lineno, func, _text in reversed(extracted):  # innermost first
        short = filename.replace("\\", "/")
        short = "/".join(short.split("/")[-2:])  # last 2 path components
        parts.append(f"{short}:{lineno}:{func}")
    return " < ".join(parts) if parts else "<empty stack>"


def _heartbeat_tick(loop: "asyncio.AbstractEventLoop", stop_event: threading.Event, interval: float) -> None:
    """Runs ON the loop thread every ``interval`` seconds; records aliveness.

    Reschedules itself via ``call_later`` rather than an ``asyncio.sleep``
    loop so the *scheduling* itself (not just the sleep) is what stalls when
    the loop is busy — exactly the behavior we want to detect.
    """
    global _last_heartbeat
    with _heartbeat_lock:
        _last_heartbeat = time.monotonic()
    if not stop_event.is_set():
        loop.call_later(interval, _heartbeat_tick, loop, stop_event, interval)


def _maybe_log_lag(drift_ms: float, thread_id: int) -> None:
    global _last_log_time
    now = time.monotonic()
    with _log_lock:
        if _last_log_time is not None and (now - _last_log_time) < _LOG_THROTTLE_SECONDS:
            return
        _last_log_time = now
    stack = _format_stack(thread_id)
    logger.warning("[LOOP-LAG] drift_ms=%.0f thread=%d stack=%s", drift_ms, thread_id, stack)


def last_lag_time() -> Optional[float]:
    """``time.monotonic()`` of the most recent ``[LOOP-LAG]`` emission, or None.

    Used by the slow-request middleware to report whether the watchdog fired
    *during* a given request's lifetime (``last_lag_time() is not None and
    last_lag_time() >= request_start_monotonic``).
    """
    with _log_lock:
        return _last_log_time


def lag_fired_since(start_monotonic: float) -> bool:
    """True if a ``[LOOP-LAG]`` line was emitted at or after ``start_monotonic``."""
    fired_at = last_lag_time()
    return fired_at is not None and fired_at >= start_monotonic


def _watch_loop(loop_thread_id: int, stop_event: threading.Event, interval: float) -> None:
    """Independent watcher thread body: polls the heartbeat, samples on stall."""
    poll = min(interval, _POLL_INTERVAL_SECONDS) or _POLL_INTERVAL_SECONDS
    while not stop_event.wait(poll):
        try:
            enabled, threshold_ms = _read_config()
        except Exception:
            enabled, threshold_ms = True, _DEFAULT_THRESHOLD_MS
        if not enabled:
            continue

        with _heartbeat_lock:
            last = _last_heartbeat
        if last is None:
            continue

        now = time.monotonic()
        # How much later than expected is it since the last tick? A healthy
        # loop keeps `now - last` close to `interval`; a stalled loop lets it
        # grow unbounded while the blocking call holds the thread.
        drift_s = (now - last) - interval
        threshold_s = threshold_ms / 1000.0
        if drift_s < threshold_s:
            continue
        try:
            _maybe_log_lag(drift_ms=drift_s * 1000.0, thread_id=loop_thread_id)
        except Exception:
            logger.debug("loop watchdog: failed to log lag", exc_info=True)


def start_loop_watchdog(
    loop: Optional["asyncio.AbstractEventLoop"] = None,
    *,
    interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
) -> bool:
    """Start the loop-lag watchdog for the given (or current running) loop.

    Must be called from a coroutine running on the loop to be watched (so
    ``asyncio.get_running_loop()`` resolves correctly and the heartbeat's
    ``call_later`` binds to that loop) — mirrors
    ``gateway.memory_monitor.start_memory_monitoring``'s "safe to call
    multiple times, no-ops while already running" contract.

    Returns True if a fresh watchdog was started, False if one was already
    running.
    """
    global _watch_thread, _stop_event, _loop_thread_id, _interval_seconds, _last_heartbeat

    with _lock:
        if _watch_thread is not None and _watch_thread.is_alive():
            return False

        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.warning(
                    "[LOOP-LAG] start_loop_watchdog() called with no running event loop; skipping."
                )
                return False

        _loop_thread_id = threading.get_ident()
        _interval_seconds = float(interval_seconds)
        _stop_event = threading.Event()

        # Prime the heartbeat immediately so the watcher thread has a
        # baseline to compare against from the first poll.
        with _heartbeat_lock:
            _last_heartbeat = time.monotonic()

        loop.call_later(_interval_seconds, _heartbeat_tick, loop, _stop_event, _interval_seconds)

        _watch_thread = threading.Thread(
            target=_watch_loop,
            args=(_loop_thread_id, _stop_event, _interval_seconds),
            name="loop-lag-watchdog",
            daemon=True,
        )
        _watch_thread.start()

        logger.info(
            "[LOOP-LAG] Event-loop lag watchdog started (interval=%.1fs, thread=%d)",
            _interval_seconds,
            _loop_thread_id,
        )
        return True


def stop_loop_watchdog(timeout: float = 2.0) -> None:
    """Stop the watcher thread. Safe to call even if never started."""
    global _watch_thread, _stop_event

    with _lock:
        if _stop_event is None or _watch_thread is None:
            return
        _stop_event.set()
        thread = _watch_thread
        _watch_thread = None
        _stop_event = None

    try:
        thread.join(timeout=timeout)
    except Exception:
        pass

    logger.info("[LOOP-LAG] Event-loop lag watchdog stopped")


def is_running() -> bool:
    with _lock:
        return _watch_thread is not None and _watch_thread.is_alive()
