"""Tests for the Marvi subconscious/presence activation endpoints and the
"What Marvi knows" read-only memory viewer added to hermes_cli/web_server.py.

Follows the ``TestClient`` + ``_isolate_hermes_home`` pattern used throughout
tests/hermes_cli/test_web_server.py. The cron/presence layers are mocked at
the module boundary these endpoints import from (``cron.subconscious`` /
``hermes_cli.presence_cmd``) — these tests exercise routing, request/response
shape, and error handling, not the underlying cron/AW/watcher mechanics
(those belong to their owning workstreams' own test suites).
"""

import json
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
        with patch(
            "cron.subconscious.enable", return_value={"enabled": True}
        ) as mock_enable:
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
        fake_status = {
            "enabled": False,
            "interval": "20m",
            "idle_trigger_minutes": 15,
            "tiers": {},
            "job_id": "job-1",
            "job_state": "paused",
            "last_run_at": None,
            "next_run_at": None,
        }
        with patch(
            "cron.subconscious.disable", return_value=fake_status
        ) as mock_disable:
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


class TestComposioEndpoints:
    def test_setup_stores_secret_and_enables_connect_mcp(self, client):
        from hermes_cli.config import get_env_value, load_config, read_raw_config

        response = client.post(
            "/api/composio/setup",
            json={"api_key": "sdk-key", "consumer_api_key": "mcp-key"},
        )
        assert response.status_code == 200
        config = load_config()
        assert get_env_value("COMPOSIO_API_KEY") == "sdk-key"
        assert get_env_value("COMPOSIO_CONSUMER_API_KEY") == "mcp-key"
        assert "api_key" not in (config.get("composio") or {})
        assert read_raw_config()["mcp_servers"]["composio"]["headers"] == {
            "x-consumer-api-key": "${COMPOSIO_CONSUMER_API_KEY}"
        }

    def test_status_migrates_legacy_plaintext_key(self, client):
        from hermes_cli.config import get_env_value, load_config, save_config

        save_config({"composio": {"api_key": "legacy", "surfaces": ["gmail"]}})
        response = client.get("/api/composio/status")
        assert response.status_code == 200
        config = load_config()
        assert get_env_value("COMPOSIO_API_KEY") == "legacy"
        assert "api_key" not in config["composio"]
        assert response.json()["sdk_configured"] is True
        assert response.json()["mcp_enabled"] is False
        assert response.json()["legacy_key_present"] is False

    def test_snapshot_surfaces_are_validated_and_saved(self, client):
        response = client.put(
            "/api/composio/snapshots", json={"surfaces": ["gmail", "slack"]}
        )
        assert response.status_code == 200
        assert response.json()["snapshot_surfaces"] == ["gmail", "slack"]

        rejected = client.put("/api/composio/snapshots", json={"surfaces": ["notion"]})
        assert rejected.status_code == 400

    def test_connect_returns_direct_sdk_authorization(self, client):
        expected = {"connected": False, "redirect_url": "https://auth.example"}
        with patch("hermes_cli.web_server._composio_connect_sync", return_value=expected):
            response = client.post("/api/composio/connect", json={"toolkit": "gmail"})
        assert response.status_code == 200
        assert response.json()["redirect_url"] == "https://auth.example"

    def test_toolkit_catalog_response_is_forwarded(self, client):
        fake = {
            "toolkits": [
                {"slug": "gmail", "name": "Gmail", "description": "", "categories": []}
            ],
            "total": 1,
        }
        with patch("hermes_cli.web_server._composio_toolkits_sync", return_value=fake):
            response = client.get("/api/composio/toolkits?search=gmail")
        assert response.status_code == 200
        assert response.json()["toolkits"][0]["slug"] == "gmail"


