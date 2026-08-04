"""Tests for tools/presence/resource_policy.py -- the "get out of the way"
heavy-foreground-app policy that demotes the voice stack and defers
subconscious background work.

No real Win32/AW calls: every external boundary (config, AW client,
Win32 probes, voice_residency) is monkeypatched/mocked.
"""

from __future__ import annotations

import sys
import types

import pytest

from tools.presence import resource_policy as rp


@pytest.fixture(autouse=True)
def _reset_verdict_cache():
    """Every test starts with a cold should_defer_background_work() cache."""
    rp._verdict_cache = None
    rp._verdict_cache_at = 0.0
    yield
    rp._verdict_cache = None
    rp._verdict_cache_at = 0.0


# ---------------------------------------------------------------------------
# Heavy-app list matching
# ---------------------------------------------------------------------------


class TestMatchesHeavyList:
    def test_case_insensitive_substring_match(self):
        assert rp._matches_heavy_list("OBS64.EXE", ["obs"]) is True

    def test_substring_within_longer_name_matches(self):
        assert rp._matches_heavy_list("Adobe Premiere Pro 2026.exe", ["premiere"]) is True

    def test_no_match(self):
        assert rp._matches_heavy_list("chrome.exe", ["obs", "blender"]) is False

    def test_none_app_name_never_matches(self):
        assert rp._matches_heavy_list(None, ["obs"]) is False

    def test_empty_needle_list_never_matches(self):
        assert rp._matches_heavy_list("obs64.exe", []) is False

    def test_default_list_used_when_no_apps_given(self):
        assert rp._matches_heavy_list("Blender.exe") is True
        assert rp._matches_heavy_list("notepad.exe") is False


class TestHeavyAppsConfig:
    def test_defaults_when_config_missing(self, monkeypatch):
        monkeypatch.setattr(rp, "_presence_cfg", lambda: {})
        assert rp.heavy_apps() == list(rp.DEFAULT_HEAVY_APPS)

    def test_config_override_used_verbatim(self, monkeypatch):
        monkeypatch.setattr(rp, "_presence_cfg", lambda: {"heavy_apps": ["foocraft", "bargame"]})
        assert rp.heavy_apps() == ["foocraft", "bargame"]

    def test_non_list_value_falls_back_to_defaults(self, monkeypatch):
        monkeypatch.setattr(rp, "_presence_cfg", lambda: {"heavy_apps": "not-a-list"})
        assert rp.heavy_apps() == list(rp.DEFAULT_HEAVY_APPS)

    def test_blank_entries_are_dropped(self, monkeypatch):
        monkeypatch.setattr(rp, "_presence_cfg", lambda: {"heavy_apps": ["obs", "  ", ""]})
        assert rp.heavy_apps() == ["obs"]

    def test_default_list_has_sane_size(self):
        # ~8 sane defaults per the design brief.
        assert 6 <= len(rp.DEFAULT_HEAVY_APPS) <= 10


class TestResourcePolicyEnabled:
    def test_enabled_by_default_when_section_missing(self, monkeypatch):
        monkeypatch.setattr(rp, "_presence_cfg", lambda: {})
        assert rp._resource_policy_enabled() is True

    def test_explicit_disable_respected(self, monkeypatch):
        monkeypatch.setattr(
            rp, "_presence_cfg", lambda: {"resource_policy": {"enabled": False}}
        )
        assert rp._resource_policy_enabled() is False

    def test_non_dict_section_falls_back_to_enabled(self, monkeypatch):
        monkeypatch.setattr(rp, "_presence_cfg", lambda: {"resource_policy": "nonsense"})
        assert rp._resource_policy_enabled() is True


# ---------------------------------------------------------------------------
# is_heavy_foreground()
# ---------------------------------------------------------------------------


class TestIsHeavyForeground:
    def test_true_when_app_name_matches(self, monkeypatch):
        monkeypatch.setattr(rp, "_foreground_app_name", lambda: "obs64.exe")
        monkeypatch.setattr(rp, "_is_fullscreen_foreground", lambda: False)
        monkeypatch.setattr(rp, "heavy_apps", lambda: list(rp.DEFAULT_HEAVY_APPS))
        assert rp.is_heavy_foreground() is True

    def test_true_when_fullscreen_even_if_app_not_on_list(self, monkeypatch):
        monkeypatch.setattr(rp, "_foreground_app_name", lambda: "mystery_game.exe")
        monkeypatch.setattr(rp, "_is_fullscreen_foreground", lambda: True)
        assert rp.is_heavy_foreground() is True

    def test_false_when_neither_matches(self, monkeypatch):
        monkeypatch.setattr(rp, "_foreground_app_name", lambda: "notepad.exe")
        monkeypatch.setattr(rp, "_is_fullscreen_foreground", lambda: False)
        assert rp.is_heavy_foreground() is False

    def test_false_when_app_name_probe_raises(self, monkeypatch):
        def _boom():
            raise RuntimeError("no display")

        monkeypatch.setattr(rp, "_foreground_app_name", _boom)
        assert rp.is_heavy_foreground() is False

    def test_false_when_fullscreen_probe_raises(self, monkeypatch):
        monkeypatch.setattr(rp, "_foreground_app_name", lambda: "notepad.exe")

        def _boom():
            raise OSError("win32 error")

        monkeypatch.setattr(rp, "_is_fullscreen_foreground", _boom)
        assert rp.is_heavy_foreground() is False


