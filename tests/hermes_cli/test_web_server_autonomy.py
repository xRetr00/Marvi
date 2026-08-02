"""Tests for the autonomy endpoints (Marvi freedom spec §1.5) added to
``hermes_cli/web_server.py``: GET /api/autonomy/status, POST
/api/autonomy/config. Exercised via the sync helper functions directly
(``_autonomy_status_sync``, ``_autonomy_config_update_sync``) — same
functions ``run_in_threadpool`` calls from the async route handlers — so
these run fast without spinning up a full FastAPI TestClient for a change
this narrowly scoped. ``tests/hermes_cli/test_web_server.py`` already covers
the app/TestClient plumbing itself.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate(_isolate_hermes_home):
    return None


class TestAutonomyStatusSync:
    def test_reflects_budget_and_pending_questions(self, monkeypatch):
        import hermes_cli.web_server as web_server

        monkeypatch.setattr(
            "agent.autonomy.budget.remaining",
            lambda: {
                "date": "2026-07-24",
                "enabled": True,
                "daily_action_budget": 8,
                "used_total": 1,
                "remaining_total": 7,
                "categories": {"research": {"limit": 4, "used": 1, "remaining": 3}},
            },
        )
        monkeypatch.setattr(
            "agent.autonomy.ask.list_pending_questions",
            lambda: [{"id": "q1", "question": "Shift your brief?", "status": "pending"}],
        )
        monkeypatch.setattr(
            web_server,
            "_read_subconscious_activity_sync",
            lambda limit: {
                "runs": [
                    {"source": "autonomy", "outcome": "message", "summary": "did research"},
                    {"source": "tick", "outcome": "no_change", "summary": "quiet"},
                ]
            },
        )

        result = web_server._autonomy_status_sync()

        assert result["enabled"] is True
        assert result["budget"]["used_total"] == 1
        assert len(result["pending_questions"]) == 1
        # Only "autonomy"-sourced activity rows are surfaced, not the whole feed.
        assert len(result["recent_actions"]) == 1
        assert result["recent_actions"][0]["source"] == "autonomy"

    def test_never_raises_when_activity_log_read_fails(self, monkeypatch):
        import hermes_cli.web_server as web_server

        monkeypatch.setattr("agent.autonomy.budget.remaining", lambda: {"enabled": True})
        monkeypatch.setattr("agent.autonomy.ask.list_pending_questions", lambda: [])

        def _boom(limit):
            raise RuntimeError("disk error")

        monkeypatch.setattr(web_server, "_read_subconscious_activity_sync", _boom)

        result = web_server._autonomy_status_sync()
        assert result["recent_actions"] == []


class TestAutonomyAnswer:
    def test_answer_question_persists_exact_pending_id(self, monkeypatch):
        from agent.autonomy import ask

        monkeypatch.setattr(ask, "get_hermes_home", lambda: __import__("pathlib").Path(
            __import__("os").environ["HERMES_HOME"]
        ))
        state = {"questions": [{"id": "q1", "status": "pending", "question": "Choose?"}]}
        ask._save_pending(state)
        result = ask.answer_question("q1", "Option A")
        assert result["answer_text"] == "Option A"
        assert ask.list_pending_questions()[0]["status"] == "answered"


class TestAutonomyConfigUpdateSync:
    def test_updates_enabled_and_per_category_and_ask_settings(self, monkeypatch):
        import hermes_cli.web_server as web_server

        saved = {}
        monkeypatch.setattr(web_server, "read_raw_config", lambda: {})
        monkeypatch.setattr(web_server, "save_config", lambda cfg: saved.update(cfg))

        body = web_server.AutonomyConfigUpdate(
            enabled=False,
            daily_action_budget=10,
            per_category={"research": 6},
            ask_max_per_day=1,
            ask_quiet_in_deep_work=False,
        )

        result = web_server._autonomy_config_update_sync(body)

        assert saved["autonomy"]["enabled"] is False
        assert saved["autonomy"]["daily_action_budget"] == 10
        assert saved["autonomy"]["per_category"]["research"] == 6
        assert saved["autonomy"]["ask"] == {"max_per_day": 1, "quiet_in_deep_work": False}
        # Return value re-reads through autonomy_config for a normalized shape.
        assert result["enabled"] is False

    def test_partial_update_preserves_existing_section(self, monkeypatch):
        import hermes_cli.web_server as web_server

        existing = {"autonomy": {"enabled": True, "per_category": {"research": 4, "browse": 2, "ask_user": 3}}}
        saved = {}
        monkeypatch.setattr(web_server, "read_raw_config", lambda: dict(existing))
        monkeypatch.setattr(web_server, "save_config", lambda cfg: saved.update(cfg))

        body = web_server.AutonomyConfigUpdate(ask_max_per_day=5)
        web_server._autonomy_config_update_sync(body)

        # Untouched fields survive the partial update.
        assert saved["autonomy"]["enabled"] is True
        assert saved["autonomy"]["per_category"]["research"] == 4
        assert saved["autonomy"]["ask"]["max_per_day"] == 5

    def test_negative_per_category_values_are_clamped_to_zero(self, monkeypatch):
        import hermes_cli.web_server as web_server

        monkeypatch.setattr(web_server, "read_raw_config", lambda: {})
        saved = {}
        monkeypatch.setattr(web_server, "save_config", lambda cfg: saved.update(cfg))

        body = web_server.AutonomyConfigUpdate(per_category={"research": -5}, daily_action_budget=-1)
        # Must not raise; negative input clamps to 0 rather than corrupting
        # the stored budget.
        web_server._autonomy_config_update_sync(body)
        assert saved["autonomy"]["per_category"]["research"] == 0
        assert saved["autonomy"]["daily_action_budget"] == 0

    def test_pydantic_rejects_non_integer_category_values_at_the_boundary(self):
        """The request-parsing layer (pydantic) is the first line of
        defense for malformed input -- confirms bad types never reach
        _autonomy_config_update_sync in the first place."""
        import hermes_cli.web_server as web_server
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            web_server.AutonomyConfigUpdate(per_category={"research": "not-a-number"})
