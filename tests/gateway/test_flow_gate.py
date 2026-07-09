"""Tests for gateway/flow_gate.py -- the presence flow gate that holds
cron/proactive deliveries while the user is heads-down in a focus app.

No real ActivityWatch or gateway needed: aw_client and the presence config
reader are monkeypatched at their source modules (flow_gate imports both
lazily inside its functions, so patching the module attribute takes effect
on the next call).
"""

import asyncio

import pytest

import gateway.flow_gate as flow_gate
import tools.presence.aw_client as aw_client_mod
import tools.presence.common as common_mod


class FakeAWClient:
    def __init__(self, *, available=True, afk="not-afk", app=None):
        self.available = available
        self.afk = afk
        self.app = app

    def is_available(self, force=False):
        return self.available

    def get_afk_state(self):
        return self.afk

    def get_current_window(self):
        if self.app is None:
            return None
        return {"data": {"app": self.app, "title": "some title"}}


def _install_client(monkeypatch, fake):
    monkeypatch.setattr(aw_client_mod, "aw_client", fake)


def _install_presence_cfg(monkeypatch, *, enabled=True, flow_gating=True):
    monkeypatch.setattr(
        common_mod, "get_presence_config",
        lambda: {"enabled": enabled, "flow_gating": flow_gating, "goblin": {}},
    )


class TestIsCronOrigin:
    def test_job_id_present(self):
        assert flow_gate.is_cron_origin({"job_id": "abc"}) is True

    def test_job_id_absent(self):
        assert flow_gate.is_cron_origin({}) is False
        assert flow_gate.is_cron_origin(None) is False

    def test_job_id_empty_string_is_falsy(self):
        assert flow_gate.is_cron_origin({"job_id": ""}) is False


class TestShouldGate:
    def test_no_job_id_never_gates(self, monkeypatch):
        _install_presence_cfg(monkeypatch, enabled=True, flow_gating=True)
        _install_client(monkeypatch, FakeAWClient(available=True, afk="not-afk", app="Code.exe"))
        assert flow_gate.should_gate({}) is False

    def test_presence_disabled_no_gate(self, monkeypatch):
        _install_presence_cfg(monkeypatch, enabled=False, flow_gating=True)
        _install_client(monkeypatch, FakeAWClient(available=True, afk="not-afk", app="Code.exe"))
        assert flow_gate.should_gate({"job_id": "j1"}) is False

    def test_flow_gating_disabled_no_gate(self, monkeypatch):
        _install_presence_cfg(monkeypatch, enabled=True, flow_gating=False)
        _install_client(monkeypatch, FakeAWClient(available=True, afk="not-afk", app="Code.exe"))
        assert flow_gate.should_gate({"job_id": "j1"}) is False

    def test_aw_unavailable_no_gate(self, monkeypatch):
        _install_presence_cfg(monkeypatch, enabled=True, flow_gating=True)
        _install_client(monkeypatch, FakeAWClient(available=False))
        assert flow_gate.should_gate({"job_id": "j1"}) is False

    def test_user_afk_no_gate(self, monkeypatch):
        _install_presence_cfg(monkeypatch, enabled=True, flow_gating=True)
        _install_client(monkeypatch, FakeAWClient(available=True, afk="afk", app="Code.exe"))
        assert flow_gate.should_gate({"job_id": "j1"}) is False

    def test_non_focus_app_no_gate(self, monkeypatch):
        _install_presence_cfg(monkeypatch, enabled=True, flow_gating=True)
        _install_client(monkeypatch, FakeAWClient(available=True, afk="not-afk", app="chrome.exe"))
        assert flow_gate.should_gate({"job_id": "j1"}) is False

    def test_active_in_focus_app_gates(self, monkeypatch):
        _install_presence_cfg(monkeypatch, enabled=True, flow_gating=True)
        _install_client(monkeypatch, FakeAWClient(available=True, afk="not-afk", app="Code.exe"))
        assert flow_gate.should_gate({"job_id": "j1"}) is True


class TestWaitIfGated:
    @pytest.mark.asyncio
    async def test_not_gated_returns_immediately(self, monkeypatch):
        _install_presence_cfg(monkeypatch, enabled=False)
        _install_client(monkeypatch, FakeAWClient(available=True, afk="not-afk", app="Code.exe"))
        # Should return without ever sleeping.
        await asyncio.wait_for(flow_gate.wait_if_gated({"job_id": "j1"}), timeout=1.0)

    @pytest.mark.asyncio
    async def test_flushes_once_user_leaves_focus_app(self, monkeypatch):
        monkeypatch.setattr(flow_gate, "POLL_INTERVAL_SECONDS", 0.01)
        monkeypatch.setattr(flow_gate, "MAX_HOLD_SECONDS", 5.0)
        _install_presence_cfg(monkeypatch, enabled=True, flow_gating=True)
        fake = FakeAWClient(available=True, afk="not-afk", app="Code.exe")
        _install_client(monkeypatch, fake)

        async def _flip_after_delay():
            await asyncio.sleep(0.03)
            fake.app = "chrome.exe"  # user switched away from the focus app

        flipper = asyncio.create_task(_flip_after_delay())
        await asyncio.wait_for(flow_gate.wait_if_gated({"job_id": "j1"}), timeout=2.0)
        await flipper

    @pytest.mark.asyncio
    async def test_fails_open_after_max_hold(self, monkeypatch):
        monkeypatch.setattr(flow_gate, "POLL_INTERVAL_SECONDS", 0.01)
        monkeypatch.setattr(flow_gate, "MAX_HOLD_SECONDS", 0.03)
        _install_presence_cfg(monkeypatch, enabled=True, flow_gating=True)
        # User never leaves the focus app -- must still flush via the ceiling.
        _install_client(monkeypatch, FakeAWClient(available=True, afk="not-afk", app="Code.exe"))
        await asyncio.wait_for(flow_gate.wait_if_gated({"job_id": "j1"}), timeout=2.0)

    @pytest.mark.asyncio
    async def test_shutdown_flag_flushes_immediately(self, monkeypatch):
        monkeypatch.setattr(flow_gate, "POLL_INTERVAL_SECONDS", 0.01)
        monkeypatch.setattr(flow_gate, "MAX_HOLD_SECONDS", 10.0)
        _install_presence_cfg(monkeypatch, enabled=True, flow_gating=True)
        _install_client(monkeypatch, FakeAWClient(available=True, afk="not-afk", app="Code.exe"))
        flow_gate._shutdown_event.set()
        try:
            await asyncio.wait_for(flow_gate.wait_if_gated({"job_id": "j1"}), timeout=1.0)
        finally:
            flow_gate._shutdown_event.clear()
