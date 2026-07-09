"""Tests for the subconscious idle-trigger debounce logic (gateway/idle_trigger.py).

``should_fire`` is pure (no gateway/event loop needed), so these are plain
unit tests over the predicate.
"""

from gateway.idle_trigger import should_fire


class TestShouldFire:
    def test_fires_after_threshold(self):
        assert should_fire(
            seconds_since_last_inbound=16 * 60,
            idle_trigger_minutes=15,
            last_inbound_at=1000.0,
            last_fired_for_inbound_at=None,
        ) is True

    def test_does_not_fire_before_threshold(self):
        assert should_fire(
            seconds_since_last_inbound=5 * 60,
            idle_trigger_minutes=15,
            last_inbound_at=1000.0,
            last_fired_for_inbound_at=None,
        ) is False

    def test_disabled_when_minutes_non_positive(self):
        assert should_fire(
            seconds_since_last_inbound=999999,
            idle_trigger_minutes=0,
            last_inbound_at=1000.0,
            last_fired_for_inbound_at=None,
        ) is False
        assert should_fire(
            seconds_since_last_inbound=999999,
            idle_trigger_minutes=-5,
            last_inbound_at=1000.0,
            last_fired_for_inbound_at=None,
        ) is False

    def test_does_not_double_fire_same_idle_window(self):
        # Already fired for this exact last_inbound_at — don't fire again.
        assert should_fire(
            seconds_since_last_inbound=20 * 60,
            idle_trigger_minutes=15,
            last_inbound_at=1000.0,
            last_fired_for_inbound_at=1000.0,
        ) is False

    def test_refires_after_new_inbound_and_new_idle_window(self):
        # A newer inbound (2000.0) than the last fire (1000.0) re-arms the trigger.
        assert should_fire(
            seconds_since_last_inbound=16 * 60,
            idle_trigger_minutes=15,
            last_inbound_at=2000.0,
            last_fired_for_inbound_at=1000.0,
        ) is True

    def test_exact_threshold_fires(self):
        assert should_fire(
            seconds_since_last_inbound=15 * 60,
            idle_trigger_minutes=15,
            last_inbound_at=1000.0,
            last_fired_for_inbound_at=None,
        ) is True
