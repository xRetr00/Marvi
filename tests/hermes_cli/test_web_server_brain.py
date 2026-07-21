"""Tests for the Brain status/config endpoints in hermes_cli/web_server.py --
specifically the additive fields the 2026-07-20 self-feeding pass added to
``GET /api/brain/status`` (discovered_folders, collected, last_discovery,
last_collect, auto_discover, ...) and the new ``PUT /api/brain/config``
accepted keys (auto_discover, max_auto_folders, collect).

Follows the ``TestClient`` + ``_isolate_hermes_home`` pattern used throughout
tests/hermes_cli/test_web_server_marvi.py.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def client(monkeypatch, _isolate_hermes_home):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    import hermes_state
    from hermes_constants import get_hermes_home
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")

    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


class TestBrainStatusEndpoint:
    def test_status_includes_self_feeding_fields(self, client):
        resp = client.get("/api/brain/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        for key in (
            "auto_discover",
            "max_auto_folders",
            "auto_folders",
            "collect_email",
            "collect_github",
            "github_max_repos",
            "discovered_folders",
            "last_discovery",
            "collected",
            "last_collect",
        ):
            assert key in data
        # Original fields untouched.
        assert "enabled" in data
        assert "files" in data
        assert "last_run" in data


class TestBrainConfigEndpoint:
    def test_put_accepts_auto_discover_and_collect_flags(self, client):
        resp = client.put(
            "/api/brain/config",
            json={"auto_discover": False, "collect": {"email": False, "github": True}},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["brain"]["auto_discover"] is False
        assert data["brain"]["collect_email"] is False
        assert data["brain"]["collect_github"] is True

    def test_put_merges_collect_flags_instead_of_replacing(self, client):
        client.put("/api/brain/config", json={"collect": {"email": False}})
        resp = client.put("/api/brain/config", json={"collect": {"github": False}})

        data = resp.json()
        # The first call's email=False must survive the second call's
        # github-only patch -- collect is merged, not replaced.
        assert data["brain"]["collect_email"] is False
        assert data["brain"]["collect_github"] is False

    def test_status_reflects_a_prior_config_put(self, client):
        client.put("/api/brain/config", json={"auto_discover": False})

        resp = client.get("/api/brain/status")

        assert resp.json()["auto_discover"] is False
