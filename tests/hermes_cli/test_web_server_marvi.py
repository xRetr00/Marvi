"""Tests for the Marvi subconscious/presence activation endpoints and the
"What Marvi knows" read-only memory viewer added to hermes_cli/web_server.py.

Follows the ``TestClient`` + ``_isolate_hermes_home`` pattern used throughout
tests/hermes_cli/test_web_server.py. The cron/presence layers are mocked at
the module boundary these endpoints import from (``cron.subconscious`` /
``hermes_cli.presence_cmd``) — these tests exercise routing, request/response
shape, and error handling, not the underlying cron/AW/watcher mechanics
(those belong to their owning workstreams' own test suites).
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


# ---------------------------------------------------------------------------
# /api/subconscious/*
# ---------------------------------------------------------------------------


class TestSubconsciousEndpoints:
    def test_enable_calls_cron_subconscious_enable_with_interval(self, client):
        fake_status = {
            "enabled": True,
            "interval": "30m",
            "idle_trigger_minutes": 15,
            "tiers": {},
            "job_id": "job-1",
            "job_state": "active",
            "last_run_at": None,
            "next_run_at": 123.0,
        }
        with patch("cron.subconscious.enable", return_value=fake_status) as mock_enable:
            resp = client.post("/api/subconscious/enable", json={"interval": "30m"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["interval"] == "30m"
        assert data["job_id"] == "job-1"
        mock_enable.assert_called_once_with("30m")

    def test_enable_without_interval_passes_none(self, client):
        with patch("cron.subconscious.enable", return_value={"enabled": True}) as mock_enable:
            resp = client.post("/api/subconscious/enable", json={})

        assert resp.status_code == 200
        mock_enable.assert_called_once_with(None)

    def test_enable_failure_returns_structured_500_not_a_stack(self, client):
        with patch("cron.subconscious.enable", side_effect=RuntimeError("boom")):
            resp = client.post("/api/subconscious/enable", json={})

        assert resp.status_code == 500
        body = resp.json()
        assert "detail" in body
        assert "boom" not in body["detail"]
        assert "Traceback" not in body["detail"]

    def test_disable_calls_cron_subconscious_disable(self, client):
        fake_status = {"enabled": False, "interval": "20m", "idle_trigger_minutes": 15,
                        "tiers": {}, "job_id": "job-1", "job_state": "paused",
                        "last_run_at": None, "next_run_at": None}
        with patch("cron.subconscious.disable", return_value=fake_status) as mock_disable:
            resp = client.post("/api/subconscious/disable")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["enabled"] is False
        mock_disable.assert_called_once_with()

    def test_disable_failure_returns_structured_500(self, client):
        with patch("cron.subconscious.disable", side_effect=RuntimeError("boom")):
            resp = client.post("/api/subconscious/disable")

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]

    def test_status_calls_cron_subconscious_status(self, client):
        fake_status = {"enabled": True, "interval": "20m", "idle_trigger_minutes": 15,
                        "tiers": {"email": "notify"}, "job_id": "job-1", "job_state": "active",
                        "last_run_at": 1.0, "next_run_at": 2.0}
        with patch("cron.subconscious.status", return_value=fake_status) as mock_status:
            resp = client.get("/api/subconscious/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["tiers"] == {"email": "notify"}
        mock_status.assert_called_once_with()

    def test_status_failure_returns_structured_500(self, client):
        with patch("cron.subconscious.status", side_effect=RuntimeError("boom")):
            resp = client.get("/api/subconscious/status")

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]


# ---------------------------------------------------------------------------
# /api/presence/*
# ---------------------------------------------------------------------------


class TestPresenceEndpoints:
    def test_setup_ok_when_job_created(self, client):
        fake_result = {
            "activitywatch_available": True,
            "watcher_ok": True,
            "watcher_message": "media watcher started (pid 123)",
            "job_ok": True,
            "job_message": "presence distiller job created (id=job-1, schedule=0 3 * * *)",
            "enabled": True,
        }
        with patch("hermes_cli.presence_cmd.setup_presence", return_value=fake_result) as mock_setup:
            resp = client.post("/api/presence/setup")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["job_ok"] is True
        assert data["activitywatch_available"] is True
        mock_setup.assert_called_once_with()

    def test_setup_not_ok_when_job_creation_fails(self, client):
        fake_result = {
            "activitywatch_available": False,
            "watcher_ok": False,
            "watcher_message": "media watcher is Windows-only (SMTC); skipped on this platform",
            "job_ok": False,
            "job_message": "failed to create presence distiller job: boom",
            "enabled": True,
        }
        with patch("hermes_cli.presence_cmd.setup_presence", return_value=fake_result):
            resp = client.post("/api/presence/setup")

        # HTTP-level success (the call itself didn't raise) but the
        # structured `ok` flag reflects the job-creation failure so the UI
        # can surface it.
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["job_ok"] is False

    def test_setup_failure_returns_structured_500(self, client):
        with patch("hermes_cli.presence_cmd.setup_presence", side_effect=RuntimeError("boom")):
            resp = client.post("/api/presence/setup")

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]

    def test_pause_reflects_underlying_ok(self, client):
        with patch("hermes_cli.presence_cmd.pause_presence",
                    return_value={"ok": True, "message": "media watcher stopped (pid 1)", "enabled": False}) as mock_pause:
            resp = client.post("/api/presence/pause")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["enabled"] is False
        mock_pause.assert_called_once_with()

    def test_pause_failure_returns_structured_500(self, client):
        with patch("hermes_cli.presence_cmd.pause_presence", side_effect=RuntimeError("boom")):
            resp = client.post("/api/presence/pause")

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]

    def test_resume_reflects_underlying_ok(self, client):
        with patch("hermes_cli.presence_cmd.resume_presence",
                    return_value={"ok": False, "message": "failed to start media watcher: boom", "enabled": True}) as mock_resume:
            resp = client.post("/api/presence/resume")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        mock_resume.assert_called_once_with()

    def test_resume_failure_returns_structured_500(self, client):
        with patch("hermes_cli.presence_cmd.resume_presence", side_effect=RuntimeError("boom")):
            resp = client.post("/api/presence/resume")

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]

    def test_status_calls_get_presence_status(self, client):
        fake_status = {
            "config": {"enabled": True, "flow_gating": True, "goblin": {}, "denylist": []},
            "activitywatch_reachable": True,
            "is_windows": True,
            "watcher_pid": 123,
            "distill_job": {"id": "job-1", "schedule_display": "0 3 * * *", "enabled": True},
        }
        with patch("hermes_cli.presence_cmd.get_presence_status", return_value=fake_status) as mock_status:
            resp = client.get("/api/presence/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["watcher_pid"] == 123
        assert data["distill_job"]["id"] == "job-1"
        mock_status.assert_called_once_with()

    def test_status_failure_returns_structured_500(self, client):
        with patch("hermes_cli.presence_cmd.get_presence_status", side_effect=RuntimeError("boom")):
            resp = client.get("/api/presence/status")

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]


# ---------------------------------------------------------------------------
# /api/marvi/knowledge
# ---------------------------------------------------------------------------


class TestMarviKnowledgeEndpoint:
    def test_empty_when_no_memory_files(self, client):
        resp = client.get("/api/marvi/knowledge")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["entries"] == []
        assert "note" in data and data["note"]

    def test_reads_entries_from_both_stores(self, client):
        from hermes_constants import get_hermes_home

        mem_dir = get_hermes_home() / "memories"
        mem_dir.mkdir(parents=True, exist_ok=True)
        (mem_dir / "USER.md").write_text("Prefers dark mode\n§\nWorks late nights", encoding="utf-8")
        (mem_dir / "MEMORY.md").write_text("Project uses pytest", encoding="utf-8")

        resp = client.get("/api/marvi/knowledge")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        texts = {e["text"] for e in data["entries"]}
        assert texts == {"Prefers dark mode", "Works late nights", "Project uses pytest"}

        by_text = {e["text"]: e for e in data["entries"]}
        assert by_text["Prefers dark mode"]["source"] == "presence"
        assert by_text["Works late nights"]["source"] == "presence"
        assert by_text["Project uses pytest"]["source"] == "subconscious"
        for entry in data["entries"]:
            assert entry["id"]
            assert entry["timestamp"]

    def test_within_file_newest_appended_first(self, client):
        from hermes_constants import get_hermes_home

        mem_dir = get_hermes_home() / "memories"
        mem_dir.mkdir(parents=True, exist_ok=True)
        (mem_dir / "USER.md").write_text("first entry\n§\nsecond entry\n§\nthird entry", encoding="utf-8")

        resp = client.get("/api/marvi/knowledge")
        data = resp.json()
        texts_in_order = [e["text"] for e in data["entries"]]

        assert texts_in_order == ["third entry", "second entry", "first entry"]

    def test_caps_at_100_entries(self, client):
        from hermes_constants import get_hermes_home
        from tools.memory_tool import ENTRY_DELIMITER

        mem_dir = get_hermes_home() / "memories"
        mem_dir.mkdir(parents=True, exist_ok=True)
        entries = [f"entry {i}" for i in range(150)]
        (mem_dir / "MEMORY.md").write_text(ENTRY_DELIMITER.join(entries), encoding="utf-8")

        resp = client.get("/api/marvi/knowledge")
        data = resp.json()

        assert len(data["entries"]) == 100

    def test_failure_returns_structured_500(self, client):
        with patch("hermes_cli.web_server._read_marvi_knowledge_entries", side_effect=RuntimeError("boom")):
            resp = client.get("/api/marvi/knowledge")

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]
