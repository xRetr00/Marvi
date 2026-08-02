"""Tests for gateway.memory_monitor — periodic process memory logging.

Ported from cline/cline#10343.  The module logs a structured
``[MEMORY] rss=...MB ...`` line periodically so long-running gateway
leaks show up as a time series in agent.log / gateway.log.
"""

from __future__ import annotations

import logging
import time

import pytest

from gateway import memory_monitor as mm


@pytest.fixture(autouse=True)
def _ensure_monitor_stopped():
    """Every test starts from a clean state and leaves one behind."""
    mm.stop_memory_monitoring(timeout=1.0)
    yield
    mm.stop_memory_monitoring(timeout=1.0)


def test_log_memory_usage_has_grep_friendly_format(caplog):
    caplog.set_level(logging.INFO, logger="gateway.memory_monitor")
    mm.log_memory_usage()
    msg = caplog.records[-1].getMessage()
    # Grep-friendly contract: line starts with [MEMORY] and carries RSS
    # (or 'unavailable'), GC counts, thread count, uptime.
    assert msg.startswith("[MEMORY]"), msg
    assert "rss=" in msg
    assert "gc=" in msg
    assert "threads=" in msg
    assert "uptime=" in msg


def test_start_logs_baseline_and_returns_true(caplog):
    caplog.set_level(logging.INFO, logger="gateway.memory_monitor")
    # Large interval so the background timer never fires during the test —
    # we're only checking the synchronous baseline behavior here.
    started = mm.start_memory_monitoring(interval_seconds=3600.0)
    assert started is True
    assert mm.is_running() is True

    messages = [r.getMessage() for r in caplog.records]
    assert any("[MEMORY] baseline " in m for m in messages), messages
    assert any("Periodic memory monitoring started" in m for m in messages), messages


def test_double_start_is_noop():
    assert mm.start_memory_monitoring(interval_seconds=3600.0) is True
    assert mm.start_memory_monitoring(interval_seconds=3600.0) is False
    assert mm.is_running() is True


def test_stop_logs_shutdown_snapshot(caplog):
    mm.start_memory_monitoring(interval_seconds=3600.0)
    caplog.clear()
    caplog.set_level(logging.INFO, logger="gateway.memory_monitor")
    mm.stop_memory_monitoring(timeout=1.0)
    assert mm.is_running() is False

    messages = [r.getMessage() for r in caplog.records]
    assert any("[MEMORY] shutdown " in m for m in messages), messages
    assert any("Periodic memory monitoring stopped" in m for m in messages), messages


def test_stop_without_start_is_noop():
    # Must not raise, must not log shutdown snapshot.
    mm.stop_memory_monitoring(timeout=0.5)
    assert mm.is_running() is False


def test_periodic_timer_fires(caplog):
    caplog.set_level(logging.INFO, logger="gateway.memory_monitor")
    # Short interval so we can observe multiple ticks inside the test budget.
    mm.start_memory_monitoring(interval_seconds=0.1)
    time.sleep(0.45)
    mm.stop_memory_monitoring(timeout=1.0)

    periodic = [
        r for r in caplog.records
        if r.getMessage().startswith("[MEMORY] rss=") or r.getMessage().startswith("[MEMORY] rss=unavailable")
    ]
    # baseline + at least 2 periodic + shutdown — but shutdown has the
    # "shutdown " prefix so it won't match the strict "[MEMORY] rss=" start.
    # We expect >= 3 bare "[MEMORY] rss=..." lines.
    assert len(periodic) >= 3, [r.getMessage() for r in caplog.records]


def test_thread_is_daemon():
    mm.start_memory_monitoring(interval_seconds=3600.0)
    assert mm._monitor_thread is not None
    assert mm._monitor_thread.daemon is True, (
        "memory monitor thread must be daemon so it can never block process exit"
    )


