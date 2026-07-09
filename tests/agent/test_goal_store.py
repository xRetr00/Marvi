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