class TestSubconsciousStatusEndpoints:
    def test_status_calls_cron_subconscious_status(self, client):
        fake_status = {
            "enabled": True,
            "interval": "20m",
            "idle_trigger_minutes": 15,
            "tiers": {"email": "notify"},
            "job_id": "job-1",
            "job_state": "active",
            "last_run_at": 1.0,
            "next_run_at": 2.0,
        }
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
        with patch(
            "hermes_cli.presence_cmd.setup_presence", return_value=fake_result
        ) as mock_setup:
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
        with patch(
            "hermes_cli.presence_cmd.setup_presence", side_effect=RuntimeError("boom")
        ):
            resp = client.post("/api/presence/setup")

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]

    def test_pause_reflects_underlying_ok(self, client):
        with patch(
            "hermes_cli.presence_cmd.pause_presence",
            return_value={
                "ok": True,
                "message": "media watcher stopped (pid 1)",
                "enabled": False,
            },
        ) as mock_pause:
            resp = client.post("/api/presence/pause")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["enabled"] is False
        mock_pause.assert_called_once_with()

    def test_pause_failure_returns_structured_500(self, client):
        with patch(
            "hermes_cli.presence_cmd.pause_presence", side_effect=RuntimeError("boom")
        ):
            resp = client.post("/api/presence/pause")

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]

    def test_resume_reflects_underlying_ok(self, client):
        with patch(
            "hermes_cli.presence_cmd.resume_presence",
            return_value={
                "ok": False,
                "message": "failed to start media watcher: boom",
                "enabled": True,
            },
        ) as mock_resume:
            resp = client.post("/api/presence/resume")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        mock_resume.assert_called_once_with()

    def test_resume_failure_returns_structured_500(self, client):
        with patch(
            "hermes_cli.presence_cmd.resume_presence", side_effect=RuntimeError("boom")
        ):
            resp = client.post("/api/presence/resume")

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]

    def test_status_calls_get_presence_status(self, client):
        fake_status = {
            "config": {
                "enabled": True,
                "flow_gating": True,
                "goblin": {},
                "denylist": [],
            },
            "activitywatch_reachable": True,
            "is_windows": True,
            "watcher_pid": 123,
            "distill_job": {
                "id": "job-1",
                "schedule_display": "0 3 * * *",
                "enabled": True,
            },
        }
        with patch(
            "hermes_cli.presence_cmd.get_presence_status", return_value=fake_status
        ) as mock_status:
            resp = client.get("/api/presence/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["watcher_pid"] == 123
        assert data["distill_job"]["id"] == "job-1"
        mock_status.assert_called_once_with()

    def test_status_failure_returns_structured_500(self, client):
        with patch(
            "hermes_cli.presence_cmd.get_presence_status",
            side_effect=RuntimeError("boom"),
        ):
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
        (mem_dir / "USER.md").write_text(
            "Prefers dark mode\n§\nWorks late nights", encoding="utf-8"
        )
        (mem_dir / "MEMORY.md").write_text("Project uses pytest", encoding="utf-8")

        resp = client.get("/api/marvi/knowledge")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        texts = {e["text"] for e in data["entries"]}
        assert texts == {
            "Prefers dark mode",
            "Works late nights",
            "Project uses pytest",
        }

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
        (mem_dir / "USER.md").write_text(
            "first entry\n§\nsecond entry\n§\nthird entry", encoding="utf-8"
        )

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
        (mem_dir / "MEMORY.md").write_text(
            ENTRY_DELIMITER.join(entries), encoding="utf-8"
        )

        resp = client.get("/api/marvi/knowledge")
        data = resp.json()

        assert len(data["entries"]) == 100

    def test_failure_returns_structured_500(self, client):
        with patch(
            "hermes_cli.web_server._read_marvi_knowledge_entries",
            side_effect=RuntimeError("boom"),
        ):
            resp = client.get("/api/marvi/knowledge")

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]


# ---------------------------------------------------------------------------
# /api/subconscious/activity, /surfaces, /suggestions — the tick visibility
# surface (see cron/scheduler.py's activity-log hooks and
# cron/scripts/subconscious/snapshot_store.py).
# ---------------------------------------------------------------------------