def test_unavailable_rss_warns_and_does_not_start(caplog, monkeypatch):
    # Force both backends to claim unavailable; start should bail.
    monkeypatch.setattr(mm, "_get_rss_mb", lambda: None)
    caplog.set_level(logging.WARNING, logger="gateway.memory_monitor")
    started = mm.start_memory_monitoring(interval_seconds=3600.0)
    assert started is False
    assert mm.is_running() is False
    assert any("Memory monitoring unavailable" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Soft ceiling (logging.memory_monitor.max_rss_mb) — pressure callbacks.
# All tests fake the RSS reading and the ceiling; no real config or models.
# ---------------------------------------------------------------------------


@pytest.fixture()
def _clean_pressure_state(monkeypatch):
    """Isolate the pressure-callback registry + debounce timestamp per test."""
    monkeypatch.setattr(mm, "_pressure_callbacks", [])
    monkeypatch.setattr(mm, "_last_pressure_fire", None)
    yield


def test_pressure_callback_fires_above_ceiling(caplog, monkeypatch, _clean_pressure_state):
    caplog.set_level(logging.INFO, logger="gateway.memory_monitor")
    monkeypatch.setattr(mm, "_max_rss_ceiling_mb", lambda: 1000)
    calls = []
    mm.register_pressure_callback(lambda: calls.append(1))

    mm._maybe_handle_memory_pressure(1500)

    assert calls == [1]
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("[MEMORY] pressure ") for m in messages), messages


def test_pressure_does_not_fire_below_ceiling(_clean_pressure_state, monkeypatch):
    monkeypatch.setattr(mm, "_max_rss_ceiling_mb", lambda: 1000)
    calls = []
    mm.register_pressure_callback(lambda: calls.append(1))

    mm._maybe_handle_memory_pressure(500)

    assert calls == []


def test_pressure_disabled_when_ceiling_is_zero(_clean_pressure_state, monkeypatch):
    monkeypatch.setattr(mm, "_max_rss_ceiling_mb", lambda: 0)
    calls = []
    mm.register_pressure_callback(lambda: calls.append(1))

    mm._maybe_handle_memory_pressure(999999)

    assert calls == []


def test_pressure_ignores_unavailable_rss(_clean_pressure_state, monkeypatch):
    monkeypatch.setattr(mm, "_max_rss_ceiling_mb", lambda: 1000)
    calls = []
    mm.register_pressure_callback(lambda: calls.append(1))

    mm._maybe_handle_memory_pressure(None)

    assert calls == []


def test_pressure_debounces_within_window(_clean_pressure_state, monkeypatch):
    monkeypatch.setattr(mm, "_max_rss_ceiling_mb", lambda: 1000)
    calls = []
    mm.register_pressure_callback(lambda: calls.append(1))

    mm._maybe_handle_memory_pressure(1500)
    mm._maybe_handle_memory_pressure(1500)
    mm._maybe_handle_memory_pressure(2000)

    # Only the first over-ceiling sample within the 10-minute window fires.
    assert calls == [1]


def test_pressure_fires_again_after_debounce_window(_clean_pressure_state, monkeypatch):
    monkeypatch.setattr(mm, "_max_rss_ceiling_mb", lambda: 1000)
    calls = []
    mm.register_pressure_callback(lambda: calls.append(1))

    mm._maybe_handle_memory_pressure(1500)
    assert calls == [1]

    # Simulate the debounce window having elapsed.
    monkeypatch.setattr(
        mm, "_last_pressure_fire", mm._last_pressure_fire - mm._PRESSURE_DEBOUNCE_SECONDS - 1
    )
    mm._maybe_handle_memory_pressure(1500)
    assert calls == [1, 1]


def test_pressure_callback_registration_is_idempotent(_clean_pressure_state, monkeypatch):
    monkeypatch.setattr(mm, "_max_rss_ceiling_mb", lambda: 1000)
    calls = []

    def cb():
        calls.append(1)

    mm.register_pressure_callback(cb)
    mm.register_pressure_callback(cb)
    mm._maybe_handle_memory_pressure(1500)

    assert calls == [1]


def test_failing_pressure_callback_does_not_block_others(_clean_pressure_state, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="gateway.memory_monitor")
    monkeypatch.setattr(mm, "_max_rss_ceiling_mb", lambda: 1000)
    calls = []

    def bad():
        raise RuntimeError("boom")

    mm.register_pressure_callback(bad)
    mm.register_pressure_callback(lambda: calls.append("ok"))
    mm._maybe_handle_memory_pressure(1500)

    assert calls == ["ok"]
    # The pressure line still logs after a callback failure.
    assert any(r.getMessage().startswith("[MEMORY] pressure ") for r in caplog.records)
