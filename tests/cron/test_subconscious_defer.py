"""Tests for the presence resource-policy defer check in
cron/subconscious.py's ``trigger_tick`` -- when the user is running a heavy
foreground app, the subconscious tick is skipped (one log line, return
False) without touching the cron job. Other cron jobs are unaffected (the
check lives only inside trigger_tick, not the scheduler).

Uses an isolated HERMES_HOME so the real config.yaml is never touched.
"""

import importlib
import sys
from unittest.mock import patch

import pytest

import tools.presence.resource_policy as rp


@pytest.fixture
def subconscious(tmp_path, monkeypatch):
    """A cron.subconscious module bound to an isolated HERMES_HOME."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.subconscious as sc
    importlib.reload(sc)
    return sc


@pytest.fixture
def enabled_subconscious(subconscious):
    """Subconscious enabled with a tracked job, so trigger_tick reaches the
    defer check (which sits after the enabled/job_id gate)."""
    with patch("cron.jobs.create_job",
               lambda **k: {"id": "job123", "schedule_display": "every 20m"}):
        subconscious.enable()
    return subconscious


class TestTriggerTickDefer:
    def test_deferred_when_heavy_foreground(self, enabled_subconscious, monkeypatch):
        monkeypatch.setattr(rp, "should_defer_background_work", lambda: True)
        with patch("cron.jobs.trigger_job") as mock_trigger:
            assert enabled_subconscious.trigger_tick(reason="idle") is False
        mock_trigger.assert_not_called()

    def test_runs_normally_when_not_busy(self, enabled_subconscious, monkeypatch):
        monkeypatch.setattr(rp, "should_defer_background_work", lambda: False)
        with patch("cron.jobs.trigger_job", return_value={"id": "job123"}) as mock_trigger:
            assert enabled_subconscious.trigger_tick(reason="idle") is True
        mock_trigger.assert_called_once_with("job123")

    def test_policy_exception_does_not_block_tick(self, enabled_subconscious, monkeypatch):
        def _boom():
            raise RuntimeError("policy blew up")

        monkeypatch.setattr(rp, "should_defer_background_work", _boom)
        with patch("cron.jobs.trigger_job", return_value={"id": "job123"}) as mock_trigger:
            assert enabled_subconscious.trigger_tick(reason="idle") is True
        mock_trigger.assert_called_once_with("job123")

    def test_missing_policy_module_does_not_block_tick(self, enabled_subconscious, monkeypatch):
        # None entry forces the guarded import to raise ImportError.
        monkeypatch.setitem(sys.modules, "tools.presence.resource_policy", None)
        with patch("cron.jobs.trigger_job", return_value={"id": "job123"}) as mock_trigger:
            assert enabled_subconscious.trigger_tick(reason="idle") is True
        mock_trigger.assert_called_once_with("job123")

    def test_disabled_short_circuits_before_policy(self, subconscious, monkeypatch):
        """When subconscious is disabled, the policy is never even consulted."""
        calls = {"n": 0}

        def _counting():
            calls["n"] += 1
            return True

        monkeypatch.setattr(rp, "should_defer_background_work", _counting)
        assert subconscious.trigger_tick() is False
        assert calls["n"] == 0


class TestDeferHelperDirect:
    def test_helper_reflects_policy_verdict(self, subconscious, monkeypatch):
        monkeypatch.setattr(rp, "should_defer_background_work", lambda: True)
        assert subconscious._should_defer_for_resource_policy() is True
        monkeypatch.setattr(rp, "should_defer_background_work", lambda: False)
        assert subconscious._should_defer_for_resource_policy() is False