class TestSubconsciousActivityEndpoint:
    def _activity_path(self):
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "subconscious" / "activity.jsonl"

    def test_empty_when_no_log_and_no_last_run(self, client):
        with patch("cron.subconscious.status", return_value={"last_run_at": None}):
            resp = client.get("/api/subconscious/activity")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["runs"] == []
        assert "note" in data and data["note"]

    def test_falls_back_to_cron_store_last_run_when_no_log(self, client):
        with patch(
            "cron.subconscious.status",
            return_value={"last_run_at": "2026-07-13T12:00:00+00:00"},
        ):
            resp = client.get("/api/subconscious/activity")

        data = resp.json()
        assert data["ok"] is True
        assert len(data["runs"]) == 1
        assert data["runs"][0]["at"] == "2026-07-13T12:00:00+00:00"
        assert data["runs"][0]["outcome"] is None
        assert "note" in data and "activity log" in data["note"]

    def test_reads_jsonl_newest_first(self, client):
        path = self._activity_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            {
                "at": "2026-07-13T10:00:00",
                "job_id": "j1",
                "outcome": "no_change",
                "summary": None,
            },
            {
                "at": "2026-07-13T10:20:00",
                "job_id": "j1",
                "outcome": "message",
                "summary": "Told you about X",
            },
            {
                "at": "2026-07-13T10:40:00",
                "job_id": "j1",
                "outcome": "error",
                "summary": "boom",
            },
        ]
        path.write_text(
            "\n".join(json.dumps(r) for r in lines) + "\n", encoding="utf-8"
        )

        resp = client.get("/api/subconscious/activity")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "note" not in data or not data["note"]
        outcomes = [r["outcome"] for r in data["runs"]]
        assert outcomes == ["error", "message", "no_change"]
        assert data["runs"][0]["summary"] == "boom"

    def test_exposes_source_diff_thought_and_output_path(self, client):
        """The activity feed's whole point is showing the thinking, not just
        the outcome — verify the richer fields make it through untouched."""
        path = self._activity_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "at": "2026-07-13T10:00:00",
            "source": "distiller",
            "job_id": "job-distiller",
            "outcome": "message",
            "summary": "Learned about your workspace",
            "diff": "App usage since last check:\n  - vscode: 2h",
            "thought": "Noted: you spent 2h in vscode on hermes-agent today.",
            "output_path": "/home/user/.hermes/cron/output/job-distiller/2026-07-13_10-00-00.md",
        }
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        resp = client.get("/api/subconscious/activity")

        data = resp.json()
        run = data["runs"][0]
        assert run["source"] == "distiller"
        assert run["diff"] == "App usage since last check:\n  - vscode: 2h"
        assert run["thought"] == "Noted: you spent 2h in vscode on hermes-agent today."
        assert (
            run["output_path"]
            == "/home/user/.hermes/cron/output/job-distiller/2026-07-13_10-00-00.md"
        )

    def test_defaults_source_to_tick_for_legacy_lines_without_it(self, client):
        """A line written before the `source` field existed should still
        read back sensibly rather than surfacing null."""
        path = self._activity_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "at": "2026-07-13T10:00:00",
                "job_id": "j1",
                "outcome": "no_change",
            })
            + "\n",
            encoding="utf-8",
        )

        resp = client.get("/api/subconscious/activity")

        assert resp.json()["runs"][0]["source"] == "tick"

    def test_respects_limit_param(self, client):
        path = self._activity_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            {"at": f"2026-07-13T10:0{i}:00", "job_id": "j1", "outcome": "no_change"}
            for i in range(5)
        ]
        path.write_text(
            "\n".join(json.dumps(r) for r in lines) + "\n", encoding="utf-8"
        )

        resp = client.get("/api/subconscious/activity?limit=2")

        assert resp.status_code == 200
        assert len(resp.json()["runs"]) == 2

    def test_skips_malformed_lines(self, client):
        path = self._activity_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "not json at all\n"
            + json.dumps({
                "at": "2026-07-13T10:00:00",
                "job_id": "j1",
                "outcome": "no_change",
            })
            + "\n",
            encoding="utf-8",
        )

        resp = client.get("/api/subconscious/activity")

        data = resp.json()
        assert len(data["runs"]) == 1
        assert data["runs"][0]["outcome"] == "no_change"

    def test_failure_returns_structured_500(self, client):
        with patch(
            "hermes_cli.web_server._read_subconscious_activity_sync",
            side_effect=RuntimeError("boom"),
        ):
            resp = client.get("/api/subconscious/activity")

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]