class TestForegroundAppNameResolution:
    def test_prefers_aw_client_when_available(self, monkeypatch):
        fake_client = types.SimpleNamespace(
            is_available=lambda: True,
            get_current_window=lambda: {"data": {"app": "Blender.exe"}},
        )
        fake_module = types.SimpleNamespace(aw_client=fake_client)
        monkeypatch.setitem(sys.modules, "tools.presence.aw_client", fake_module)
        assert rp._foreground_app_name() == "Blender.exe"

    def test_falls_back_to_win32_when_aw_unavailable(self, monkeypatch):
        fake_client = types.SimpleNamespace(
            is_available=lambda: False,
            get_current_window=lambda: (_ for _ in ()).throw(AssertionError("should not be called")),
        )
        fake_module = types.SimpleNamespace(aw_client=fake_client)
        monkeypatch.setitem(sys.modules, "tools.presence.aw_client", fake_module)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(rp, "_win32_foreground_process_name", lambda: "steam.exe")
        assert rp._foreground_app_name() == "steam.exe"

    def test_non_windows_platform_returns_none_on_aw_failure(self, monkeypatch):
        fake_client = types.SimpleNamespace(
            is_available=lambda: False,
            get_current_window=lambda: None,
        )
        fake_module = types.SimpleNamespace(aw_client=fake_client)
        monkeypatch.setitem(sys.modules, "tools.presence.aw_client", fake_module)
        monkeypatch.setattr(sys, "platform", "linux")
        assert rp._foreground_app_name() is None

    def test_aw_probe_exception_falls_through_to_win32(self, monkeypatch):
        def _boom():
            raise RuntimeError("connection refused")

        fake_client = types.SimpleNamespace(is_available=_boom)
        fake_module = types.SimpleNamespace(aw_client=fake_client)
        monkeypatch.setitem(sys.modules, "tools.presence.aw_client", fake_module)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(rp, "_win32_foreground_process_name", lambda: "unity.exe")
        assert rp._foreground_app_name() == "unity.exe"


