"""Tiered voice residency — hot/cold state tracking for the desktop voice stack.

The desktop voice stack (PocketTTS, the persistent Parakeet STT helper
subprocess, and the wake-word spotter) is warmed once at startup and, today,
stays resident forever. That is deliberate for the wake word (tiny CPU model,
must always be instant) but wasteful for PocketTTS and Parakeet when the user
hasn't spoken in a while — they pin GPU/RAM even during idle stretches.

This module is the shared *tier manager*: it tracks whether the voice stack
is "hot" (recently used) or "cold" (demoted after idling or under memory
pressure) and runs registered *demote hooks* when transitioning to cold.
Promotion back to "hot" is lazy by design — this module does not reload
anything itself. Each subsystem's existing lazy loader (``_ensure_process``,
``_resolve_pockettts_model_and_voice``, etc.) already re-warms on next use;
calling :func:`note_voice_activity` after a demotion simply flips the tracked
tier back to "hot" so the idle clock resets.

Kept dependency-free (no torch/nemo/config imports) so it can be imported
cheaply from anywhere without pulling in the heavy voice-model stack.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

TIER_HOT = "hot"
TIER_COLD = "cold"

_lock = threading.RLock()
_tier: str = TIER_HOT
_last_activity: float = time.monotonic()
_demote_hooks: List[Callable[[], None]] = []
_active_sessions = 0

_idle_lock = threading.Lock()
_idle_thread: Optional[threading.Thread] = None
_idle_stop_event: Optional[threading.Event] = None
_idle_interval_seconds: float = 0.0

# Background thread never sleeps longer than this between idle checks, so a
# small idle_minutes value (e.g. in tests) is still observed promptly.
_MAX_POLL_SECONDS = 5.0


def note_voice_activity() -> None:
    """Record voice use *now*. Resets the idle clock and marks the tier hot.

    Safe to call from any thread. If a demotion happened earlier, this is
    what promotes the tracked tier back to "hot" — the actual model/process
    reload is left to each subsystem's existing lazy loader.
    """
    global _tier, _last_activity
    with _lock:
        _last_activity = time.monotonic()
        _tier = TIER_HOT


def current_tier() -> str:
    """Return ``"hot"`` or ``"cold"``."""
    with _lock:
        return _tier


def begin_voice_session() -> None:
    """Lease the resident models for one live voice session.

    Resource policies may still mark background work as deferred while a game
    or other heavy app is focused, but they must never unload STT/TTS from
    underneath an active duplex call.  The counter (rather than a boolean)
    also covers short reconnect overlaps safely.
    """
    global _active_sessions, _tier, _last_activity
    with _lock:
        _active_sessions += 1
        _last_activity = time.monotonic()
        _tier = TIER_HOT


def end_voice_session() -> None:
    """Release one live-session lease. Idempotent at zero."""
    global _active_sessions, _last_activity
    with _lock:
        _active_sessions = max(0, _active_sessions - 1)
        _last_activity = time.monotonic()


def has_active_session() -> bool:
    with _lock:
        return _active_sessions > 0


def register_demote_hook(fn: Callable[[], None]) -> None:
    """Register a callable to run when :func:`demote` fires. Idempotent per fn."""
    with _lock:
        if fn not in _demote_hooks:
            _demote_hooks.append(fn)


def demote(reason: str) -> bool:
    """Move the voice stack to "cold": run every demote hook, then mark cold.

    Idempotent — calling this while already cold is a no-op (hooks do not
    run twice in a row). Each hook runs in its own try/except so one
    misbehaving subsystem can't block the others from unloading.
    """
    global _tier
    with _lock:
        if _active_sessions:
            logger.info(
                "[VOICE-RESIDENCY] demotion deferred (reason=%s active_sessions=%d)",
                reason,
                _active_sessions,
            )
            return False
        if _tier == TIER_COLD:
            return False
        hooks = list(_demote_hooks)
        _tier = TIER_COLD

    for hook in hooks:
        try:
            hook()
        except Exception:
            logger.exception("[VOICE-RESIDENCY] demote hook failed: %r", hook)

    logger.info("[VOICE-RESIDENCY] demoted to cold (reason=%s)", reason)
    return True


def _idle_watch_loop(stop_event: threading.Event, idle_seconds: float) -> None:
    poll = max(0.01, min(idle_seconds, _MAX_POLL_SECONDS))
    while not stop_event.wait(poll):
        with _lock:
            tier = _tier
            last = _last_activity
        if tier != TIER_HOT:
            continue
        if (time.monotonic() - last) >= idle_seconds:
            demote("idle")


def start_idle_watch(idle_minutes: float) -> None:
    """Start a daemon thread that demotes the voice stack after idling.

    ``idle_minutes <= 0`` disables the watch (no thread is started). Safe to
    call multiple times — a second call while a watch is already running is
    a no-op. Mirrors ``gateway.memory_monitor``'s background-thread style.
    """
    global _idle_thread, _idle_stop_event, _idle_interval_seconds

    with _idle_lock:
        if _idle_thread is not None and _idle_thread.is_alive():
            return

        try:
            idle_minutes = float(idle_minutes)
        except (TypeError, ValueError):
            idle_minutes = 30.0

        if idle_minutes <= 0:
            logger.info("[VOICE-RESIDENCY] idle watch disabled (idle_unload_minutes<=0)")
            return

        idle_seconds = idle_minutes * 60.0
        _idle_interval_seconds = idle_seconds
        stop_event = threading.Event()
        _idle_stop_event = stop_event
        _idle_thread = threading.Thread(
            target=_idle_watch_loop,
            args=(stop_event, idle_seconds),
            name="voice-residency-idle-watch",
            daemon=True,
        )
        _idle_thread.start()
        logger.info(
            "[VOICE-RESIDENCY] idle watch started (idle_unload_minutes=%.2f)",
            idle_minutes,
        )


def stop_idle_watch(timeout: float = 2.0) -> None:
    """Stop the idle-watch thread. Safe to call even if it was never started."""
    global _idle_thread, _idle_stop_event

    with _idle_lock:
        if _idle_stop_event is None or _idle_thread is None:
            return
        _idle_stop_event.set()
        thread = _idle_thread
        _idle_thread = None
        _idle_stop_event = None

    try:
        thread.join(timeout=timeout)
    except Exception:
        pass


def is_idle_watch_running() -> bool:
    with _idle_lock:
        return _idle_thread is not None and _idle_thread.is_alive()


def _reset_for_tests() -> None:
    """Test-only helper: restore module state to a clean baseline.

    Stops the idle watch, clears demote hooks, and resets the tier to hot.
    Not part of the public API used by other workstreams.
    """
    stop_idle_watch(timeout=1.0)
    global _tier, _last_activity, _demote_hooks, _active_sessions
    with _lock:
        _tier = TIER_HOT
        _last_activity = time.monotonic()
        _demote_hooks = []
        _active_sessions = 0