class TestSubconsciousSurfacesEndpoint:
    def test_no_surfaces_configured(self, client):
        resp = client.get("/api/subconscious/surfaces")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["surfaces"] == []

    def test_reads_healthy_surface(self, client):
        from hermes_cli.config import load_config, save_config
        from cron.scripts.subconscious.snapshot_store import open_store

        config = load_config()
        config.setdefault("composio", {})["surfaces"] = ["gmail"]
        save_config(config)

        store = open_store("gmail")
        store.mark_attempt()
        store.record_success(changed=False)
        store.save()

        resp = client.get("/api/subconscious/surfaces")

        assert resp.status_code == 200
        data = resp.json()
        surface = data["surfaces"][0]
        assert surface["surface"] == "gmail"
        assert surface["status"] == "ok"
        assert surface["consecutive_failures"] == 0
        assert surface["quiet_streak"] == 1
        assert surface["last_error"] is None

    def test_reports_backing_off_surface(self, client):
        from hermes_cli.config import load_config, save_config
        from cron.scripts.subconscious.snapshot_store import open_store

        config = load_config()
        config.setdefault("composio", {})["surfaces"] = ["github"]
        save_config(config)

        store = open_store("github")
        store.record_failure(
            "401 Unauthorized: token expired for real this time round the loop"
        )
        store.save()

        resp = client.get("/api/subconscious/surfaces")

        data = resp.json()
        surface = data["surfaces"][0]
        assert surface["surface"] == "github"
        assert surface["status"] == "backing-off"
        assert surface["consecutive_failures"] == 1
        assert "expired" in surface["last_error"]
        assert surface["next_retry_at"]

    def test_failure_returns_structured_500(self, client):
        with patch(
            "hermes_cli.web_server._read_subconscious_surfaces_sync",
            side_effect=RuntimeError("boom"),
        ):
            resp = client.get("/api/subconscious/surfaces")

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]


class TestSubconsciousSuggestionsEndpoints:
    @pytest.fixture(autouse=True)
    def _fresh_cron_stores(self, _isolate_hermes_home):
        # cron.jobs / cron.suggestions bind their storage paths to a
        # module-level constant computed from get_hermes_home() at FIRST
        # import — reloading them here re-binds those constants to THIS
        # test's isolated HERMES_HOME. Without this, whichever test in this
        # process imports the module first "wins" the path for every test
        # that follows (dedup_key collisions, leaked pending suggestions).
        import importlib

        import cron.jobs as jobs_mod
        import cron.suggestions as suggestions_mod

        importlib.reload(jobs_mod)
        importlib.reload(suggestions_mod)

    def _add_pending(self, **overrides):
        from cron.suggestions import add_suggestion

        kwargs = dict(
            title="Daily digest",
            description="Summarize yesterday's activity every morning.",
            source="subconscious",
            job_spec={"prompt": "Summarize yesterday", "schedule": "every 1d"},
            dedup_key="daily-digest",
            category="digest",
        )
        kwargs.update(overrides)
        return add_suggestion(**kwargs)

    def test_list_pending(self, client):
        self._add_pending()

        resp = client.get("/api/subconscious/suggestions")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert len(data["suggestions"]) == 1
        item = data["suggestions"][0]
        assert item["title"] == "Daily digest"
        assert item["summary"] == "Summarize yesterday's activity every morning."
        assert item["source"] == "subconscious"
        assert item["category"] == "digest"
        assert item["tier"] == "propose"
        assert item["created"]

    def test_list_empty(self, client):
        resp = client.get("/api/subconscious/suggestions")

        assert resp.status_code == 200
        assert resp.json()["suggestions"] == []

    def test_config_suggestion_lists_evidence_and_applies_guarded_value(self, client):
        from agent.learning.registry import current_value

        record = self._add_pending(
            title="Tune speaker threshold",
            job_spec=None,
            kind="config",
            config_spec={
                "path": "voice.speaker_id.threshold",
                "current": current_value("voice.speaker_id.threshold"),
                "value": 0.4,
                "rationale": "Owner scores consistently cluster below the current boundary.",
                "scope": "user",
            },
            loop="voice_threshold",
        )

        listed = client.get("/api/subconscious/suggestions").json()["suggestions"][0]
        assert listed["kind"] == "config"
        assert listed["tier"] == "propose"
        assert listed["config_spec"]["human"] == "speaker owner threshold"

        accepted = client.post(f"/api/subconscious/suggestions/{record['id']}/accept")
        assert accepted.status_code == 200
        assert accepted.json()["result"]["value"] == 0.4

    def test_accept_creates_job_and_clears_pending(self, client):
        record = self._add_pending()

        resp = client.post(f"/api/subconscious/suggestions/{record['id']}/accept")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["job"]["prompt"] == "Summarize yesterday"

        follow_up = client.get("/api/subconscious/suggestions")
        assert follow_up.json()["suggestions"] == []

    def test_accept_unknown_id_returns_404(self, client):
        resp = client.post("/api/subconscious/suggestions/does-not-exist/accept")

        assert resp.status_code == 404

    def test_dismiss_clears_pending(self, client):
        record = self._add_pending()

        resp = client.post(f"/api/subconscious/suggestions/{record['id']}/dismiss")

        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        follow_up = client.get("/api/subconscious/suggestions")
        assert follow_up.json()["suggestions"] == []

    def test_dismiss_unknown_id_returns_404(self, client):
        resp = client.post("/api/subconscious/suggestions/does-not-exist/dismiss")

        assert resp.status_code == 404

    def test_accept_failure_returns_structured_500(self, client):
        with patch(
            "hermes_cli.web_server._accept_subconscious_suggestion_sync",
            side_effect=RuntimeError("boom"),
        ):
            resp = client.post("/api/subconscious/suggestions/x/accept")

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]

    def test_accept_stale_config_proposal_returns_conflict(self, client):
        with patch(
            "hermes_cli.web_server._accept_subconscious_suggestion_sync",
            side_effect=ValueError("stale config proposal"),
        ):
            resp = client.post("/api/subconscious/suggestions/x/accept")

        assert resp.status_code == 409


