"""Compatibility tests for the Marvi rebrand."""

from pathlib import Path

import hermes_constants as hc
from hermes_cli import banner
from hermes_cli.commands import resolve_command


class TestMarviHomeCompatibility:
    def test_marvi_home_is_preferred_over_legacy_hermes_home(self, tmp_path, monkeypatch):
        """MARVI_HOME should win when both home env vars are present."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(hc, "_profile_fallback_warned", False)

        marvi_home = tmp_path / ".marvi"
        legacy_home = tmp_path / ".hermes"
        marvi_home.mkdir()
        legacy_home.mkdir()

        monkeypatch.setenv("MARVI_HOME", str(marvi_home))
        monkeypatch.setenv("HERMES_HOME", str(legacy_home))

        assert hc.get_hermes_home() == marvi_home
        assert hc.display_hermes_home() == "~/.marvi"

    def test_legacy_hermes_home_still_resolves_when_marvi_home_is_unset(
        self, tmp_path, monkeypatch
    ):
        """Legacy HERMES_HOME installs must remain readable during migration."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(hc, "_profile_fallback_warned", False)

        legacy_home = tmp_path / ".hermes"
        legacy_home.mkdir()

        monkeypatch.delenv("MARVI_HOME", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(legacy_home))

        assert hc.get_hermes_home() == legacy_home
        assert hc.display_hermes_home() == "~/.hermes"

    def test_default_root_honors_marvi_home_alias(self, tmp_path, monkeypatch):
        """Profile-level helpers should continue accepting MARVI_HOME."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        marvi_profile = tmp_path / ".marvi" / "profiles" / "coder"
        legacy_home = tmp_path / ".hermes"
        marvi_profile.mkdir(parents=True)
        legacy_home.mkdir()

        monkeypatch.setenv("MARVI_HOME", str(marvi_profile))
        monkeypatch.setenv("HERMES_HOME", str(legacy_home))

        assert hc.get_default_hermes_root() == tmp_path / ".marvi"


class TestMarviCliIdentity:
    def test_version_label_uses_marvi_branding_and_legacy_command_stays_registered(
        self, monkeypatch
    ):
        """The visible brand should be Marvi while the hermes command remains valid."""
        monkeypatch.setattr(banner, "get_git_banner_state", lambda: None)

        assert banner.format_banner_version_label().startswith("Marvi Agent v")
        assert resolve_command("version") is not None
        assert resolve_command("v") is not None
