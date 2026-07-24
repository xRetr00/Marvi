"""Tests for plugins/uni_portal/check.py's daily-check orchestration (Marvi
freedom spec §1.3): login -> collect -> diff -> notify -> save snapshot,
the 2FA/CAPTCHA stop-and-ask path, and the disabled/login-failed short
circuits. login/collect/notify/episodic/graph are all faked — no browser,
no network, no credentials.
"""

from __future__ import annotations

import pytest

from plugins.uni_portal import check, snapshot as snap


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(snap, "get_hermes_home", lambda: tmp_path)


@pytest.fixture
def _enabled_config(monkeypatch):
    monkeypatch.setattr(
        "plugins.uni_portal.portal._uni_portal_config",
        lambda: {
            "enabled": True,
            "check_schedule": "0 18 * * *",
            "portal_url": "https://example.edu/login",
            "grades_path": "https://example.edu/grades",
            "announcements_path": "https://example.edu/announcements",
            "schedule_path": "https://example.edu/schedule",
        },
    )


@pytest.fixture
def _no_op_collectors(monkeypatch):
    monkeypatch.setattr("plugins.uni_portal.portal.collect_grades", lambda: [])
    monkeypatch.setattr("plugins.uni_portal.portal.collect_announcements", lambda: [])
    monkeypatch.setattr("plugins.uni_portal.portal.collect_schedule", lambda: [])


class TestDisabled:
    def test_returns_disabled_when_not_enabled(self, monkeypatch):
        monkeypatch.setattr(
            "plugins.uni_portal.portal._uni_portal_config",
            lambda: {"enabled": False, "check_schedule": "", "portal_url": "", "grades_path": "", "announcements_path": "", "schedule_path": ""},
        )
        result = check.run_daily_check()
        assert result == {"ok": False, "changed": False, "error": "disabled"}


class TestLoginFailure:
    def test_login_returning_false_short_circuits(self, _enabled_config, monkeypatch):
        monkeypatch.setattr("plugins.uni_portal.portal.login", lambda: False)
        result = check.run_daily_check()
        assert result["ok"] is False
        assert result["error"] == "login_failed"


class TestLoginBlocked:
    def test_2fa_or_captcha_routes_to_ask_user(self, _enabled_config, monkeypatch):
        from plugins.uni_portal.portal import LoginBlocked

        def _blocked():
            raise LoginBlocked("a verification code.")

        monkeypatch.setattr("plugins.uni_portal.portal.login", _blocked)
        ask_calls = []
        monkeypatch.setattr(
            "agent.autonomy.ask.ask_user",
            lambda question, context="", category="general", **kw: ask_calls.append(question) or {"id": "q1"},
        )

        result = check.run_daily_check()

        assert result["ok"] is False
        assert "login_blocked" in result["error"]
        assert len(ask_calls) == 1
        assert "verification code" in ask_calls[0]

    def test_never_attempts_to_bypass_2fa(self, _enabled_config, monkeypatch, _no_op_collectors):
        """Regression guard for the spec's hard non-goal: a LoginBlocked
        must never lead to collect_grades/collect_announcements being
        called (i.e. no attempt to proceed past the block)."""
        from plugins.uni_portal.portal import LoginBlocked

        monkeypatch.setattr(
            "plugins.uni_portal.portal.login", lambda: (_ for _ in ()).throw(LoginBlocked("captcha"))
        )
        monkeypatch.setattr("agent.autonomy.ask.ask_user", lambda *a, **kw: None)
        collect_calls = []
        monkeypatch.setattr(
            "plugins.uni_portal.portal.collect_grades", lambda: collect_calls.append(1) or []
        )

        check.run_daily_check()

        assert collect_calls == []


class TestNoChanges:
    def test_no_changes_still_saves_snapshot_and_reports_ok(self, _enabled_config, monkeypatch, _no_op_collectors):
        monkeypatch.setattr("plugins.uni_portal.portal.login", lambda: True)
        notify_calls = []
        monkeypatch.setattr("plugins.uni_portal.check._notify_user", lambda summary: notify_calls.append(summary))

        result = check.run_daily_check()

        assert result == {"ok": True, "changed": False, "error": None}
        assert notify_calls == []
        assert snap.load_snapshot()["captured_at"] is not None


class TestChangesDetected:
    def test_new_grade_triggers_notify_and_episodic_and_graph(self, _enabled_config, monkeypatch):
        monkeypatch.setattr("plugins.uni_portal.portal.login", lambda: True)
        monkeypatch.setattr(
            "plugins.uni_portal.portal.collect_grades", lambda: [{"course": "CS101", "grade": "AA"}]
        )
        monkeypatch.setattr("plugins.uni_portal.portal.collect_announcements", lambda: [])
        monkeypatch.setattr("plugins.uni_portal.portal.collect_schedule", lambda: [])

        notify_calls = []
        monkeypatch.setattr("plugins.uni_portal.check._notify_user", lambda summary: notify_calls.append(summary))
        episode_calls = []
        monkeypatch.setattr(
            "agent.memory.episodic.record_episode",
            lambda **kw: episode_calls.append(kw) or 1,
        )
        graph_calls = []
        monkeypatch.setattr(
            "agent.memory.graph_builder.record_from_memory_entry",
            lambda text, topic=None: graph_calls.append((text, topic)) or 1,
        )

        result = check.run_daily_check()

        assert result == {"ok": True, "changed": True, "error": None}
        assert len(notify_calls) == 1
        assert "CS101" in notify_calls[0]
        assert len(episode_calls) == 1
        assert episode_calls[0]["source"] == "uni_portal"
        assert len(graph_calls) == 1

    def test_second_run_with_same_grades_reports_no_change(self, _enabled_config, monkeypatch):
        monkeypatch.setattr("plugins.uni_portal.portal.login", lambda: True)
        monkeypatch.setattr(
            "plugins.uni_portal.portal.collect_grades", lambda: [{"course": "CS101", "grade": "AA"}]
        )
        monkeypatch.setattr("plugins.uni_portal.portal.collect_announcements", lambda: [])
        monkeypatch.setattr("plugins.uni_portal.portal.collect_schedule", lambda: [])
        monkeypatch.setattr("plugins.uni_portal.check._notify_user", lambda summary: None)
        monkeypatch.setattr("agent.memory.episodic.record_episode", lambda **kw: 1)
        monkeypatch.setattr("agent.memory.graph_builder.record_from_memory_entry", lambda text, topic=None: 1)

        first = check.run_daily_check()
        second = check.run_daily_check()

        assert first["changed"] is True
        assert second["changed"] is False


class TestNeverRaises:
    def test_exception_anywhere_in_the_flow_degrades_to_error_result(self, _enabled_config, monkeypatch):
        monkeypatch.setattr(
            "plugins.uni_portal.portal.login", lambda: (_ for _ in ()).throw(RuntimeError("browser crashed"))
        )
        result = check.run_daily_check()
        assert result["ok"] is False
        assert result["error"] is not None