class TestLearningEndpoints:
    def test_summary_is_threadpooled_and_preserves_loop_config_paths(self, client):
        with patch(
            "hermes_cli.web_server._read_learning_summary_sync",
            return_value={
                "loops": [{"loop": "voice_threshold", "config_path": "learning.voice_tuning.enabled", "enabled": True, "samples": 200, "last_proposal": None, "pending": 0}],
                "learned_tiers": ["calendar"],
            },
        ):
            response = client.get("/api/learning/summary")

        assert response.status_code == 200
        assert response.json()["loops"][0]["config_path"] == "learning.voice_tuning.enabled"

    def test_outcomes_filter_and_validation(self, client, _isolate_hermes_home):
        from agent.learning.outcomes import record

        record("trust", "calendar", "accepted", ref="cal-1")
        record("escalation", "voice", "corrected", ref="voice-1")

        response = client.get("/api/learning/outcomes?loop=trust")
        assert response.status_code == 200
        assert [row["loop"] for row in response.json()["outcomes"]] == ["trust"]
        assert client.get("/api/learning/outcomes?loop=unknown").status_code == 400

    def test_dismiss_failure_returns_structured_500(self, client):
        with patch(
            "hermes_cli.web_server._dismiss_subconscious_suggestion_sync",
            side_effect=RuntimeError("boom"),
        ):
            resp = client.post("/api/subconscious/suggestions/x/dismiss")

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]


# ---------------------------------------------------------------------------
# /api/mind — ?history=1 narrative history (2026-07-14 hardening pass)
# ---------------------------------------------------------------------------


class TestMindNarrativeHistoryEndpoint:
    def test_no_history_key_by_default(self, client):
        resp = client.get("/api/mind")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "narrative_history" not in data

    def test_history_flag_returns_rotated_versions(self, client):
        from cron import subconscious

        subconscious.write_narrative("first")
        subconscious.write_narrative("second")
        subconscious.write_narrative("third")

        resp = client.get("/api/mind?history=1")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert [entry["text"] for entry in data["narrative_history"]] == [
            "second",
            "first",
        ]
        assert data["narrative"] == "third"

    def test_history_flag_empty_on_cold_start(self, client):
        resp = client.get("/api/mind?history=1")

        assert resp.status_code == 200
        assert resp.json()["narrative_history"] == []

    def test_failure_returns_structured_500(self, client):
        with patch(
            "hermes_cli.web_server._mind_state_sync", side_effect=RuntimeError("boom")
        ):
            resp = client.get("/api/mind?history=1")

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]


