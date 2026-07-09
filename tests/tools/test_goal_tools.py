"""Tests for tools/goal_tools.py — the goal_add/goal_update/goal_list tools
and the subconscious-gated suggest_automation tool.

Uses an isolated HERMES_HOME for the underlying goal_store/suggestions
storage so the real ~/.hermes files are never touched.
"""

import importlib
import json

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_constants
    importlib.reload(hermes_constants)
    import agent.goal_store as gs
    importlib.reload(gs)
    import cron.suggestions as sugg
    importlib.reload(sugg)
    import tools.goal_tools  # ensure registered
    importlib.reload(tools.goal_tools)
    return tools.goal_tools


class TestGoalTools:
    def test_goal_add_and_list(self, env):
        out = json.loads(env._handle_goal_add({"title": "Ship it", "detail": "by Friday"}))
        assert out["ok"] is True
        assert out["goal"]["title"] == "Ship it"

        listed = json.loads(env._handle_goal_list({}))
        assert listed["ok"] is True
        assert listed["count"] == 1

    def test_goal_add_requires_title(self, env):
        out = json.loads(env._handle_goal_add({}))
        assert out.get("error") or out.get("ok") is False

    def test_goal_update_by_id(self, env):
        added = json.loads(env._handle_goal_add({"title": "X"}))
        goal_id = added["goal"]["id"]
        out = json.loads(env._handle_goal_update({"goal_id": goal_id, "status": "done"}))
        assert out["ok"] is True
        assert out["goal"]["status"] == "done"

    def test_goal_update_unknown_ref(self, env):
        out = env._handle_goal_update({"goal_id": "nope", "status": "done"})
        assert "error" in out or json.loads(out).get("ok") is False

    def test_goal_update_requires_a_field(self, env):
        added = json.loads(env._handle_goal_add({"title": "X"}))
        out = env._handle_goal_update({"goal_id": added["goal"]["id"]})
        parsed = json.loads(out)
        assert parsed.get("ok") is not True

    def test_goal_list_filters(self, env):
        env._handle_goal_add({"title": "A"})
        added = json.loads(env._handle_goal_add({"title": "B"}))
        env._handle_goal_update({"goal_id": added["goal"]["id"], "status": "done"})

        out = json.loads(env._handle_goal_list({"status": "active"}))
        assert out["count"] == 1


class TestSuggestAutomationGate:
    def test_hidden_when_subconscious_disabled(self, env):
        assert env._subconscious_toolset_enabled() is False

    def test_suggest_automation_registers_pending_suggestion(self, env):
        out = json.loads(env._handle_suggest_automation({
            "title": "Weekly digest",
            "description": "summarize the week",
            "dedup_key": "subconscious:weekly-digest",
            "job_spec": {"prompt": "summarize", "schedule": "0 18 * * 5"},
        }))
        assert out["ok"] is True
        assert out["registered"] is True
        assert out["auto_created"] is False
        assert out["suggestion"]["source"] == "subconscious"

    def test_suggest_automation_auto_tier_creates_job(self, env, monkeypatch):
        from unittest.mock import patch

        monkeypatch.setattr(
            "cron.suggestions.get_tiers_config", lambda: {"digest": "auto"}
        )
        with patch("cron.jobs.create_job", lambda **k: {"id": "job42", "name": k.get("name")}):
            out = json.loads(env._handle_suggest_automation({
                "title": "Auto digest",
                "category": "digest",
                "dedup_key": "subconscious:auto-digest",
                "job_spec": {"prompt": "p", "schedule": "0 8 * * *"},
            }))
        assert out["ok"] is True
        assert out["auto_created"] is True
        assert out["job"]["id"] == "job42"

        # Record is latched as accepted — not pending anymore.
        import cron.suggestions as sugg
        assert sugg.list_pending() == []

    def test_suggest_automation_non_auto_category_stays_pending(self, env, monkeypatch):
        monkeypatch.setattr(
            "cron.suggestions.get_tiers_config", lambda: {"digest": "auto"}
        )
        out = json.loads(env._handle_suggest_automation({
            "title": "Other thing",
            "category": "not-approved",
            "dedup_key": "subconscious:other",
            "job_spec": {"prompt": "p", "schedule": "0 8 * * *"},
        }))
        assert out["auto_created"] is False
        import cron.suggestions as sugg
        assert len(sugg.list_pending()) == 1

    def test_suggest_automation_requires_dedup_key(self, env):
        out = env._handle_suggest_automation({
            "title": "x",
            "job_spec": {"prompt": "p", "schedule": "1h"},
        })
        parsed = json.loads(out)
        assert parsed.get("ok") is not True

    def test_suggest_automation_requires_job_spec(self, env):
        out = env._handle_suggest_automation({
            "title": "x",
            "dedup_key": "k",
        })
        parsed = json.loads(out)
        assert parsed.get("ok") is not True
