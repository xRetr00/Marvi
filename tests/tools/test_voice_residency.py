"""Tests for tools.voice_residency -- the shared hot/cold tier manager for the
desktop voice stack (PocketTTS, Parakeet STT helper, wake word).

Fakes demote hooks; never touches real models/subprocesses.
"""

from __future__ import annotations

import time

import pytest

from tools import voice_residency as vr


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Every test starts from a clean hot tier with no hooks/idle watch."""
    vr._reset_for_tests()
    yield
    vr._reset_for_tests()


class TestTierTransitions:
    def test_starts_hot(self):
        assert vr.current_tier() == "hot"

    def test_demote_moves_to_cold(self):
        vr.demote("test")
        assert vr.current_tier() == "cold"

    def test_note_voice_activity_promotes_to_hot(self):
        vr.demote("test")
        assert vr.current_tier() == "cold"
        vr.note_voice_activity()
        assert vr.current_tier() == "hot"

    def test_note_voice_activity_is_noop_while_already_hot(self):
        vr.note_voice_activity()
        assert vr.current_tier() == "hot"


class TestDemoteHooks:
    def test_hook_runs_on_demote(self):
        calls = []
        vr.register_demote_hook(lambda: calls.append(1))
        vr.demote("test")
        assert calls == [1]

    def test_hook_registration_is_idempotent(self):
        calls = []

        def hook():
            calls.append(1)

        vr.register_demote_hook(hook)
        vr.register_demote_hook(hook)
        vr.register_demote_hook(hook)
        vr.demote("test")
        assert calls == [1]

    def test_multiple_distinct_hooks_all_run(self):
        calls = []
        vr.register_demote_hook(lambda: calls.append("a"))
        vr.register_demote_hook(lambda: calls.append("b"))
        vr.demote("test")
        assert set(calls) == {"a", "b"}

    def test_demote_while_already_cold_is_noop(self):
        calls = []
        vr.register_demote_hook(lambda: calls.append(1))
        vr.demote("first")
        assert calls == [1]
        vr.demote("second")
        # Hook must not run a second time -- demote() is idempotent while cold.
        assert calls == [1]
        assert vr.current_tier() == "cold"

    def test_a_failing_hook_does_not_block_other_hooks(self):
        calls = []

        def bad_hook():
            raise RuntimeError("boom")

        vr.register_demote_hook(bad_hook)
        vr.register_demote_hook(lambda: calls.append("ok"))
        vr.demote("test")
        assert calls == ["ok"]
        assert vr.current_tier() == "cold"

    def test_demote_logs_reason(self, caplog):
        import logging

        caplog.set_level(logging.INFO, logger="tools.voice_residency")
        vr.demote("idle")
        messages = [r.getMessage() for r in caplog.records]
        assert any("idle" in m for m in messages), messages

    def test_active_session_defers_demotion_until_release(self):
        calls = []
        vr.register_demote_hook(lambda: calls.append("unloaded"))

        vr.begin_voice_session()
        assert vr.has_active_session() is True
        assert vr.demote("heavy-foreground-app") is False
        assert vr.current_tier() == "hot"
        assert calls == []

        vr.end_voice_session()
        assert vr.has_active_session() is False
        assert vr.demote("heavy-foreground-app") is True
        assert vr.current_tier() == "cold"
        assert calls == ["unloaded"]

    def test_overlapping_session_leases_keep_models_hot(self):
        vr.begin_voice_session()
        vr.begin_voice_session()
        vr.end_voice_session()

        assert vr.has_active_session() is True
        assert vr.demote("memory-pressure") is False

        vr.end_voice_session()
        assert vr.has_active_session() is False


class TestIdleWatch:
    def test_idle_watch_demotes_after_timeout(self):
        # idle_minutes tiny enough to fire within the test budget.
        vr.start_idle_watch(idle_minutes=0.01)  # 0.6s
        assert vr.is_idle_watch_running() is True
        time.sleep(1.2)
        assert vr.current_tier() == "cold"
        vr.stop_idle_watch()

    def test_activity_resets_idle_clock(self):
        vr.start_idle_watch(idle_minutes=0.01)  # 0.6s
        # Keep poking activity so the idle watch never has a full idle window.
        for _ in range(3):
            time.sleep(0.3)
            vr.note_voice_activity()
        assert vr.current_tier() == "hot"
        vr.stop_idle_watch()

    def test_zero_idle_minutes_disables_watch(self):
        vr.start_idle_watch(idle_minutes=0)
        assert vr.is_idle_watch_running() is False

    def test_negative_idle_minutes_disables_watch(self):
        vr.start_idle_watch(idle_minutes=-5)
        assert vr.is_idle_watch_running() is False

    def test_double_start_is_noop(self):
        vr.start_idle_watch(idle_minutes=5)
        thread1 = vr._idle_thread
        vr.start_idle_watch(idle_minutes=5)
        assert vr._idle_thread is thread1
        vr.stop_idle_watch()

    def test_stop_without_start_is_noop(self):
        vr.stop_idle_watch()
        assert vr.is_idle_watch_running() is False

    def test_stop_idle_watch_stops_thread(self):
        vr.start_idle_watch(idle_minutes=5)
        assert vr.is_idle_watch_running() is True
        vr.stop_idle_watch()
        assert vr.is_idle_watch_running() is False
