"""Tests for GET /api/memory/archived + POST /api/memory/restore/{id}
(Loop 3, memory-maturity spec §Loop 3).

Follows the ``TestClient`` + ``_isolate_hermes_home`` pattern used throughout
tests/hermes_cli/test_web_server_marvi.py and test_web_server_episodic.py.
Archive state is seeded via the real (per-test, tempdir-isolated)
tools.memory_tool store/archive rather than mocking it, so these tests also
exercise the store <-> endpoint wiring.
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


class TestMemoryArchivedEndpoint:
    def test_empty_archive_returns_empty_list(self, client):
        resp = client.get("/api/memory/archived")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["entries"] == []

    def test_returns_archived_entries(self, client):
        from tools.memory_tool import MemoryStore, archive_entry

        store = MemoryStore()
        store.add("memory", "Stale fact")
        store.load_from_disk()
        archive_entry(store, "memory", "Stale fact", reason="test")

        resp = client.get("/api/memory/archived")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["entries"]) == 1
        assert data["entries"][0]["text"] == "Stale fact"
        assert data["entries"][0]["target"] == "memory"

    def test_target_filter(self, client):
        from tools.memory_tool import MemoryStore, archive_entry

        store = MemoryStore()
        store.add("memory", "Memory fact")
        store.add("user", "User fact")
        store.load_from_disk()
        archive_entry(store, "memory", "Memory fact", reason="test")
        archive_entry(store, "user", "User fact", reason="test")

        resp = client.get("/api/memory/archived", params={"target": "user"})

        data = resp.json()
        assert len(data["entries"]) == 1
        assert data["entries"][0]["target"] == "user"

    def test_invalid_target_returns_400(self, client):
        resp = client.get("/api/memory/archived", params={"target": "bogus"})
        assert resp.status_code == 400

    def test_failure_returns_structured_500(self, client):
        with patch(
            "hermes_cli.web_server._read_memory_archived_sync",
            side_effect=RuntimeError("boom"),
        ):
            resp = client.get("/api/memory/archived")

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]


class TestMemoryRestoreEndpoint:
    def test_restore_round_trip(self, client):
        from tools.memory_tool import MemoryStore, archive_entry

        store = MemoryStore()
        store.add("memory", "Restorable fact")
        store.load_from_disk()
        record = archive_entry(store, "memory", "Restorable fact", reason="test")

        resp = client.post(f"/api/memory/restore/{record['id']}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["text"] == "Restorable fact"

        fresh_store = MemoryStore()
        fresh_store.load_from_disk()
        assert "Restorable fact" in fresh_store.memory_entries

        # Restored entry no longer shows up as archived.
        archived_resp = client.get("/api/memory/archived")
        assert archived_resp.json()["entries"] == []

    def test_restore_unknown_id_returns_400(self, client):
        resp = client.post("/api/memory/restore/memory:doesnotexist")
        assert resp.status_code == 400

    def test_failure_returns_structured_500(self, client):
        with patch(
            "hermes_cli.web_server._restore_memory_entry_sync",
            side_effect=RuntimeError("boom"),
        ):
            resp = client.post("/api/memory/restore/some-id")

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]
