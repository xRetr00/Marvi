"""Tests for cron/scripts/subconscious/composio_client.py's lazy-install
wiring -- ``is_sdk_installed`` stays a cheap, install-free presence check,
while ``ensure_sdk_installed``/``_import_composio_sdk`` route through
``tools.lazy_deps`` (feature ``integration.composio``) to auto-install the
SDK on first use instead of just telling the user to run pip themselves.

No test here imports (or installs) the real ``composio`` package --
``tools.lazy_deps.ensure`` and ``importlib``'s import machinery are faked.
"""

from __future__ import annotations

import builtins
from types import SimpleNamespace

import pytest

import cron.scripts.subconscious.composio_client as composio_client_mod
import tools.lazy_deps as lazy_deps


def _block_composio_import(monkeypatch):
    """Make ``import composio`` raise ImportError, as if the package were
    genuinely not installed."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "composio" or name.startswith("composio."):
            raise ImportError("No module named 'composio'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def _allow_composio_import(monkeypatch):
    """Make ``import composio`` succeed with a stub module object."""
    import sys
    import types

    stub = types.ModuleType("composio")
    monkeypatch.setitem(sys.modules, "composio", stub)
    return stub


class TestIsSdkInstalled:
    def test_returns_false_when_import_fails(self, monkeypatch):
        _block_composio_import(monkeypatch)
        assert composio_client_mod.is_sdk_installed() is False

    def test_returns_true_when_import_succeeds(self, monkeypatch):
        _allow_composio_import(monkeypatch)
        assert composio_client_mod.is_sdk_installed() is True

    def test_never_triggers_an_install(self, monkeypatch):
        """is_sdk_installed() is used by passive status output (`hermes
        composio list`) -- it must never shell out to pip."""
        _block_composio_import(monkeypatch)
        monkeypatch.setattr(
            lazy_deps, "ensure",
            lambda *a, **kw: pytest.fail("is_sdk_installed() must never call lazy_deps.ensure"),
        )
        assert composio_client_mod.is_sdk_installed() is False


class TestEnsureSdkInstalled:
    def test_already_installed_is_a_noop(self, monkeypatch):
        _allow_composio_import(monkeypatch)
        monkeypatch.setattr(
            lazy_deps, "ensure",
            lambda *a, **kw: pytest.fail("ensure_sdk_installed() should not call lazy_deps.ensure when already installed"),
        )
        assert composio_client_mod.ensure_sdk_installed() is True

    def test_missing_sdk_triggers_lazy_install(self, monkeypatch):
        _block_composio_import(monkeypatch)
        calls = []

        def fake_ensure(feature, *, prompt=False):
            calls.append((feature, prompt))
            _allow_composio_import(monkeypatch)  # simulate a successful install

        monkeypatch.setattr(lazy_deps, "ensure", fake_ensure)

        assert composio_client_mod.ensure_sdk_installed(prompt=True) is True
        assert calls == [("integration.composio", True)]

    def test_declined_install_raises_composio_unavailable(self, monkeypatch):
        _block_composio_import(monkeypatch)

        def fake_ensure(feature, *, prompt=False):
            raise lazy_deps.FeatureUnavailable(feature, ("composio==0.17.1",), "user declined install at prompt")

        monkeypatch.setattr(lazy_deps, "ensure", fake_ensure)

        with pytest.raises(composio_client_mod.ComposioUnavailable, match="declined"):
            composio_client_mod.ensure_sdk_installed(prompt=True)

    def test_disabled_lazy_installs_raises_composio_unavailable(self, monkeypatch):
        _block_composio_import(monkeypatch)

        def fake_ensure(feature, *, prompt=False):
            raise lazy_deps.FeatureUnavailable(feature, ("composio==0.17.1",), "lazy installs disabled (security.allow_lazy_installs=false)")

        monkeypatch.setattr(lazy_deps, "ensure", fake_ensure)

        with pytest.raises(composio_client_mod.ComposioUnavailable, match="lazy installs disabled"):
            composio_client_mod.ensure_sdk_installed(prompt=False)

    def test_install_reports_success_but_still_not_importable_raises(self, monkeypatch):
        # ensure() itself succeeds (no exception) but the package still
        # doesn't import afterward -- must not silently claim success.
        _block_composio_import(monkeypatch)
        monkeypatch.setattr(lazy_deps, "ensure", lambda *a, **kw: None)

        with pytest.raises(composio_client_mod.ComposioUnavailable):
            composio_client_mod.ensure_sdk_installed()


class TestImportComposioSdk:
    def test_auto_installs_when_missing(self, monkeypatch):
        _block_composio_import(monkeypatch)

        def fake_ensure(feature, *, prompt=False):
            assert feature == "integration.composio"
            assert prompt is False  # internal SDK-usage seam is unattended
            _allow_composio_import(monkeypatch)

        monkeypatch.setattr(lazy_deps, "ensure", fake_ensure)

        mod = composio_client_mod._import_composio_sdk()
        assert mod.__name__ == "composio"

    def test_already_installed_skips_lazy_deps_entirely(self, monkeypatch):
        _allow_composio_import(monkeypatch)
        monkeypatch.setattr(
            lazy_deps, "ensure",
            lambda *a, **kw: pytest.fail("_import_composio_sdk() should not call lazy_deps.ensure when already installed"),
        )
        composio_client_mod._import_composio_sdk()

    def test_raises_composio_unavailable_not_import_error(self, monkeypatch):
        _block_composio_import(monkeypatch)

        def fake_ensure(feature, *, prompt=False):
            raise lazy_deps.FeatureUnavailable(feature, ("composio==0.17.1",), "pip install failed: network unreachable")

        monkeypatch.setattr(lazy_deps, "ensure", fake_ensure)

        with pytest.raises(composio_client_mod.ComposioUnavailable):
            composio_client_mod._import_composio_sdk()


class TestInstallHint:
    def test_hint_mentions_lazy_deps_pinned_spec(self):
        hint = composio_client_mod.install_hint()
        assert "composio==0.17.1" in hint

    def test_hint_falls_back_gracefully_if_lazy_deps_unavailable(self, monkeypatch):
        import builtins as _builtins

        real_import = _builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "tools.lazy_deps":
                raise ImportError("boom")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(_builtins, "__import__", fake_import)
        hint = composio_client_mod.install_hint()
        assert "composio==0.17.1" in hint


def test_verify_auth_retries_sdk_without_removed_user_id_kwarg():
    calls = []

    class ConnectedAccounts:
        def list(self):
            calls.append("listed")
            return []

    client = composio_client_mod.ComposioClient("valid-key")
    client._sdk_client = SimpleNamespace(connected_accounts=ConnectedAccounts())

    assert client.verify_auth() is True
    assert calls == ["listed"]


def test_lazy_deps_allowlist_registers_composio():
    assert "integration.composio" in lazy_deps.LAZY_DEPS
    assert lazy_deps.LAZY_DEPS["integration.composio"] == (composio_client_mod.COMPOSIO_PACKAGE_SPEC,)
