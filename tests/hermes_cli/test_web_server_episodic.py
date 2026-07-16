"""Tests for GET /api/memory/episodes (Loop 1, memory-maturity spec §1.5).

Follows the ``TestClient`` + ``_isolate_hermes_home`` pattern used throughout
tests/hermes_cli/test_web_server_marvi.py. Episodes are seeded via the real
(per-test, tempdir-isolated) episodic store rather than mocking the store
itself, so these tests also exercise the store <-> endpoint wiring.
"""

from unittest.mock import patch

import pytest


@pytest.fixture
def client(monkeypatch, _isolate_hermes_home):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    import hermes_state
    from hermes_constants import get_hermes_home
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")

    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


class TestMemoryEpisodesEndpoint:
    def test_empty_store_returns_note(self, client):
        resp = client.get("/api/memory/episodes")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["episodes"] == []
        assert "note" in data and data["note"]

    def test_returns_recorded_episodes(self, client):
        from agent.memory.episodic import record_episode

        record_episode(kind="task", title="Fixed the build", summary="Green now.", source="s", ref="1")
        record_episode(kind="room", title="Lights dimmed", source="s", ref="2")

        resp = client.get("/api/memory/episodes")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert len(data["episodes"]) == 2
        titles = {ep["title"] for ep in data["episodes"]}
        assert titles == {"Fixed the build", "Lights dimmed"}
        for ep in data["episodes"]:
            assert set(ep) == {"id", "ts", "kind", "actor", "title", "summary", "source"}

    def test_kind_filter(self, client):
        from agent.memory.episodic import record_episode

        record_episode(kind="task", title="a task", source="s", ref="1")
        record_episode(kind="room", title="a room event", source="s", ref="2")

        resp = client.get("/api/memory/episodes", params={"kind": "room"})

        data = resp.json()
        assert len(data["episodes"]) == 1
        assert data["episodes"][0]["kind"] == "room"

    def test_invalid_kind_returns_400(self, client):
        resp = client.get("/api/memory/episodes", params={"kind": "not-a-kind"})
        assert resp.status_code == 400

    def test_query_filter(self, client):
        from agent.memory.episodic import record_episode

        record_episode(kind="task", title="Deploy the frontend", source="s", ref="1")
        record_episode(kind="task", title="Buy groceries", source="s", ref="2")

        resp = client.get("/api/memory/episodes", params={"q": "frontend"})

        data = resp.json()
        assert len(data["episodes"]) == 1
        assert data["episodes"][0]["title"] == "Deploy the frontend"

    def test_since_filter(self, client):
        from agent.memory.episodic import record_episode

        record_episode(kind="task", title="old", source="s", ref="1", ts="2026-01-01T00:00:00+00:00")
        record_episode(kind="task", title="new", source="s", ref="2", ts="2026-12-01T00:00:00+00:00")

        resp = client.get("/api/memory/episodes", params={"since": "2026-06-01T00:00:00+00:00"})

        data = resp.json()
        assert [ep["title"] for ep in data["episodes"]] == ["new"]

    def test_limit_param_is_honored(self, client):
        from agent.memory.episodic import record_episode

        for i in range(5):
            record_episode(kind="task", title=f"item-{i}", source="s", ref=str(i))

        resp = client.get("/api/memory/episodes", params={"limit": 2})

        data = resp.json()
        assert len(data["episodes"]) == 2

    def test_failure_returns_structured_500(self, client):
        with patch(
            "hermes_cli.web_server._read_memory_episodes_sync",
            side_effect=RuntimeError("boom"),
        ):
            resp = client.get("/api/memory/episodes")

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]
