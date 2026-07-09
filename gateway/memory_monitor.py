"""Periodic process memory usage logging for the gateway.

Ported from cline/cline#10343 (src/standalone/memory-monitor.ts).

The gateway is a long-lived process that accumulates memory as it caches
agent instances, session transcripts, tool schemas, memory providers, MCP
connections, etc.  A slow leak in any of those subsystems is invisible
in a single log line — you only see it by watching RSS climb over hours.

This module emits a single structured ``[MEMORY] ...`` line every N
minutes (default 5) so maintainers investigating a suspected leak can
grep ``agent.log`` / ``gateway.log`` for a time series of RSS + Python
GC stats.  The timer runs in a background thread and shuts down cleanly
with the gateway.

Design notes (parity with the Cline port):
  * Grep-friendly single-line format beginning ``[MEMORY]``.
  * Final snapshot logged on shutdown so "last RSS before exit" is
    always in the log.
  * Baseline snapshot logged immediately on start.
  * Daemon thread — never blocks process exit.
  * Uses ``resource`` (stdlib, Linux/macOS) first and falls back to
    ``psutil`` when ``resource`` isn't available (Windows).  Both are
    optional; when neither works we emit a single WARNING and disable
    the monitor rather than crashing the gateway.

Config: ``logging.memory_monitor`` in ``config.yaml`` — see
``hermes_cli/config.py`` for the defaults block.

Soft ceiling: ``logging.memory_monitor.max_rss_mb`` (default 0 = disabled).
When set, each periodic sample that exceeds the ceiling runs registered
*pressure callbacks* (see :func:`register_pressure_callback`), forces a
``gc.collect()``, and logs a single ``[MEMORY] pressure ...`` line. Firing
is debounced to at most once per ``_PRESSURE_DEBOUNCE_SECONDS`` so a
sustained high-RSS period doesn't spam callbacks every tick.
"""

from __future__ import annotations

import gc
import logging
import os
import sys
import threading
import time
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

_BYTES_TO_MB = 1024 * 1024

_monitor_thread: Optional[threading.Thread] = None
_stop_event: Optional[threading.Event] = None
_start_time: Optional[float] = None
_interval_seconds: float = 300.0  # 5 minutes
_lock = threading.Lock()

_pressure_callbacks: List[Callable[[], None]] = []
_pressure_lock = threading.Lock()
_last_pressure_fire: Optional[float] = None
_PRESSURE_DEBOUNCE_SECONDS = 600.0  # 10 minutes


def _get_rss_mb() -> Optional[int]:
    """Return current process resident set size in MB, or None if unavailable.

    Tries ``resource.getrusage`` first (Linux/macOS, no extra deps), then
    falls back to ``psutil`` which is an optional hermes-agent dep.
    """
    # Linux / macOS — resource is stdlib.  On Linux ru_maxrss is in KB,
    # on macOS it is in bytes (yes, really).  We use it as a cheap
    # "current" RSS — ru_maxrss reports the high-water mark for the
    # process, which is what you actually want for leak detection.
    try:
        import resource

        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            return int(maxrss / _BYTES_TO_MB)
        # Linux / other unices: KB
        return int(maxrss / 1024)
    except Exception:
        pass

    # Fallback: psutil (Windows, or unusual unix without resource).
    try:
        import psutil  # type: ignore

        rss = psutil.Process(os.getpid()).memory_info().rss
        return int(rss / _BYTES_TO_MB)
    except Exception:
        return None


def log_memory_usage(prefix: str = "") -> None:
    """Log current memory usage in a grep-friendly ``[MEMORY] ...`` line.

    Safe to call on-demand from any thread at important lifecycle
    moments (after shutdown, after context compression, etc.).

    Parameters
    ----------
    prefix
        Optional extra tag inserted after ``[MEMORY]`` — e.g.
        ``"baseline"``, ``"shutdown"``.
    """
    rss = _get_rss_mb()
    uptime = int(time.monotonic() - _start_time) if _start_time else 0
    # gc.get_stats() returns per-generation collection counts; the sum
    # is a cheap proxy for "how much garbage have we created".
    try:
        gc_counts = gc.get_count()  # (gen0, gen1, gen2)
    except Exception:
        gc_counts = (0, 0, 0)
    # Thread count is a handy correlate when diagnosing thread leaks.
    try:
        thread_count = threading.active_count()
    except Exception:
        thread_count = 0

    tag = f"{prefix} " if prefix else ""
    if rss is None:
        logger.info(
            "[MEMORY] %srss=unavailable gc=%s threads=%d uptime=%ds",
            tag,
            gc_counts,
            thread_count,
            uptime,
        )
    else:
        logger.info(
            "[MEMORY] %srss=%dMB gc=%s threads=%d uptime=%ds",
            tag,
            rss,
            gc_counts,
            thread_count,
            uptime,
        )


