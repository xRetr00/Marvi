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


def _activity_lines(env):
    import json as _json

    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "subconscious" / "activity.jsonl"
    if not path.exists():
        return []
    return [_json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestSuggestGoalAutoCreate:
    """suggest_goal(action="add") creates the goal directly (origin=
    "inferred") instead of only registering a pending suggestion, subject
    to the concurrent-inferred-goal cap and title-similarity dedup.
    pause/done always stay consent-first suggestions."""

    def test_add_auto_creates_an_inferred_goal(self, env):
        out = json.loads(env._handle_suggest_goal({
            "action": "add",
            "title": "Learn Spanish",
            "detail": "daily practice",
            "dedup_key": "subconscious:goal:learn-spanish",
        }))

        assert out["ok"] is True
        assert out["auto_created"] is True
        assert out["goal"]["title"] == "Learn Spanish"
        assert out["goal"]["origin"] == "inferred"

        import agent.goal_store as gs
        stored = gs.list_goals()
        assert len(stored) == 1
        assert stored[0]["origin"] == "inferred"

        import cron.suggestions as sugg
        assert sugg.list_pending() == []  # never went through the suggestion path

    def test_add_logs_an_activity_entry(self, env):
        json.loads(env._handle_suggest_goal({
            "action": "add",
            "title": "Learn Spanish",
            "dedup_key": "subconscious:goal:learn-spanish",
        }))

        lines = _activity_lines(env)
        assert len(lines) == 1
        assert lines[0]["source"] == "goal"
        assert "Learn Spanish" in lines[0]["summary"]

    def test_add_falls_back_to_suggestion_when_a_similar_goal_already_exists(self, env):
        import agent.goal_store as gs

        gs.add_goal(title="Learn spanish!", origin="user")

        out = json.loads(env._handle_suggest_goal({
            "action": "add",
            "title": "Learn Spanish",
            "dedup_key": "subconscious:goal:learn-spanish",
        }))

        assert out["auto_created"] is False
        assert out["registered"] is True
        assert len(gs.list_goals()) == 1  # no duplicate created

        import cron.suggestions as sugg
        assert len(sugg.list_pending()) == 1

    def test_dedup_matches_regardless_of_existing_goal_status(self, env):
        import agent.goal_store as gs

        done = gs.add_goal(title="Learn Spanish", origin="user")
        gs.update_goal(done["id"], status="done")

        out = json.loads(env._handle_suggest_goal({
            "action": "add",
            "title": "learn spanish",
            "dedup_key": "subconscious:goal:learn-spanish",
        }))

        assert out["auto_created"] is False
        assert len(gs.list_goals()) == 1

    def test_add_falls_back_to_suggestion_when_inferred_cap_reached(self, env):
        import agent.goal_store as gs

        for i in range(3):  # default goals.max_inferred
            gs.add_goal(title=f"Inferred goal {i}", origin="inferred")

        out = json.loads(env._handle_suggest_goal({
            "action": "add",
            "title": "One more inferred goal",
            "dedup_key": "subconscious:goal:one-more",
        }))

        assert out["auto_created"] is False
        assert len(gs.list_goals()) == 3

        import cron.suggestions as sugg
        assert len(sugg.list_pending()) == 1

    def test_paused_inferred_goals_do_not_count_toward_the_cap(self, env):
        import agent.goal_store as gs

        for i in range(3):
            g = gs.add_goal(title=f"Inferred goal {i}", origin="inferred")
            gs.update_goal(g["id"], status="paused")

        out = json.loads(env._handle_suggest_goal({
            "action": "add",
            "title": "A fresh inferred goal",
            "dedup_key": "subconscious:goal:fresh",
        }))

        assert out["auto_created"] is True

    def test_user_created_inferred_looking_goals_do_not_block_cap_check(self, env):
        # The cap only counts origin="inferred" -- a user manually adding
        # three goals of their own must never block Marvi's own inference.
        import agent.goal_store as gs

        for i in range(5):
            gs.add_goal(title=f"My own goal {i}", origin="user")

        out = json.loads(env._handle_suggest_goal({
            "action": "add",
            "title": "Marvi's own inferred goal",
            "dedup_key": "subconscious:goal:marvi-own",
        }))

        assert out["auto_created"] is True

    def test_max_inferred_is_configurable(self, env, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.cfg_get",
            lambda cfg, *keys, default=None: 1 if keys == ("goals", "max_inferred") else default,
        )
        import agent.goal_store as gs

        gs.add_goal(title="Already inferred", origin="inferred")

        out = json.loads(env._handle_suggest_goal({
            "action": "add",
            "title": "Second inferred goal",
            "dedup_key": "subconscious:goal:second",
        }))

        assert out["auto_created"] is False

    def test_pause_action_always_stays_a_suggestion(self, env):
        import agent.goal_store as gs

        goal = gs.add_goal(title="Existing goal", origin="user")

        out = json.loads(env._handle_suggest_goal({
            "action": "pause",
            "goal_id": goal["id"],
            "title": "Existing goal",
            "dedup_key": "subconscious:goal:pause-existing",
        }))

        assert out.get("auto_created") is False
        assert out["registered"] is True
        # The goal itself was never mutated by suggest_goal directly.
        assert gs.get_goal(goal["id"])["status"] == "active"

        import cron.suggestions as sugg
        assert len(sugg.list_pending()) == 1

    def test_done_action_always_stays_a_suggestion(self, env):
        import agent.goal_store as gs

        goal = gs.add_goal(title="Existing goal", origin="user")

        out = json.loads(env._handle_suggest_goal({
            "action": "done",
            "goal_id": goal["id"],
            "title": "Existing goal",
            "dedup_key": "subconscious:goal:done-existing",
        }))

        assert out.get("auto_created") is False
        assert gs.get_goal(goal["id"])["status"] == "active"

        import cron.suggestions as sugg
        assert len(sugg.list_pending()) == 1

    def test_suggest_goal_requires_dedup_key(self, env):
        out = json.loads(env._handle_suggest_goal({"action": "add", "title": "x"}))
        assert out.get("ok") is not True
