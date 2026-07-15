"""Tests for gateway/world_trigger.py -- the smart-room real-time proactive
trigger.

``is_wake_worthy``, ``is_arrival_event``, and ``should_trigger`` are pure
(no I/O, no gateway/event loop needed), so most of this is plain unit tests
over those predicates. ``watch()`` is exercised end-to-end with a fake
``plugins.smart_room.runtime.state_store`` module and a fake gateway object
-- never the real plugin/runtime.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from gateway import world_trigger


# ---------------------------------------------------------------------------
# is_wake_worthy
# ---------------------------------------------------------------------------


class TestIsWakeWorthy:
    def test_owntracks_arrive_home_is_wake_worthy(self):
        assert world_trigger.is_wake_worthy(
            {"type": "phone_location_changed", "transition": "arrive", "zone": "home"}
        ) is True

    def test_owntracks_leave_home_is_wake_worthy(self):
        assert world_trigger.is_wake_worthy(
            {"type": "phone_location_changed", "transition": "leave", "zone": "home"}
        ) is True

    def test_owntracks_arrive_non_home_zone_is_not_wake_worthy(self):
        assert world_trigger.is_wake_worthy(
            {"type": "phone_location_changed", "transition": "arrive", "zone": "university"}
        ) is False

    def test_device_offline_is_wake_worthy(self):
        assert world_trigger.is_wake_worthy({"type": "device_offline", "device": "tuya_bulb"}) is True

    def test_presence_detected_after_long_vacancy_is_wake_worthy(self):
        assert world_trigger.is_wake_worthy(
            {"type": "presence_detected", "vacancy_seconds": world_trigger.LONG_VACANCY_SECONDS}
        ) is True
        assert world_trigger.is_wake_worthy(
            {"type": "presence_detected", "vacancy_seconds": world_trigger.LONG_VACANCY_SECONDS + 60}
        ) is True

    def test_presence_detected_after_short_vacancy_is_not_wake_worthy(self):
        assert world_trigger.is_wake_worthy(
            {"type": "presence_detected", "vacancy_seconds": 30.0}
        ) is False

    def test_presence_detected_without_vacancy_info_is_not_wake_worthy(self):
        # No preceding presence_cleared observed this session -- routine
        # flicker vs. genuine return can't be distinguished, so default safe.
        assert world_trigger.is_wake_worthy({"type": "presence_detected", "source": "ble"}) is False

    def test_presence_cleared_is_not_wake_worthy(self):
        assert world_trigger.is_wake_worthy({"type": "presence_cleared"}) is False

    def test_mode_changed_is_not_wake_worthy(self):
        # Light on/off, mode changes -- explicitly excluded per spec.
        assert world_trigger.is_wake_worthy({"type": "mode_changed", "mode": "work"}) is False

    def test_sleep_cancelled_and_alarm_acknowledged_are_not_wake_worthy(self):
        assert world_trigger.is_wake_worthy({"type": "sleep_cancelled", "reason": "evening"}) is False
        assert world_trigger.is_wake_worthy({"type": "alarm_acknowledged", "alarm_id": "a1"}) is False

    def test_non_dict_event_is_not_wake_worthy(self):
        assert world_trigger.is_wake_worthy(None) is False
        assert world_trigger.is_wake_worthy("not a dict") is False

    def test_malformed_vacancy_seconds_is_not_wake_worthy(self):
        assert world_trigger.is_wake_worthy(
            {"type": "presence_detected", "vacancy_seconds": "not-a-number"}
        ) is False


# ---------------------------------------------------------------------------
# is_arrival_event
# ---------------------------------------------------------------------------


class TestIsArrivalEvent:
    def test_presence_detected_is_always_an_arrival(self):
        # The runtime only ever emits presence_detected on a genuine
        # absence -> presence transition -- no vacancy-length filter needed.
        assert world_trigger.is_arrival_event({"type": "presence_detected", "source": "ble"}) is True
        assert world_trigger.is_arrival_event({"type": "presence_detected", "vacancy_seconds": 5}) is True

    def test_owntracks_arrive_home_is_an_arrival(self):
        assert world_trigger.is_arrival_event(
            {"type": "phone_location_changed", "transition": "arrive", "zone": "home"}
        ) is True

    def test_owntracks_leave_home_is_not_an_arrival(self):
        assert world_trigger.is_arrival_event(
            {"type": "phone_location_changed", "transition": "leave", "zone": "home"}
        ) is False

    def test_owntracks_arrive_non_home_is_not_an_arrival(self):
        assert world_trigger.is_arrival_event(
            {"type": "phone_location_changed", "transition": "arrive", "zone": "bakery"}
        ) is False

    def test_presence_cleared_is_not_an_arrival(self):
        assert world_trigger.is_arrival_event({"type": "presence_cleared"}) is False

    def test_device_offline_is_not_an_arrival(self):
        assert world_trigger.is_arrival_event({"type": "device_offline"}) is False

    def test_non_dict_event_is_not_an_arrival(self):
        assert world_trigger.is_arrival_event(None) is False


# ---------------------------------------------------------------------------
# should_trigger (debounce)
# ---------------------------------------------------------------------------


class TestShouldTrigger:
    def test_fires_when_never_triggered_before(self):
        assert world_trigger.should_trigger(
            last_triggered_monotonic=None, now_monotonic=1000.0, debounce_seconds=600
        ) is True

    def test_does_not_fire_within_debounce_window(self):
        assert world_trigger.should_trigger(
            last_triggered_monotonic=1000.0, now_monotonic=1300.0, debounce_seconds=600
        ) is False

    def test_fires_again_once_debounce_window_elapses(self):
        assert world_trigger.should_trigger(
            last_triggered_monotonic=1000.0, now_monotonic=1600.0, debounce_seconds=600
        ) is True

    def test_exact_boundary_fires(self):
        assert world_trigger.should_trigger(
            last_triggered_monotonic=1000.0, now_monotonic=1600.0, debounce_seconds=600.0
        ) is True


# ---------------------------------------------------------------------------
# _enrich_with_vacancy
# ---------------------------------------------------------------------------


class TestEnrichWithVacancy:
    def test_presence_detected_after_presence_cleared_gets_vacancy_seconds(self):
        state = world_trigger._WorldWatchState()
        cleared = {"id": 1, "at": "2026-07-15T10:00:00+00:00", "type": "presence_cleared"}
        detected = {"id": 2, "at": "2026-07-15T12:30:00+00:00", "type": "presence_detected"}

        world_trigger._enrich_with_vacancy(cleared, state)
        enriched = world_trigger._enrich_with_vacancy(detected, state)

        assert enriched["vacancy_seconds"] == pytest.approx(2.5 * 3600)
        # Original event dict must not be mutated -- callers rely on the
        # returned copy only.
        assert "vacancy_seconds" not in detected

    def test_presence_detected_without_prior_cleared_is_unchanged(self):
        state = world_trigger._WorldWatchState()
        detected = {"id": 1, "at": "2026-07-15T12:00:00+00:00", "type": "presence_detected"}

        result = world_trigger._enrich_with_vacancy(detected, state)

        assert result is detected
        assert "vacancy_seconds" not in result

    def test_vacancy_is_consumed_after_one_presence_detected(self):
        # A second presence_detected without an intervening presence_cleared
        # must not get a (stale) vacancy_seconds.
        state = world_trigger._WorldWatchState()
        cleared = {"id": 1, "at": "2026-07-15T10:00:00+00:00", "type": "presence_cleared"}
        first = {"id": 2, "at": "2026-07-15T11:00:00+00:00", "type": "presence_detected"}
        second = {"id": 3, "at": "2026-07-15T11:05:00+00:00", "type": "presence_detected"}

        world_trigger._enrich_with_vacancy(cleared, state)
        world_trigger._enrich_with_vacancy(first, state)
        result = world_trigger._enrich_with_vacancy(second, state)

        assert result is second
        assert "vacancy_seconds" not in result

    def test_other_event_types_pass_through_unchanged(self):
        state = world_trigger._WorldWatchState()
        event = {"id": 1, "type": "mode_changed", "mode": "work"}
        assert world_trigger._enrich_with_vacancy(event, state) is event


# ---------------------------------------------------------------------------
# watch() -- end to end with a fake smart_room state_store + fake gateway
# ---------------------------------------------------------------------------


class FakeGateway:
    def __init__(self):
        self._running = True


def _install_fake_state_store(monkeypatch, events_batches):
    """Installs a fake plugins.smart_room.runtime.state_store module whose
    load_transition_events() pops one batch per call (or [] once exhausted).
    """
    fake_root = types.ModuleType("plugins")
    fake_smart_room = types.ModuleType("plugins.smart_room")
    fake_runtime = types.ModuleType("plugins.smart_room.runtime")
    fake_state_store = types.ModuleType("plugins.smart_room.runtime.state_store")

    batches = list(events_batches)

    def load_transition_events(after_id=0):
        if batches:
            return batches.pop(0)
        return []

    fake_state_store.load_transition_events = load_transition_events
    monkeypatch.setitem(sys.modules, "plugins", fake_root)
    monkeypatch.setitem(sys.modules, "plugins.smart_room", fake_smart_room)
    monkeypatch.setitem(sys.modules, "plugins.smart_room.runtime", fake_runtime)
    monkeypatch.setitem(sys.modules, "plugins.smart_room.runtime.state_store", fake_state_store)
    return fake_state_store


async def _run_watch_for_iterations(monkeypatch, gateway, iterations, poll_interval=0.01):
    """Runs watch() and stops the gateway after N sleep/poll iterations
    have completed, then cancels the task cleanly."""
    count = {"n": 0}
    real_sleep = asyncio.sleep

    async def _counting_sleep(seconds):
        await real_sleep(0)  # yield without actually waiting
        count["n"] += 1
        if count["n"] > iterations:
            gateway._running = False

    monkeypatch.setattr(world_trigger.asyncio, "sleep", _counting_sleep)
    task = asyncio.create_task(world_trigger.watch(gateway, interval=poll_interval))
    await asyncio.wait_for(task, timeout=5.0)


class TestWatchMissingPlugin:
    @pytest.mark.asyncio
    async def test_returns_immediately_when_plugin_not_installed(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "plugins.smart_room.runtime.state_store", None)
        monkeypatch.setitem(sys.modules, "plugins.smart_room.runtime", None)
        gateway = FakeGateway()

        # Must return without ever sleeping/polling -- if it looped, this
        # would hang since gateway._running never flips.
        await asyncio.wait_for(world_trigger.watch(gateway), timeout=2.0)


class TestWatchFirstPollBaseline:
    @pytest.mark.asyncio
    async def test_first_batch_never_triggers_a_tick(self, monkeypatch):
        _install_fake_state_store(
            monkeypatch,
            [[{"id": 1, "at": "2026-07-15T10:00:00+00:00", "type": "phone_location_changed",
               "transition": "arrive", "zone": "home"}]],
        )
        triggered = []
        monkeypatch.setattr(world_trigger, "_debounce_seconds", lambda: 0)
        monkeypatch.setattr(world_trigger, "_flush_on_arrival_enabled", lambda: True)
        monkeypatch.setattr(world_trigger, "_nudge_flow_gate", lambda: triggered.append("nudge"))

        import cron.subconscious as subconscious_mod
        monkeypatch.setattr(subconscious_mod, "trigger_tick", lambda reason="idle": triggered.append(reason) or True)

        gateway = FakeGateway()
        await _run_watch_for_iterations(monkeypatch, gateway, iterations=2)

        # Never triggers a tick or a flow-gate nudge on the baseline batch --
        # this is history from before the watcher started, not a live event.
        assert triggered == []


class TestWatchTriggersOnWakeWorthyEvent:
    @pytest.mark.asyncio
    async def test_fires_tick_on_owner_arrival(self, monkeypatch):
        arrive_event = {
            "id": 2, "at": "2026-07-15T12:00:00+00:00", "type": "phone_location_changed",
            "transition": "arrive", "zone": "home",
        }
        _install_fake_state_store(monkeypatch, [[], [arrive_event]])
        monkeypatch.setattr(world_trigger, "_debounce_seconds", lambda: 0)
        monkeypatch.setattr(world_trigger, "_flush_on_arrival_enabled", lambda: False)

        calls = []
        import cron.subconscious as subconscious_mod
        monkeypatch.setattr(
            subconscious_mod, "trigger_tick", lambda reason="idle": calls.append(reason) or True
        )

        gateway = FakeGateway()
        await _run_watch_for_iterations(monkeypatch, gateway, iterations=3)

        assert calls == ["world"]

    @pytest.mark.asyncio
    async def test_does_not_fire_tick_for_non_wake_worthy_events(self, monkeypatch):
        mode_event = {"id": 2, "at": "2026-07-15T12:00:00+00:00", "type": "mode_changed", "mode": "work"}
        _install_fake_state_store(monkeypatch, [[], [mode_event]])
        monkeypatch.setattr(world_trigger, "_debounce_seconds", lambda: 0)
        monkeypatch.setattr(world_trigger, "_flush_on_arrival_enabled", lambda: False)

        calls = []
        import cron.subconscious as subconscious_mod
        monkeypatch.setattr(
            subconscious_mod, "trigger_tick", lambda reason="idle": calls.append(reason) or True
        )

        gateway = FakeGateway()
        await _run_watch_for_iterations(monkeypatch, gateway, iterations=3)

        assert calls == []

    @pytest.mark.asyncio
    async def test_debounce_suppresses_a_second_tick(self, monkeypatch):
        arrive_event = {
            "id": 2, "at": "2026-07-15T12:00:00+00:00", "type": "phone_location_changed",
            "transition": "arrive", "zone": "home",
        }
        leave_event = {
            "id": 3, "at": "2026-07-15T12:05:00+00:00", "type": "phone_location_changed",
            "transition": "leave", "zone": "home",
        }
        _install_fake_state_store(monkeypatch, [[], [arrive_event], [leave_event]])
        monkeypatch.setattr(world_trigger, "_debounce_seconds", lambda: 600)
        monkeypatch.setattr(world_trigger, "_flush_on_arrival_enabled", lambda: False)

        calls = []
        import cron.subconscious as subconscious_mod
        monkeypatch.setattr(
            subconscious_mod, "trigger_tick", lambda reason="idle": calls.append(reason) or True
        )

        gateway = FakeGateway()
        await _run_watch_for_iterations(monkeypatch, gateway, iterations=4)

        # Only the first wake-worthy event fires -- the second is within the
        # debounce window (real monotonic clock barely advances in-test).
        assert calls == ["world"]


class TestWatchFlowGateNudge:
    @pytest.mark.asyncio
    async def test_nudges_flow_gate_on_arrival_when_enabled(self, monkeypatch):
        arrive_event = {"id": 2, "at": "2026-07-15T12:00:00+00:00", "type": "presence_detected", "source": "ble"}
        _install_fake_state_store(monkeypatch, [[], [arrive_event]])
        monkeypatch.setattr(world_trigger, "_debounce_seconds", lambda: 0)
        monkeypatch.setattr(world_trigger, "_flush_on_arrival_enabled", lambda: True)

        nudged = []
        monkeypatch.setattr(world_trigger, "_nudge_flow_gate", lambda: nudged.append(1))

        import cron.subconscious as subconscious_mod
        monkeypatch.setattr(subconscious_mod, "trigger_tick", lambda reason="idle": True)

        gateway = FakeGateway()
        await _run_watch_for_iterations(monkeypatch, gateway, iterations=3)

        assert nudged == [1]

    @pytest.mark.asyncio
    async def test_does_not_nudge_flow_gate_when_disabled(self, monkeypatch):
        arrive_event = {"id": 2, "at": "2026-07-15T12:00:00+00:00", "type": "presence_detected", "source": "ble"}
        _install_fake_state_store(monkeypatch, [[], [arrive_event]])
        monkeypatch.setattr(world_trigger, "_debounce_seconds", lambda: 0)
        monkeypatch.setattr(world_trigger, "_flush_on_arrival_enabled", lambda: False)

        nudged = []
        monkeypatch.setattr(world_trigger, "_nudge_flow_gate", lambda: nudged.append(1))

        gateway = FakeGateway()
        await _run_watch_for_iterations(monkeypatch, gateway, iterations=3)

        assert nudged == []

    @pytest.mark.asyncio
    async def test_does_not_nudge_flow_gate_for_non_arrival_events(self, monkeypatch):
        leave_event = {
            "id": 2, "at": "2026-07-15T12:00:00+00:00", "type": "phone_location_changed",
            "transition": "leave", "zone": "home",
        }
        _install_fake_state_store(monkeypatch, [[], [leave_event]])
        monkeypatch.setattr(world_trigger, "_debounce_seconds", lambda: 0)
        monkeypatch.setattr(world_trigger, "_flush_on_arrival_enabled", lambda: True)

        nudged = []
        monkeypatch.setattr(world_trigger, "_nudge_flow_gate", lambda: nudged.append(1))

        import cron.subconscious as subconscious_mod
        monkeypatch.setattr(subconscious_mod, "trigger_tick", lambda reason="idle": True)

        gateway = FakeGateway()
        await _run_watch_for_iterations(monkeypatch, gateway, iterations=3)

        assert nudged == []


class TestWatchNeverCrashesOnIterationError:
    @pytest.mark.asyncio
    async def test_load_transition_events_error_does_not_kill_the_watcher(self, monkeypatch):
        fake_root = types.ModuleType("plugins")
        fake_smart_room = types.ModuleType("plugins.smart_room")
        fake_runtime = types.ModuleType("plugins.smart_room.runtime")
        fake_state_store = types.ModuleType("plugins.smart_room.runtime.state_store")

        def _boom(after_id=0):
            raise RuntimeError("disk read failed")

        fake_state_store.load_transition_events = _boom
        monkeypatch.setitem(sys.modules, "plugins", fake_root)
        monkeypatch.setitem(sys.modules, "plugins.smart_room", fake_smart_room)
        monkeypatch.setitem(sys.modules, "plugins.smart_room.runtime", fake_runtime)
        monkeypatch.setitem(sys.modules, "plugins.smart_room.runtime.state_store", fake_state_store)

        gateway = FakeGateway()
        # Must not raise -- the per-iteration try/except swallows it.
        await _run_watch_for_iterations(monkeypatch, gateway, iterations=2)
