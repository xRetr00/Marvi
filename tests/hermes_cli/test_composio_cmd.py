"""Tests for ``hermes_cli/composio_cmd.py`` (`hermes composio connect|list`).

The Composio SDK is fully mocked -- no test here imports the real
``composio`` package.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import cron.scripts.subconscious.composio_client as composio_client_mod
from hermes_cli import composio_cmd
from hermes_cli.config import get_env_value, load_config, read_raw_config


class FakeClient:
    def __init__(self, api_key=None, *, auth_ok=True, connect_result=None, connect_error=None):
        self.api_key = api_key
        self._auth_ok = auth_ok
        self._connect_result = connect_result or {"status": "active"}
        self._connect_error = connect_error

    def verify_auth(self):
        if not self._auth_ok:
            raise composio_client_mod.ComposioAuthError("bad key")
        return True

    def initiate_connection(self, app, *, user_id="default"):
        if self._connect_error:
            raise self._connect_error
        return self._connect_result

    def get_connection_status(self, app, *, user_id="default"):
        return {"connected": True, "status": "active"}


@pytest.fixture
def fake_sdk(monkeypatch):
    """Pretend the Composio SDK is installed and route ComposioClient() to a
    FakeClient instance so no test touches the real SDK."""
    state = {"auth_ok": True, "connect_error": None}

    def _factory(api_key):
        return FakeClient(api_key, auth_ok=state["auth_ok"], connect_error=state["connect_error"])

    monkeypatch.setattr(composio_client_mod, "is_sdk_installed", lambda: True)
    monkeypatch.setattr(composio_client_mod, "ComposioClient", _factory)
    monkeypatch.setattr(composio_client_mod, "get_client", lambda api_key=None: _factory(api_key or "x"))
    return state


class TestConnect:
    def test_connect_requires_an_app_name(self, capsys):
        args = SimpleNamespace(app="", api_key="k")
        with pytest.raises(SystemExit) as exc:
            composio_cmd.cmd_composio_connect(args)
        assert exc.value.code == 1
        assert "Usage" in capsys.readouterr().out

    def test_connect_without_api_key_and_non_interactive_fails(self, monkeypatch, fake_sdk):
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        args = SimpleNamespace(app="gmail", api_key=None)
        with pytest.raises(SystemExit) as exc:
            composio_cmd.cmd_composio_connect(args)
        assert exc.value.code == 1

    def test_connect_fails_when_sdk_missing_and_auto_install_fails(self, monkeypatch):
        monkeypatch.setattr(composio_client_mod, "is_sdk_installed", lambda: False)
        monkeypatch.setattr(
            composio_client_mod,
            "ensure_sdk_installed",
            lambda **kw: (_ for _ in ()).throw(composio_client_mod.ComposioUnavailable("nope, no network")),
        )
        args = SimpleNamespace(app="gmail", api_key="k123")
        with pytest.raises(SystemExit) as exc:
            composio_cmd.cmd_composio_connect(args)
        assert exc.value.code == 1

    def test_connect_auto_installs_missing_sdk_then_connects(self, monkeypatch, fake_sdk, capsys):
        # is_sdk_installed() reports missing up front, but ensure_sdk_installed()
        # (the lazy auto-install path) succeeds -- connect should proceed
        # instead of asking the user to install it themselves.
        calls = []
        monkeypatch.setattr(composio_client_mod, "is_sdk_installed", lambda: False)
        monkeypatch.setattr(
            composio_client_mod,
            "ensure_sdk_installed",
            lambda **kw: calls.append(kw) or True,
        )

        args = SimpleNamespace(app="gmail", api_key="k123")
        composio_cmd.cmd_composio_connect(args)

        assert calls, "ensure_sdk_installed should have been called instead of just printing a hint"
        assert calls[0].get("prompt") is True  # interactive CLI call site prompts
        out = capsys.readouterr().out
        assert "installing" in out.lower()
        config = load_config()
        assert "gmail" in config["composio"]["surfaces"]

    def test_connect_success_persists_secret_mcp_and_surface(self, fake_sdk, capsys):
        args = SimpleNamespace(app="gmail", api_key="k123")
        composio_cmd.cmd_composio_connect(args)

        config = load_config()
        assert "api_key" not in config["composio"]
        assert get_env_value("COMPOSIO_API_KEY") == "k123"
        assert config["mcp_servers"]["composio"]["url"] == "https://connect.composio.dev/mcp"
        assert read_raw_config()["mcp_servers"]["composio"]["headers"] == {
            "x-consumer-api-key": "${COMPOSIO_API_KEY}"
        }
        assert "gmail" in config["composio"]["surfaces"]
        assert "Marvi is now set up to watch 'gmail'" in capsys.readouterr().out

    def test_connect_is_idempotent_on_surfaces_list(self, fake_sdk):
        args = SimpleNamespace(app="gmail", api_key="k123")
        composio_cmd.cmd_composio_connect(args)
        composio_cmd.cmd_composio_connect(args)

        config = load_config()
        assert config["composio"]["surfaces"].count("gmail") == 1

    def test_connect_auth_error_aborts_without_saving_surface(self, fake_sdk, capsys):
        fake_sdk["auth_ok"] = False
        args = SimpleNamespace(app="gmail", api_key="badkey")

        with pytest.raises(SystemExit) as exc:
            composio_cmd.cmd_composio_connect(args)
        assert exc.value.code == 1

        config = load_config()
        assert "gmail" not in (config.get("composio", {}) or {}).get("surfaces", [])

    def test_connect_warns_on_unimplemented_surface_but_still_connects(self, fake_sdk, capsys):
        args = SimpleNamespace(app="notion", api_key="k123")
        composio_cmd.cmd_composio_connect(args)

        out = capsys.readouterr().out
        assert "no delta-fetcher implemented" in out
        config = load_config()
        assert "notion" in config["composio"]["surfaces"]

    def test_connect_prints_redirect_url_when_present(self, fake_sdk, capsys, monkeypatch):
        fake_client = FakeClient(
            "k123", connect_result={"status": "pending", "redirect_url": "https://composio.dev/auth/abc"}
        )
        monkeypatch.setattr(composio_client_mod, "ComposioClient", lambda api_key: fake_client)

        args = SimpleNamespace(app="gmail", api_key="k123")
        composio_cmd.cmd_composio_connect(args)

        out = capsys.readouterr().out
        assert "https://composio.dev/auth/abc" in out


class TestList:
    def test_list_with_no_surfaces_configured(self, capsys):
        composio_cmd.cmd_composio_list(SimpleNamespace())
        out = capsys.readouterr().out
        assert "No surfaces connected yet" in out

    def test_list_reports_connected_status_and_sync_freshness(self, fake_sdk, capsys):
        # Connect first so config + api key exist.
        composio_cmd.cmd_composio_connect(SimpleNamespace(app="gmail", api_key="k123"))
        capsys.readouterr()  # discard connect output

        composio_cmd.cmd_composio_list(SimpleNamespace())
        out = capsys.readouterr().out
        assert "gmail" in out
        assert "connected" in out
        assert "never" in out  # no sync has happened yet

    def test_list_reports_sdk_not_installed(self, monkeypatch, capsys):
        monkeypatch.setattr(composio_client_mod, "is_sdk_installed", lambda: False)
        from hermes_cli.config import load_config, save_config

        config = load_config()
        config.setdefault("composio", {})["surfaces"] = ["gmail"]
        save_config(config)

        composio_cmd.cmd_composio_list(SimpleNamespace())
        out = capsys.readouterr().out
        assert "sdk not installed" in out
