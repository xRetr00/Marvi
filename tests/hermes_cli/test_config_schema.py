"""Regression coverage for the desktop config schema surface.

The desktop Settings -> "Memory & Context" section is a schema-driven editor
(apps/desktop/src/app/settings/config-settings.tsx): it only renders a field
if GET /api/config/schema includes it. These tests lock in that
memory.memory_enabled / memory.user_profile_enabled stay present and typed
as booleans, so the toggle can't silently disappear from the UI again if
someone renames the config keys or excludes them via _SCHEMA_OVERRIDES.
"""

from hermes_cli.config import DEFAULT_CONFIG, load_config


def _schema():
    from hermes_cli import web_server

    return web_server.CONFIG_SCHEMA


def test_memory_toggles_in_schema():
    schema = _schema()

    assert schema["memory.memory_enabled"]["type"] == "boolean"
    assert schema["memory.user_profile_enabled"]["type"] == "boolean"


def test_memory_char_limits_in_schema():
    schema = _schema()

    assert schema["memory.memory_char_limit"]["type"] == "number"
    assert schema["memory.user_char_limit"]["type"] == "number"


def test_default_config_memory_enabled_by_default():
    # Backward-compat: an on-disk config that predates the memory section
    # (or omits it) must still resolve memory_enabled/user_profile_enabled
    # to True after the defaults merge, matching DEFAULT_CONFIG.
    assert DEFAULT_CONFIG["memory"]["memory_enabled"] is True
    assert DEFAULT_CONFIG["memory"]["user_profile_enabled"] is True


def test_load_config_backfills_memory_defaults(tmp_path, monkeypatch):
    # A config.yaml that never mentions "memory" at all must still come back
    # from load_config() with memory_enabled/user_profile_enabled populated
    # from DEFAULT_CONFIG -- this is what the desktop GET /api/config
    # response (and therefore the Settings toggle) actually reads.
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "config.yaml").write_text("model:\n  default: test-model\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(home))

    cfg = load_config()

    assert cfg["memory"]["memory_enabled"] is True
    assert cfg["memory"]["user_profile_enabled"] is True
