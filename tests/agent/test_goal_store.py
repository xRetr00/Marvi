"""Tests for the standing goal store (agent/goal_store.py).

Covers CRUD, validation, and the system-prompt rendering helper. Uses an
isolated HERMES_HOME so the real ~/.hermes/goals.json is never touched.
"""

import importlib

import pytest


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An agent.goal_store module bound to an isolated HERMES_HOME."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_constants
    importlib.reload(hermes_constants)
    import agent.goal_store as gs
    importlib.reload(gs)
    return gs


class TestCRUD:
    def test_add_and_list(self, store):
        goal = store.add_goal(title="Ship Q3 report", detail="draft by Friday")
        assert goal["title"] == "Ship Q3 report"
        assert goal["detail"] == "draft by Friday"
        assert goal["status"] == "active"
        assert goal["horizon"] == "short"
        assert "id" in goal and goal["id"]
        assert goal["created"] == goal["updated"]

        goals = store.list_goals()
        assert len(goals) == 1
        assert goals[0]["id"] == goal["id"]

    def test_add_requires_title(self, store):
        with pytest.raises(ValueError):
            store.add_goal(title="")

    def test_add_rejects_invalid_status_or_horizon(self, store):
        with pytest.raises(ValueError):
            store.add_goal(title="x", status="bogus")
        with pytest.raises(ValueError):
            store.add_goal(title="x", horizon="bogus")

    def test_update_by_id(self, store):
        goal = store.add_goal(title="Learn Spanish", horizon="long")
        updated = store.update_goal(goal["id"], status="paused", detail="on hold")
        assert updated["status"] == "paused"
        assert updated["detail"] == "on hold"
        assert updated["updated"] != goal["updated"] or updated["updated"] >= goal["created"]

    def test_update_by_index_and_title(self, store):
        store.add_goal(title="First")
        store.add_goal(title="Second")
        by_index = store.update_goal("2", status="done")
        assert by_index["title"] == "Second"
        assert by_index["status"] == "done"

        by_title = store.update_goal("first", detail="d")
        assert by_title["detail"] == "d"

    def test_update_unknown_ref_returns_none(self, store):
        assert store.update_goal("nope", status="done") is None

    def test_update_rejects_invalid_status(self, store):
        goal = store.add_goal(title="x")
        with pytest.raises(ValueError):
            store.update_goal(goal["id"], status="bogus")

    def test_remove_goal(self, store):
        goal = store.add_goal(title="x")
        assert store.remove_goal(goal["id"]) is True
        assert store.list_goals() == []
        assert store.remove_goal(goal["id"]) is False

    def test_get_goal_resolution(self, store):
        goal = store.add_goal(title="Findable")
        assert store.get_goal(goal["id"])["id"] == goal["id"]
        assert store.get_goal("1")["id"] == goal["id"]
        assert store.get_goal("findable")["id"] == goal["id"]
        assert store.get_goal("nope") is None

    def test_list_filters(self, store):
        store.add_goal(title="Active short", status="active", horizon="short")
        store.add_goal(title="Paused long", status="paused", horizon="long")
        assert len(store.list_goals(status="active")) == 1
        assert len(store.list_goals(horizon="long")) == 1
        assert len(store.active_goals()) == 1

    def test_persists_across_reload(self, store, tmp_path, monkeypatch):
        store.add_goal(title="Persisted")
        import importlib
        import agent.goal_store as gs2
        importlib.reload(gs2)
        assert len(gs2.list_goals()) == 1

    def test_file_permissions(self, store):
        import os
        import sys

        store.add_goal(title="x")
        if sys.platform != "win32":
            mode = os.stat(store.GOALS_FILE).st_mode & 0o777
            assert mode == 0o600

    def test_profile_switch_resolves_goal_path_at_call_time(self, store, tmp_path, monkeypatch):
        other = tmp_path / "other-profile"
        other.mkdir()
        monkeypatch.setattr(store, "get_hermes_home", lambda: other)
        store.add_goal(title="Other profile goal")
        assert (other / "goals.json").exists()
        assert store.list_goals()[0]["title"] == "Other profile goal"