class TestFullscreenForeground:
    def test_false_on_non_windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        assert rp._is_fullscreen_foreground() is False

    def test_false_when_hwnd_probe_raises(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")

        def _boom():
            raise OSError("no window")

        monkeypatch.setattr(rp, "_win32_foreground_hwnd", _boom)
        assert rp._is_fullscreen_foreground() is False

    def test_false_when_no_foreground_hwnd(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(rp, "_win32_foreground_hwnd", lambda: 0)
        assert rp._is_fullscreen_foreground() is False

    def test_delegates_to_window_is_fullscreen(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(rp, "_win32_foreground_hwnd", lambda: 12345)
        monkeypatch.setattr(rp, "_win32_window_is_fullscreen", lambda hwnd: hwnd == 12345)
        assert rp._is_fullscreen_foreground() is True


# ---------------------------------------------------------------------------
# should_defer_background_work() -- TTL cache + enable gate
# ---------------------------------------------------------------------------


class TestShouldDeferBackgroundWork:
    def test_false_when_policy_disabled_even_if_heavy(self, monkeypatch):
        monkeypatch.setattr(rp, "_resource_policy_enabled", lambda: False)
        monkeypatch.setattr(rp, "is_heavy_foreground", lambda: True)
        assert rp.should_defer_background_work() is False

    def test_true_when_enabled_and_heavy(self, monkeypatch):
        monkeypatch.setattr(rp, "_resource_policy_enabled", lambda: True)
        monkeypatch.setattr(rp, "is_heavy_foreground", lambda: True)
        assert rp.should_defer_background_work() is True

    def test_false_when_enabled_and_not_heavy(self, monkeypatch):
        monkeypatch.setattr(rp, "_resource_policy_enabled", lambda: True)
        monkeypatch.setattr(rp, "is_heavy_foreground", lambda: False)
        assert rp.should_defer_background_work() is False

    def test_verdict_is_cached_within_ttl(self, monkeypatch):
        calls = {"n": 0}

        def _heavy():
            calls["n"] += 1
            return True

        monkeypatch.setattr(rp, "_resource_policy_enabled", lambda: True)
        monkeypatch.setattr(rp, "is_heavy_foreground", _heavy)

        clock = {"t": 1000.0}
        monkeypatch.setattr(rp.time, "monotonic", lambda: clock["t"])

        assert rp.should_defer_background_work() is True
        clock["t"] += 5.0  # well within the 30s TTL
        assert rp.should_defer_background_work() is True
        assert calls["n"] == 1

    def test_verdict_reprobes_after_ttl_expires(self, monkeypatch):
        calls = {"n": 0}

        def _heavy():
            calls["n"] += 1
            return calls["n"] == 1  # True first time, False second time

        monkeypatch.setattr(rp, "_resource_policy_enabled", lambda: True)
        monkeypatch.setattr(rp, "is_heavy_foreground", _heavy)

        clock = {"t": 1000.0}
        monkeypatch.setattr(rp.time, "monotonic", lambda: clock["t"])

        assert rp.should_defer_background_work() is True
        clock["t"] += rp._VERDICT_TTL_SECONDS + 1.0
        assert rp.should_defer_background_work() is False
        assert calls["n"] == 2

    def test_force_bypasses_cache(self, monkeypatch):
        calls = {"n": 0}

        def _heavy():
            calls["n"] += 1
            return True

        monkeypatch.setattr(rp, "_resource_policy_enabled", lambda: True)
        monkeypatch.setattr(rp, "is_heavy_foreground", _heavy)

        assert rp.should_defer_background_work() is True
        assert rp.should_defer_background_work(force=True) is True
        assert calls["n"] == 2

    def test_fail_safe_on_exception(self, monkeypatch):
        def _boom():
            raise RuntimeError("config read failed")

        monkeypatch.setattr(rp, "_resource_policy_enabled", _boom)
        assert rp.should_defer_background_work() is False


# ---------------------------------------------------------------------------
# enforce()
# ---------------------------------------------------------------------------


class TestEnforce:
    def _install_fake_voice_residency(self, monkeypatch, *, tier="hot"):
        # `from tools import voice_residency` resolves to a *module
        # attribute*, not just a sys.modules entry -- if the submodule was
        # never actually loaded by the real import machinery, the parent
        # package object won't have the attribute set. Setting it directly
        # (in addition to sys.modules, for anyone doing `import
        # tools.voice_residency`) makes the fake resolve either way.
        import tools as tools_pkg

        calls = {"demote": []}
        fake_module = types.ModuleType("tools.voice_residency")
        fake_module.current_tier = lambda: tier
        fake_module.demote = lambda reason: calls["demote"].append(reason)
        monkeypatch.setitem(sys.modules, "tools.voice_residency", fake_module)
        monkeypatch.setattr(tools_pkg, "voice_residency", fake_module, raising=False)
        return calls

    def test_demotes_when_busy_and_hot(self, monkeypatch):
        monkeypatch.setattr(rp, "should_defer_background_work", lambda: True)
        calls = self._install_fake_voice_residency(monkeypatch, tier="hot")
        rp.enforce()
        assert calls["demote"] == ["heavy-foreground-app"]

    def test_restarts_enabled_media_watcher(self, monkeypatch):
        import hermes_cli.presence_cmd as presence_cmd
        import tools.presence.common as presence_common

        started = []
        monkeypatch.setattr(presence_common, "get_presence_config", lambda: {"enabled": True})
        monkeypatch.setattr(presence_cmd, "watcher_pid_if_running", lambda: None)
        monkeypatch.setattr(presence_cmd, "start_watcher", lambda: (started.append(True) or True, "started"))
        monkeypatch.setattr(rp, "should_defer_background_work", lambda: False)

        rp.enforce()

        assert started == [True]

    def test_no_demote_when_not_busy(self, monkeypatch):
        monkeypatch.setattr(rp, "should_defer_background_work", lambda: False)
        calls = self._install_fake_voice_residency(monkeypatch, tier="hot")
        rp.enforce()
        assert calls["demote"] == []

    def test_no_demote_when_already_cold(self, monkeypatch):
        monkeypatch.setattr(rp, "should_defer_background_work", lambda: True)
        calls = self._install_fake_voice_residency(monkeypatch, tier="cold")
        rp.enforce()
        assert calls["demote"] == []

    def test_missing_voice_residency_module_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(rp, "should_defer_background_work", lambda: True)
        # A None entry in sys.modules is the documented way to force any
        # subsequent import of this exact name to raise ImportError --
        # simulates "tools/voice_residency.py doesn't exist yet" reliably,
        # regardless of whether the parallel workstream has landed it.
        import tools as tools_pkg

        monkeypatch.setitem(sys.modules, "tools.voice_residency", None)
        if hasattr(tools_pkg, "voice_residency"):
            monkeypatch.delattr(tools_pkg, "voice_residency")
        rp.enforce()  # must not raise

    def test_current_tier_exception_does_not_raise(self, monkeypatch):
        import tools as tools_pkg

        monkeypatch.setattr(rp, "should_defer_background_work", lambda: True)
        fake_module = types.ModuleType("tools.voice_residency")

        def _boom():
            raise RuntimeError("residency state corrupt")

        fake_module.current_tier = _boom
        fake_module.demote = lambda reason: None
        monkeypatch.setitem(sys.modules, "tools.voice_residency", fake_module)
        monkeypatch.setattr(tools_pkg, "voice_residency", fake_module, raising=False)
        rp.enforce()  # must not raise
