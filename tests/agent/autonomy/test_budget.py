"""Tests for the autonomy budget (Marvi freedom spec §1.1,
``agent/autonomy/budget.py``).

HERMES_HOME is isolated per-test by the autouse ``_hermetic_environment``
fixture in ``tests/conftest.py``; the ``_isolate`` fixture below additionally
points the module's own ``get_hermes_home`` at a fresh ``tmp_path`` (mirrors
``tests/cron/test_subconscious_initiatives.py``'s pattern) and stubs
``cron.scheduler.record_subconscious_activity`` so budget spends never touch
the real activity-log module.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    from agent.autonomy import budget

    monkeypatch.setattr(budget, "get_hermes_home", lambda: tmp_path)
    return budget


@pytest.fixture(autouse=True)
def _stub_activity_log(monkeypatch):
    """Budget spends log to cron.scheduler.record_subconscious_activity —
    stub it so tests don't depend on that module's own storage plumbing."""
    calls = []

    def _fake(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("cron.scheduler.record_subconscious_activity", _fake)
    return calls


class TestConfig:
    def test_defaults(self, _isolate):
        cfg = _isolate.autonomy_config({})
        assert cfg["enabled"] is True
        assert cfg["daily_action_budget"] == 8
        assert cfg["per_category"] == {"research": 4, "browse": 2, "ask_user": 3}
        assert cfg["ask"] == {"max_per_day": 3, "quiet_in_deep_work": True}

    def test_config_overrides(self, _isolate):
        cfg = _isolate.autonomy_config(
            {
                "autonomy": {
                    "enabled": False,
                    "daily_action_budget": 20,
                    "per_category": {"research": 10},
                    "ask": {"max_per_day": 1, "quiet_in_deep_work": False},
                }
            }
        )
        assert cfg["enabled"] is False
        assert cfg["daily_action_budget"] == 20
        # Overridden category updates; untouched categories keep their default.
        assert cfg["per_category"]["research"] == 10
        assert cfg["per_category"]["browse"] == 2
        assert cfg["ask"] == {"max_per_day": 1, "quiet_in_deep_work": False}


class TestTrySpend:
    def test_spend_decrements_category_and_total(self, _isolate):
        assert _isolate.try_spend("research") is True
        snap = _isolate.remaining()
        assert snap["categories"]["research"]["used"] == 1
        assert snap["categories"]["research"]["remaining"] == 3
        assert snap["used_total"] == 1
        assert snap["remaining_total"] == 7

    def test_unknown_category_fails_closed(self, _isolate):
        assert _isolate.try_spend("teleportation") is False
        assert _isolate.remaining()["used_total"] == 0

    def test_category_budget_exhausted(self, _isolate):
        # browse: default limit 2
        assert _isolate.try_spend("browse") is True
        assert _isolate.try_spend("browse") is True
        assert _isolate.try_spend("browse") is False
        snap = _isolate.remaining()
        assert snap["categories"]["browse"]["used"] == 2
        assert snap["categories"]["browse"]["remaining"] == 0

    def test_daily_total_caps_across_categories(self, _isolate, tmp_path):
        # Configure a tiny total budget that's smaller than the sum of
        # per-category limits, so the total cap is the binding constraint.
        import hermes_cli.config as config_mod

        monkeypatched_cfg = {
            "autonomy": {"daily_action_budget": 2, "per_category": {"research": 4, "browse": 2, "ask_user": 3}}
        }

        import agent.autonomy.budget as budget

        def _fake_load_config():
            return monkeypatched_cfg

        import hermes_cli.config as cfg_mod

        real_cfg_get = cfg_mod.cfg_get
        # autonomy_config() reads via hermes_cli.config.load_config internally
        # (imported lazily inside the function) -- patch that module's
        # load_config so our tiny total budget takes effect.
        import sys

        orig_load_config = cfg_mod.load_config
        cfg_mod.load_config = _fake_load_config
        try:
            assert budget.try_spend("research") is True
            assert budget.try_spend("browse") is True
            # Total budget (2) is exhausted even though research/browse
            # category limits (4/2) still have room.
            assert budget.try_spend("research") is False
        finally:
            cfg_mod.load_config = orig_load_config

    def test_disabled_autonomy_never_spends(self, _isolate):
        import hermes_cli.config as cfg_mod

        orig_load_config = cfg_mod.load_config
        cfg_mod.load_config = lambda: {"autonomy": {"enabled": False}}
        try:
            assert _isolate.try_spend("research") is False
        finally:
            cfg_mod.load_config = orig_load_config


class TestResetAndPersistence:
    def test_state_persists_across_loads(self, _isolate):
        _isolate.try_spend("research")
        _isolate.try_spend("ask_user")
        snap = _isolate.remaining()
        assert snap["used_total"] == 2

        # Simulate a fresh process load reading the same on-disk state.
        state = _isolate.load_state()
        assert state["used_total"] == 2
        assert state["used"]["research"] == 1
        assert state["used"]["ask_user"] == 1

    def test_reset_if_new_day_zeroes_out_stale_state(self, _isolate):
        _isolate.try_spend("research")
        path = _isolate.budget_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["date"] = "2000-01-01"
        path.write_text(json.dumps(data), encoding="utf-8")

        state = _isolate.load_state()
        assert state["used_total"] == 0
        assert state["used"] == {}

    def test_malformed_state_file_degrades_to_empty(self, _isolate):
        path = _isolate.budget_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        state = _isolate.load_state()
        assert state["used_total"] == 0


class TestRemaining:
    def test_remaining_reflects_disabled_config(self, _isolate):
        import hermes_cli.config as cfg_mod

        orig_load_config = cfg_mod.load_config
        cfg_mod.load_config = lambda: {"autonomy": {"enabled": False}}
        try:
            snap = _isolate.remaining()
            assert snap["enabled"] is False
        finally:
            cfg_mod.load_config = orig_load_config

    def test_remaining_never_raises_on_broken_config(self, _isolate, monkeypatch):
        """Config-read failure fails OPEN (enabled defaults True), mirroring
        agent/memory/graph.py's graph_config / agent/memory/decay.py's
        decay_config — a transient config-read glitch degrades to defaults,
        not to "autonomy silently off"."""

        def _boom():
            raise RuntimeError("disk on fire")

        monkeypatch.setattr("hermes_cli.config.load_config", _boom)
        snap = _isolate.remaining()
        assert isinstance(snap, dict)
        assert snap["enabled"] is True
        assert snap["daily_action_budget"] == 8
