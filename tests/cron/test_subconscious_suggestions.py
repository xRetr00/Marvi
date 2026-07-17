"""Tests for the subconscious source + proactivity tiers added to
cron/suggestions.py (Contract 2), and the config/tier plumbing in
cron/subconscious.py (Contract 3).

Uses an isolated HERMES_HOME so the real suggestions.json/config.yaml are
never touched.
"""

import importlib
from unittest.mock import patch

import pytest


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A cron.suggestions module bound to an isolated HERMES_HOME."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.suggestions as s
    importlib.reload(s)
    return s


def _add(store, key="k1", title="Test", source="subconscious", category="general"):
    return store.add_suggestion(
        title=title,
        description="desc",
        source=source,
        job_spec={"prompt": "do it", "schedule": "every 30m", "name": title, "deliver": "origin"},
        dedup_key=key,
        category=category,
    )


class TestSubconsciousSource:
    def test_subconscious_is_a_valid_source(self, store):
        assert "subconscious" in store.VALID_SOURCES
        rec = _add(store)
        assert rec is not None
        assert rec["source"] == "subconscious"

    def test_category_defaults_to_general(self, store):
        rec = store.add_suggestion(
            title="x", description="d", source="subconscious",
            job_spec={"prompt": "p", "schedule": "1h"}, dedup_key="k",
        )
        assert rec["category"] == "general"

    def test_dedup_and_consent_first_semantics_preserved(self, store):
        """Registering never auto-creates a job — it's still just a pending record."""
        assert _add(store, key="dup") is not None
        assert _add(store, key="dup") is None
        pending = store.list_pending()
        assert len(pending) == 1
        assert pending[0]["status"] == "pending"

        assert store.dismiss_suggestion("1") is True
        assert _add(store, key="dup") is None  # latched, never re-offered


class TestTiers:
    def test_default_tier_is_propose(self, store):
        assert store.resolve_tier("unknown-category") == store.DEFAULT_TIER
        assert store.resolve_tier("unknown-category") == "propose"

    def test_configured_tier_is_respected(self, store):
        tiers = {"morning-briefing": "auto", "spending-alerts": "notify"}
        assert store.resolve_tier("morning-briefing", tiers=tiers) == "auto"
        assert store.resolve_tier("spending-alerts", tiers=tiers) == "notify"
        assert store.resolve_tier("anything-else", tiers=tiers) == "propose"

    def test_invalid_configured_value_falls_back_to_default(self, store):
        tiers = {"weird": "not-a-real-tier"}
        assert store.resolve_tier("weird", tiers=tiers) == "propose"

    def test_is_auto_tier(self, store):
        tiers = {"auto-approved": "auto"}
        assert store.is_auto_tier("auto-approved", tiers=tiers) is True
        assert store.is_auto_tier("everything-else", tiers=tiers) is False

    def test_resolve_tier_reads_config_by_default(self, store, monkeypatch):
        from hermes_cli.config import load_config, save_config

        cfg = load_config()
        cfg["subconscious"] = {"tiers": {"news-digest": "auto"}}
        save_config(cfg)

        assert store.resolve_tier("news-digest") == "auto"
        assert store.resolve_tier("something-unconfigured") == "propose"


class TestSubconsciousConfig:
    @pytest.fixture
    def subconscious(self, tmp_path, monkeypatch):
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        import hermes_constants
        importlib.reload(hermes_constants)
        import cron.subconscious as sc
        importlib.reload(sc)
        return sc

    def test_disabled_by_default(self, subconscious):
        assert subconscious.is_enabled() is False
        assert subconscious.idle_trigger_minutes() == subconscious.DEFAULT_IDLE_TRIGGER_MINUTES

    def test_enable_creates_job_and_persists_config(self, subconscious):
        created = []

        def fake_create_job(**kwargs):
            created.append(kwargs)
            return {"id": f"job{len(created)}", "name": kwargs.get("name"), "schedule_display": kwargs["schedule"]}

        with patch("cron.jobs.create_job", fake_create_job):
            info = subconscious.enable()

        assert info["enabled"] is True
        tick = next(job for job in created if job["name"] == subconscious.JOB_NAME)
        reflection = next(job for job in created if job["name"] == subconscious.REFLECTION_JOB_NAME)
        assert tick["schedule"] == "every 20m"
        assert tick["script"] == subconscious.SNAPSHOT_SHIM_NAME
        assert reflection["schedule"] == subconscious.DEFAULT_REFLECTION_SCHEDULE
        assert subconscious.is_enabled() is True

    def test_enable_is_idempotent_no_second_job(self, subconscious):
        calls = {"create": 0}

        def fake_create_job(**kwargs):
            calls["create"] += 1
            return {"id": f"job{calls['create']}", "name": kwargs.get("name"), "schedule_display": kwargs["schedule"]}

        with patch("cron.jobs.create_job", fake_create_job), \
             patch("cron.jobs.get_job", lambda jid: {"id": jid, "state": "scheduled", "schedule_display": "every 20m"}):
            subconscious.enable()
            subconscious.enable()

        # First enable creates the tick + reflection + dreaming jobs; the
        # second is idempotent (all three already tracked → no new create).
        assert calls["create"] == 3

    def test_tick_toolsets_are_all_registered(self, subconscious):
        """Regression guard: every name in _TICK_TOOLSETS must be a real,
        registered toolset (tools.registry) — a typo here (e.g. "search"
        instead of "web") silently strips that capability from every
        subconscious tick with no error anywhere, since create_job's
        enabled_toolsets is just a filter, not a validated reference."""
        from tools.registry import discover_builtin_tools, registry

        discover_builtin_tools()
        registered = set(registry.get_registered_toolset_names())
        missing = [t for t in subconscious._TICK_TOOLSETS if t not in registered]
        assert missing == [], f"_TICK_TOOLSETS references unregistered toolset(s): {missing}"

    def test_disable_pauses_job_and_flips_config(self, subconscious):
        with patch("cron.jobs.create_job", lambda **k: {"id": "job123", "schedule_display": "every 20m"}):
            subconscious.enable()

        with patch("cron.jobs.pause_job") as mock_pause:
            info = subconscious.disable()

        # Disable now pauses all three background-thinking jobs: tick,
        # reflection, and the weekly dreaming consolidation.
        assert mock_pause.call_count == 3
        assert info["enabled"] is False

    def test_snapshot_shim_written_under_hermes_home(self, subconscious):
        shim = subconscious._write_snapshot_shim()
        assert shim.exists()
        content = shim.read_text(encoding="utf-8")
        assert "runpy.run_path" in content
        assert "subconscious_snapshot.py" in content

    def test_trigger_tick_noop_when_disabled(self, subconscious):
        assert subconscious.trigger_tick() is False

    def test_trigger_tick_calls_cron_trigger_job(self, subconscious):
        with patch("cron.jobs.create_job", lambda **k: {"id": "job123", "schedule_display": "every 20m"}):
            subconscious.enable()

        with patch("cron.jobs.trigger_job", return_value={"id": "job123"}) as mock_trigger:
            assert subconscious.trigger_tick(reason="idle") is True
        mock_trigger.assert_called_once_with("job123")