# ---------------------------------------------------------------------------
# /api/brain/* — status, search, index, config (2026-07-14 hardening pass)
# ---------------------------------------------------------------------------


class TestBrainStatusEndpoint:
    def test_reports_config_and_store_stats(self, client):
        resp = client.get("/api/brain/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["enabled"] is False
        assert data["folders"] == []
        assert data["files"] == 0
        assert data["chunks"] == 0
        assert "last_run" in data
        assert data["last_run"]["at"] is None

    def test_reflects_a_completed_index_run(self, client, tmp_path):
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "note.txt").write_text("hello brain", encoding="utf-8")

        put_resp = client.put(
            "/api/brain/config", json={"enabled": True, "folders": [str(folder)]}
        )
        assert put_resp.status_code == 200

        index_resp = client.post("/api/brain/index")
        assert index_resp.status_code == 200

        status_resp = client.get("/api/brain/status")
        data = status_resp.json()
        assert data["ok"] is True
        assert data["enabled"] is True
        assert data["files"] == 1
        assert data["last_run"]["indexed"] == 1
        assert data["last_run"]["at"]

    def test_failure_returns_structured_500(self, client):
        with patch(
            "hermes_cli.web_server._brain_status_sync", side_effect=RuntimeError("boom")
        ):
            resp = client.get("/api/brain/status")

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]


class TestBrainConfigEndpoint:
    def test_rejects_nonexistent_folder_with_structured_error(self, client, tmp_path):
        missing = tmp_path / "does-not-exist"

        resp = client.put("/api/brain/config", json={"folders": [str(missing)]})

        assert resp.status_code == 400
        assert str(missing) in resp.json()["detail"]

    def test_accepts_existing_folder(self, client, tmp_path):
        folder = tmp_path / "real"
        folder.mkdir()

        resp = client.put("/api/brain/config", json={"folders": [str(folder)]})

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["brain"]["folders"] == [str(folder)]

    def test_enable_with_valid_folder_does_not_500(self, client, tmp_path):
        """Regression test: update_brain_config's enable branch calls
        ensure_index_job(cfg), which must actually be imported in this
        function's scope — a prior version of this endpoint only imported
        brain_config, so enabling Brain here raised an unhandled NameError."""
        folder = tmp_path / "real"
        folder.mkdir()

        resp = client.put(
            "/api/brain/config", json={"enabled": True, "folders": [str(folder)]}
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["brain"]["enabled"] is True

    def test_disable_pauses_job_without_error(self, client, tmp_path):
        folder = tmp_path / "real"
        folder.mkdir()
        client.put(
            "/api/brain/config", json={"enabled": True, "folders": [str(folder)]}
        )

        resp = client.put("/api/brain/config", json={"enabled": False})

        assert resp.status_code == 200
        assert resp.json()["brain"]["enabled"] is False

    def test_failure_returns_structured_500(self, client):
        with patch("hermes_cli.config.load_config", side_effect=RuntimeError("boom")):
            resp = client.put("/api/brain/config", json={"enabled": False})

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]


class TestBrainSearchAndIndexEndpoints:
    def test_search_empty_index_returns_no_results(self, client):
        resp = client.get("/api/brain/search", params={"q": "anything"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["results"] == []

    def test_search_failure_returns_structured_500(self, client):
        with patch(
            "tools.brain.store.BrainStore.search", side_effect=RuntimeError("boom")
        ):
            resp = client.get("/api/brain/search", params={"q": "x"})

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]

    def test_index_with_no_folders_configured_is_a_no_op(self, client):
        resp = client.post("/api/brain/index")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["indexed"] == 0

    def test_index_failure_returns_structured_500(self, client):
        with patch(
            "tools.brain.indexer.index_configured_folders",
            side_effect=RuntimeError("boom"),
        ):
            resp = client.post("/api/brain/index")

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]
