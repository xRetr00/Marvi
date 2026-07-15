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
import tools.presence.rhythm as rhythm_mod


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


class TestRhythmStandDown:
    """The rhythm-model early return: outside the user's typical active
    hours for today, the gate stands down entirely (deliver freely) even if
    they happen to be in a focus app right now."""

    def _gateable(self, monkeypatch):
        """Set up a scenario that would definitely gate absent a rhythm."""
        _install_presence_cfg(monkeypatch, enabled=True, flow_gating=True)
        _install_client(monkeypatch, FakeAWClient(available=True, afk="not-afk", app="Code.exe"))

    def test_outside_active_hours_skips_gating(self, monkeypatch):
        self._gateable(monkeypatch)
        monkeypatch.setattr(rhythm_mod, "is_outside_active_hours", lambda now=None: True)
        assert flow_gate.should_gate({"job_id": "j1"}) is False

    def test_inside_active_hours_gates_normally(self, monkeypatch):
        self._gateable(monkeypatch)
        monkeypatch.setattr(rhythm_mod, "is_outside_active_hours", lambda now=None: False)
        assert flow_gate.should_gate({"job_id": "j1"}) is True

    def test_rhythm_check_error_falls_back_to_gating(self, monkeypatch):
        self._gateable(monkeypatch)

        def _boom(now=None):
            raise RuntimeError("rhythm exploded")

        monkeypatch.setattr(rhythm_mod, "is_outside_active_hours", _boom)
        assert flow_gate.should_gate({"job_id": "j1"}) is True

    def test_no_rhythm_file_gates_normally(self, monkeypatch):
        # Real is_outside_active_hours against the isolated HERMES_HOME
        # (no rhythm.json written) -- behavior identical to pre-rhythm code.
        self._gateable(monkeypatch)
        assert flow_gate.should_gate({"job_id": "j1"}) is True

    def _write_rhythm_file(self, weekdays):
        import json
        from datetime import datetime

        path = rhythm_mod._rhythm_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema_version": rhythm_mod.RHYTHM_SCHEMA_VERSION,
            "generated_at": datetime.now().isoformat(),
            "days_analyzed": 5,
            "weekdays": weekdays,
        }), encoding="utf-8")

    def test_fake_rhythm_file_outside_hours_skips_gating(self, monkeypatch):
        # End-to-end through the real rhythm module: write a rhythm file
        # whose active window for *today* clearly excludes the current time.
        from hermes_time import now as hermes_now

        self._gateable(monkeypatch)
        now = hermes_now()
        window = ("22:00", "23:00") if now.hour < 12 else ("01:00", "02:00")
        self._write_rhythm_file({
            str(now.weekday()): {
                "active_start": window[0],
                "active_end": window[1],
                "deep_work_windows": [],
            }
        })
        assert flow_gate.should_gate({"job_id": "j1"}) is False

    def test_fake_rhythm_file_inside_hours_gates_normally(self, monkeypatch):
        from hermes_time import now as hermes_now

        self._gateable(monkeypatch)
        now = hermes_now()
        self._write_rhythm_file({
            str(now.weekday()): {
                "active_start": "00:00",
                "active_end": "23:59",
                "deep_work_windows": [],
            }
        })
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


class TestRequestFlushCheck:
    """The public nudge gateway/world_trigger.py calls on an owner-arrival
    event to make a held delivery re-check should_gate promptly instead of
    waiting out the rest of POLL_INTERVAL_SECONDS."""

    def test_noop_when_nothing_is_held(self):
        # No waiter to wake -- must not raise, must not leave stray state
        # that affects a later, unrelated hold.
        flow_gate.request_flush_check()
        assert flow_gate._flush_requested.is_set()
        flow_gate._flush_requested.clear()

    @pytest.mark.asyncio
    async def test_wakes_a_held_delivery_before_the_poll_interval_elapses(self, monkeypatch):
        # A long poll interval that would never elapse within the test
        # timeout on its own -- only request_flush_check() can end the wait.
        monkeypatch.setattr(flow_gate, "POLL_INTERVAL_SECONDS", 30.0)
        monkeypatch.setattr(flow_gate, "MAX_HOLD_SECONDS", 60.0)
        _install_presence_cfg(monkeypatch, enabled=True, flow_gating=True)
        fake = FakeAWClient(available=True, afk="not-afk", app="Code.exe")
        _install_client(monkeypatch, fake)

        async def _nudge_after_delay():
            await asyncio.sleep(0.03)
            fake.app = "chrome.exe"  # user left the focus app
            flow_gate.request_flush_check()

        nudger = asyncio.create_task(_nudge_after_delay())
        # Would time out well before a 30s poll interval elapses if the
        # nudge didn't wake the wait early.
        await asyncio.wait_for(flow_gate.wait_if_gated({"job_id": "j1"}), timeout=2.0)
        await nudger

    @pytest.mark.asyncio
    async def test_callable_from_a_worker_thread(self, monkeypatch):
        # gateway/world_trigger.py's watcher may call this off the gateway's
        # event-loop thread -- threading.Event is safe to set cross-thread.
        monkeypatch.setattr(flow_gate, "POLL_INTERVAL_SECONDS", 30.0)
        monkeypatch.setattr(flow_gate, "MAX_HOLD_SECONDS", 60.0)
        _install_presence_cfg(monkeypatch, enabled=True, flow_gating=True)
        fake = FakeAWClient(available=True, afk="not-afk", app="Code.exe")
        _install_client(monkeypatch, fake)

        async def _nudge_from_thread_after_delay():
            await asyncio.sleep(0.03)
            fake.app = "chrome.exe"
            await asyncio.to_thread(flow_gate.request_flush_check)

        nudger = asyncio.create_task(_nudge_from_thread_after_delay())
        await asyncio.wait_for(flow_gate.wait_if_gated({"job_id": "j1"}), timeout=2.0)
        await nudger

    @pytest.mark.asyncio
    async def test_nudge_before_any_hold_does_not_cause_early_flush_of_a_later_hold(self, monkeypatch):
        # A stray nudge with nothing held must not make a LATER, unrelated
        # hold flush before the user actually leaves the focus app -- the
        # flag is consumed (cleared) by every wait_if_gated iteration, not
        # just the one that observed it set.
        monkeypatch.setattr(flow_gate, "POLL_INTERVAL_SECONDS", 0.02)
        monkeypatch.setattr(flow_gate, "MAX_HOLD_SECONDS", 5.0)
        _install_presence_cfg(monkeypatch, enabled=True, flow_gating=True)
        fake = FakeAWClient(available=True, afk="not-afk", app="Code.exe")
        _install_client(monkeypatch, fake)

        flow_gate.request_flush_check()  # nothing held yet -- stray nudge

        async def _flip_after_delay():
            await asyncio.sleep(0.15)
            fake.app = "chrome.exe"

        flipper = asyncio.create_task(_flip_after_delay())
        started = asyncio.get_event_loop().time()
        await asyncio.wait_for(flow_gate.wait_if_gated({"job_id": "j1"}), timeout=2.0)
        elapsed = asyncio.get_event_loop().time() - started
        await flipper
        # Must have actually waited for the app switch, not flushed instantly
        # off the stray pre-existing nudge.
        assert elapsed >= 0.1