class TestOrigin:
    """origin ("user"/"inferred") -- see tools/goal_tools.py's auto-goal
    inference path for the writer side; this covers the store contract:
    default value, validation, backward-compat with pre-existing records,
    and the "Keep" flip via update_goal."""

    def test_default_origin_is_user(self, store):
        goal = store.add_goal(title="x")
        assert goal["origin"] == "user"

    def test_add_goal_accepts_inferred_origin(self, store):
        goal = store.add_goal(title="x", origin="inferred")
        assert goal["origin"] == "inferred"
        assert store.list_goals()[0]["origin"] == "inferred"

    def test_add_goal_rejects_invalid_origin(self, store):
        with pytest.raises(ValueError):
            store.add_goal(title="x", origin="bogus")

    def test_old_record_without_origin_reads_as_user(self, store):
        # Simulate a goal written before the origin field existed by writing
        # the raw file directly, bypassing add_goal.
        import json

        store.GOALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        store.GOALS_FILE.write_text(
            json.dumps({"goals": [{
                "id": "legacy1",
                "title": "Pre-existing goal",
                "detail": "",
                "status": "active",
                "horizon": "short",
                "created": "2026-01-01T00:00:00+00:00",
                "updated": "2026-01-01T00:00:00+00:00",
            }]}),
            encoding="utf-8",
        )

        goals = store.load_goals()
        assert len(goals) == 1
        assert goals[0]["origin"] == "user"

    def test_old_record_origin_backfill_does_not_touch_other_fields(self, store):
        import json

        store.GOALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        store.GOALS_FILE.write_text(
            json.dumps({"goals": [{
                "id": "legacy1", "title": "Pre-existing goal", "detail": "d",
                "status": "paused", "horizon": "long",
                "created": "2026-01-01T00:00:00+00:00", "updated": "2026-01-01T00:00:00+00:00",
            }]}),
            encoding="utf-8",
        )
        goal = store.load_goals()[0]
        assert goal["title"] == "Pre-existing goal"
        assert goal["status"] == "paused"
        assert goal["horizon"] == "long"

    def test_update_goal_can_flip_origin_to_user(self, store):
        goal = store.add_goal(title="x", origin="inferred")
        updated = store.update_goal(goal["id"], origin="user")
        assert updated["origin"] == "user"
        assert store.list_goals()[0]["origin"] == "user"

    def test_update_goal_rejects_invalid_origin(self, store):
        goal = store.add_goal(title="x")
        with pytest.raises(ValueError):
            store.update_goal(goal["id"], origin="bogus")


class TestPromptRendering:
    def test_empty_when_no_active_goals(self, store):
        assert store.format_active_goals_for_prompt() == ""

    def test_only_active_goals_rendered(self, store):
        store.add_goal(title="Active one", detail="do it")
        done = store.add_goal(title="Done one")
        store.update_goal(done["id"], status="done")

        block = store.format_active_goals_for_prompt()
        assert "Active one" in block
        assert "do it" in block
        assert "Done one" not in block

    def test_caps_to_max_goals(self, store):
        for i in range(15):
            store.add_goal(title=f"Goal {i}")
        block = store.format_active_goals_for_prompt(max_goals=3)
        # Header line + 3 goal lines.
        assert len(block.splitlines()) == 4

    def test_inferred_goal_marked_in_prompt(self, store):
        store.add_goal(title="User goal", origin="user")
        store.add_goal(title="Auto goal", origin="inferred")

        block = store.format_active_goals_for_prompt()
        lines = {line for line in block.splitlines()}
        assert any("User goal" in line and "(inferred)" not in line for line in lines)
        assert any("Auto goal" in line and "(inferred)" in line for line in lines)