def register_pressure_callback(fn: Callable[[], None]) -> None:
    """Register a callable to run when RSS exceeds the configured soft ceiling.

    Mirrors ``tools.voice_residency.register_demote_hook`` — idempotent per
    function, each callback runs in its own try/except at fire time.
    """
    with _pressure_lock:
        if fn not in _pressure_callbacks:
            _pressure_callbacks.append(fn)


def _max_rss_ceiling_mb() -> int:
    """Read ``logging.memory_monitor.max_rss_mb`` (default 0 = disabled).

    Not added to ``DEFAULT_CONFIG`` — read with an explicit default like the
    rest of the codebase's ``cfg_get`` call sites.
    """
    try:
        from hermes_cli.config import cfg_get, load_config_readonly

        cfg = load_config_readonly()
        value = cfg_get(cfg, "logging", "memory_monitor", "max_rss_mb", default=0)
        return int(value or 0)
    except Exception:
        return 0


def _maybe_handle_memory_pressure(rss_mb: Optional[int]) -> None:
    """If ``rss_mb`` exceeds the configured ceiling, fire pressure callbacks.

    Debounced to at most once per ``_PRESSURE_DEBOUNCE_SECONDS`` so callbacks
    don't run on every single tick while RSS stays above the ceiling.
    """
    global _last_pressure_fire

    if rss_mb is None:
        return

    ceiling = _max_rss_ceiling_mb()
    if ceiling <= 0 or rss_mb < ceiling:
        return

    now = time.monotonic()
    with _pressure_lock:
        if _last_pressure_fire is not None and (now - _last_pressure_fire) < _PRESSURE_DEBOUNCE_SECONDS:
            return
        _last_pressure_fire = now
        callbacks = list(_pressure_callbacks)

    for callback in callbacks:
        try:
            callback()
        except Exception:
            logger.exception("[MEMORY] pressure callback failed: %r", callback)

    gc.collect()
    logger.info(
        "[MEMORY] pressure rss=%dMB ceiling=%dMB callbacks=%d",
        rss_mb,
        ceiling,
        len(callbacks),
    )


def _monitor_loop(stop_event: threading.Event, interval: float) -> None:
    """Background thread body — log every ``interval`` seconds until stopped."""
    while not stop_event.wait(interval):
        try:
            log_memory_usage()
            _maybe_handle_memory_pressure(_get_rss_mb())
        except Exception as e:
            # Never let the monitor crash the gateway; just log and carry on.
            logger.debug("Memory monitor iteration failed: %s", e)


def start_memory_monitoring(interval_seconds: float = 300.0) -> bool:
    """Start periodic memory usage logging in a daemon thread.

    Logs immediately to capture a baseline, then every ``interval_seconds``.
    Safe to call multiple times — subsequent calls are no-ops while the
    first monitor is still running.

    Parameters
    ----------
    interval_seconds
        How often to log.  Default 300s (5 minutes), matching the
        upstream cline/cline implementation.

    Returns
    -------
    bool
        True if a fresh monitor thread was started, False if one was
        already running or if memory introspection isn't available.
    """
    global _monitor_thread, _stop_event, _start_time, _interval_seconds

    with _lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return False

        # Sanity-check that we can read RSS at all.  If neither resource
        # nor psutil works, no point spinning a thread that can only log
        # "rss=unavailable" forever — warn once and bail.
        if _get_rss_mb() is None:
            logger.warning(
                "[MEMORY] Memory monitoring unavailable: neither resource.getrusage "
                "nor psutil could read process RSS — skipping periodic logging.",
            )
            return False

        _start_time = time.monotonic()
        _interval_seconds = float(interval_seconds)
        _stop_event = threading.Event()

        # Baseline snapshot before the loop starts.
        log_memory_usage(prefix="baseline")

        _monitor_thread = threading.Thread(
            target=_monitor_loop,
            args=(_stop_event, _interval_seconds),
            name="gateway-memory-monitor",
            daemon=True,
        )
        _monitor_thread.start()

        logger.info(
            "[MEMORY] Periodic memory monitoring started (interval: %ds)",
            int(_interval_seconds),
        )
        return True


def stop_memory_monitoring(timeout: float = 2.0) -> None:
    """Stop the monitor thread and log a final snapshot.

    Safe to call even if ``start_memory_monitoring()`` was never called.
    """
    global _monitor_thread, _stop_event

    with _lock:
        if _stop_event is None or _monitor_thread is None:
            return

        # Final snapshot before teardown so "last RSS" is always in the log.
        try:
            log_memory_usage(prefix="shutdown")
        except Exception:
            pass

        _stop_event.set()
        thread = _monitor_thread
        _monitor_thread = None
        _stop_event = None

    # Join outside the lock so a stuck log call can't deadlock shutdown.
    try:
        thread.join(timeout=timeout)
    except Exception:
        pass

    logger.info("[MEMORY] Periodic memory monitoring stopped")


def is_running() -> bool:
    """True if the background monitor thread is alive."""
    with _lock:
        return _monitor_thread is not None and _monitor_thread.is_alive()
