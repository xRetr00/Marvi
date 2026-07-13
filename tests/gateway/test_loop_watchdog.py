"""Tests for gateway.loop_watchdog -- the event-loop lag watchdog.

Verifies the watchdog detects a stalled event loop (simulated with a
synchronous ``time.sleep()`` call made directly on the loop thread, exactly
like a rogue ``requests.get()`` or blocking file read would), logs a single
grep-friendly ``[LOOP-LAG] ...`` line naming the stack, and honors the
config gate + 5s throttle.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest

from gateway import loop_watchdog as lw


@pytest.fixture(autouse=True)
def _ensure_watchdog_stopped():
    """Every test starts from a clean state and leaves one behind."""
    lw.stop_loop_watchdog(timeout=1.0)
    with lw._log_lock:
        lw._last_log_time = None
    yield
    lw.stop_loop_watchdog(timeout=1.0)
    with lw._log_lock:
        lw._last_log_time = None


class TestFormatStack:
    def test_current_thread_produces_real_frames(self):
        stack = lw._format_stack(threading.get_ident())
        assert stack not in ("<no frame available>", "<empty stack>", "<stack extraction failed>")
        # faulthandler-style "file:line:func" segments joined by " < ".
        assert ":" in stack

    def test_unknown_thread_id_is_safe(self):
        stack = lw._format_stack(-1)
        assert stack == "<no frame available>"


class TestMaybeLogLagThrottle:
    def test_first_call_logs(self, caplog):
        caplog.set_level(logging.WARNING, logger="gateway.loop_watchdog")
        lw._maybe_log_lag(drift_ms=300.0, thread_id=threading.get_ident())
        lines = [r for r in caplog.records if "[LOOP-LAG]" in r.getMessage()]
        assert len(lines) == 1
        msg = lines[0].getMessage()
        assert "drift_ms=300" in msg
        assert "thread=" in msg
        assert "stack=" in msg

    def test_second_call_within_window_is_throttled(self, caplog):
        caplog.set_level(logging.WARNING, logger="gateway.loop_watchdog")
        lw._maybe_log_lag(drift_ms=300.0, thread_id=threading.get_ident())
        lw._maybe_log_lag(drift_ms=400.0, thread_id=threading.get_ident())
        lines = [r for r in caplog.records if "[LOOP-LAG]" in r.getMessage()]
        assert len(lines) == 1

    def test_call_after_throttle_window_logs_again(self, caplog, monkeypatch):
        caplog.set_level(logging.WARNING, logger="gateway.loop_watchdog")
        monkeypatch.setattr(lw, "_LOG_THROTTLE_SECONDS", 0.05)
        lw._maybe_log_lag(drift_ms=300.0, thread_id=threading.get_ident())
        time.sleep(0.1)
        lw._maybe_log_lag(drift_ms=300.0, thread_id=threading.get_ident())
        lines = [r for r in caplog.records if "[LOOP-LAG]" in r.getMessage()]
        assert len(lines) == 2


class TestLagFiredSince:
    def test_none_before_any_lag(self):
        assert lw.last_lag_time() is None
        assert lw.lag_fired_since(time.monotonic()) is False

    def test_true_after_lag_logged(self):
        t0 = time.monotonic()
        lw._maybe_log_lag(drift_ms=300.0, thread_id=threading.get_ident())
        assert lw.last_lag_time() is not None
        assert lw.last_lag_time() >= t0
        assert lw.lag_fired_since(t0) is True
        assert lw.lag_fired_since(time.monotonic() + 5) is False


class TestStartStopLifecycle:
    @pytest.mark.asyncio
    async def test_start_returns_true_then_false_while_running(self):
        started = lw.start_loop_watchdog(interval_seconds=10.0)
        assert started is True
        assert lw.is_running() is True

        started_again = lw.start_loop_watchdog(interval_seconds=10.0)
        assert started_again is False

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self):
        lw.start_loop_watchdog(interval_seconds=10.0)
        lw.stop_loop_watchdog(timeout=1.0)
        assert lw.is_running() is False
        # Calling again (already stopped) must not raise.
        lw.stop_loop_watchdog(timeout=1.0)

    def test_start_without_running_loop_returns_false(self):
        # No asyncio event loop running in this (sync) test — start_loop_watchdog
        # must fail closed rather than raising.
        started = lw.start_loop_watchdog()
        assert started is False
        assert lw.is_running() is False


class TestDetectsSimulatedStall:
    @pytest.mark.asyncio
    async def test_lag_detected_and_logged_with_stack_during_sync_stall(self, monkeypatch, caplog):
        # Config gate: enabled, with a low threshold so a short simulated
        # stall reliably crosses it.
        monkeypatch.setattr(lw, "_read_config", lambda: (True, 50.0))
        caplog.set_level(logging.WARNING, logger="gateway.loop_watchdog")

        started = lw.start_loop_watchdog(interval_seconds=0.1)
        assert started is True

        def _blocking_work() -> None:
            # Simulates a rogue synchronous call (e.g. requests.get()) made
            # directly on the event-loop thread -- this literally blocks the
            # running loop for the duration, exactly the failure mode the
            # watchdog exists to catch.
            time.sleep(0.6)

        _blocking_work()

        # Give the independent watcher thread a moment to observe the
        # heartbeat resume and finish logging.
        await asyncio.sleep(0.3)

        lag_lines = [r.getMessage() for r in caplog.records if "[LOOP-LAG]" in r.getMessage()]
        assert lag_lines, "expected the watchdog to detect and log the simulated stall"
        msg = lag_lines[0]
        assert msg.startswith("[LOOP-LAG]")
        assert "drift_ms=" in msg
        assert f"thread={threading.get_ident()}" in msg
        assert "stack=" in msg
        # The stack snippet should name real frames, not the "no frame"
        # fallback -- this is the "accurate reasons" contract: a freeze
        # names the blocking function.
        assert "<no frame available>" not in msg

    @pytest.mark.asyncio
    async def test_disabled_via_config_never_logs(self, monkeypatch, caplog):
        monkeypatch.setattr(lw, "_read_config", lambda: (False, 50.0))
        caplog.set_level(logging.WARNING, logger="gateway.loop_watchdog")

        started = lw.start_loop_watchdog(interval_seconds=0.05)
        assert started is True

        time.sleep(0.4)  # stall the loop; watchdog must no-op while disabled
        await asyncio.sleep(0.2)

        lag_lines = [r.getMessage() for r in caplog.records if "[LOOP-LAG]" in r.getMessage()]
        assert not lag_lines
