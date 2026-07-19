"""Tests for the episodic memory store (Loop 1, memory-maturity spec §1.7).

HERMES_HOME is isolated to a per-test tempdir by the autouse
``_hermetic_environment`` fixture in ``tests/conftest.py``, so every test
here gets a fresh ``episodic.db``.
"""

from __future__ import annotations

import json
import logging

import pytest

from agent.memory import episodic


@pytest.fixture(autouse=True)
def _reset_hook_flag(monkeypatch):
    """Each test gets a fresh HERMES_HOME; also reset the session-finalize
    hook's one-shot registration flag so tests that exercise it don't see
    stale state from an earlier test in this file."""
    monkeypatch.setattr(episodic, "_session_finalize_hook_registered", False)


class TestRecordAndQuery:
    def test_record_episode_returns_id_and_is_recallable(self):
        episode_id = episodic.record_episode(
            kind="task", title="Fixed the build", summary="CI was red, now green.", source="test", ref="ref-1",
        )

        assert isinstance(episode_id, int)
        rows = episodic.recent(limit=5)
        assert len(rows) == 1
        assert rows[0]["id"] == episode_id
        assert rows[0]["title"] == "Fixed the build"
        assert rows[0]["kind"] == "task"
        assert rows[0]["actor"] == "marvi"  # default actor
        assert rows[0]["entities"] == []

    def test_record_episode_rejects_invalid_kind(self):
        assert episodic.record_episode(kind="not-a-kind", title="x", source="test", ref="r") is None
        assert episodic.count() == 0

    def test_record_episode_falls_back_to_marvi_for_invalid_actor(self):
        episode_id = episodic.record_episode(
            kind="device", title="Phone connected", source="test", ref="r1", actor="bogus",
        )
        assert episode_id is not None
        row = episodic.recent(limit=1)[0]
        assert row["actor"] == "marvi"

    def test_record_episode_requires_title_and_source(self):
        assert episodic.record_episode(kind="task", title="", source="test", ref="r") is None
        assert episodic.record_episode(kind="task", title="ok", source="", ref="r") is None

    def test_record_episode_clamps_importance(self):
        low = episodic.record_episode(kind="task", title="a", source="s", ref="1", importance=-5)
        high = episodic.record_episode(kind="task", title="b", source="s", ref="2", importance=5)
        rows = {r["id"]: r for r in episodic.recent(limit=10)}
        assert rows[low]["importance"] == 0.0
        assert rows[high]["importance"] == 1.0

    def test_record_episode_stores_entities(self):
        episodic.record_episode(
            kind="room", title="Living room dimmed", source="s", ref="1", entities=["living_room", "lights"],
        )
        row = episodic.recent(limit=1)[0]
        assert set(row["entities"]) == {"living_room", "lights"}

    def test_count_reflects_stored_rows(self):
        assert episodic.count() == 0
        episodic.record_episode(kind="task", title="a", source="s", ref="1")
        episodic.record_episode(kind="task", title="b", source="s", ref="2")
        assert episodic.count() == 2

    def test_logs_metadata_without_episode_content(self, caplog):
        with caplog.at_level(logging.INFO, logger=episodic.__name__):
            episodic.record_episode(
                kind="task",
                title="private remembered title",
                summary="private remembered summary",
                source="test",
                ref="private-reference",
                entities=["private-entity"],
            )
            episodic.query(text="private remembered title", entities=["private-entity"])

        assert "episodic memory recorded" in caplog.text
        assert "episodic memory queried mode=text" in caplog.text
        assert "private remembered title" not in caplog.text
        assert "private remembered summary" not in caplog.text
        assert "private-reference" not in caplog.text
        assert "private-entity" not in caplog.text


class TestIdempotency:
    def test_same_source_ref_is_not_duplicated(self):
        first = episodic.record_episode(kind="task", title="first title", source="cron", ref="job-1")
        second = episodic.record_episode(kind="task", title="second title (ignored)", source="cron", ref="job-1")

        assert first == second
        assert episodic.count() == 1
        # The original title wins -- record_episode is a no-op on a repeat ref.
        assert episodic.recent(limit=1)[0]["title"] == "first title"

    def test_different_ref_is_a_new_row(self):
        episodic.record_episode(kind="task", title="a", source="cron", ref="job-1")
        episodic.record_episode(kind="task", title="b", source="cron", ref="job-2")
        assert episodic.count() == 2

    def test_missing_ref_never_dedupes(self):
        episodic.record_episode(kind="conversation", title="chat", source="session")
        episodic.record_episode(kind="conversation", title="chat", source="session")
        assert episodic.count() == 2


class TestFtsRecall:
    def test_text_query_matches_title_and_summary(self):
        episodic.record_episode(kind="task", title="Deploy the frontend", summary="Shipped v2 to prod", source="s", ref="1")
        episodic.record_episode(kind="task", title="Unrelated errand", summary="Bought groceries", source="s", ref="2")

        results = episodic.query(text="frontend")
        assert len(results) == 1
        assert results[0]["title"] == "Deploy the frontend"

        results2 = episodic.query(text="groceries")
        assert len(results2) == 1
        assert results2[0]["title"] == "Unrelated errand"

    def test_text_query_no_match_returns_empty(self):
        episodic.record_episode(kind="task", title="Deploy", source="s", ref="1")
        assert episodic.query(text="zzz_nonexistent_term") == []

    def test_text_query_respects_kind_filter(self):
        episodic.record_episode(kind="task", title="Sync report", source="s", ref="1")
        episodic.record_episode(kind="room", title="Sync lights", source="s", ref="2")

        results = episodic.query(text="Sync", kind="room")
        assert len(results) == 1
        assert results[0]["kind"] == "room"


class TestTimeRangeQuery:
    def test_since_and_until_filter(self):
        episodic.record_episode(kind="task", title="old", source="s", ref="1", ts="2026-01-01T00:00:00+00:00")
        episodic.record_episode(kind="task", title="mid", source="s", ref="2", ts="2026-06-01T00:00:00+00:00")
        episodic.record_episode(kind="task", title="new", source="s", ref="3", ts="2026-12-01T00:00:00+00:00")

        results = episodic.query(since="2026-02-01T00:00:00+00:00", until="2026-07-01T00:00:00+00:00")
        assert [r["title"] for r in results] == ["mid"]

    def test_query_orders_newest_first(self):
        episodic.record_episode(kind="task", title="first", source="s", ref="1", ts="2026-01-01T00:00:00+00:00")
        episodic.record_episode(kind="task", title="second", source="s", ref="2", ts="2026-06-01T00:00:00+00:00")

        results = episodic.query(limit=10)
        assert [r["title"] for r in results] == ["second", "first"]

    def test_importance_breaks_ties_at_same_timestamp(self):
        ts = "2026-01-01T00:00:00+00:00"
        episodic.record_episode(kind="task", title="low", source="s", ref="1", ts=ts, importance=0.2)
        episodic.record_episode(kind="task", title="high", source="s", ref="2", ts=ts, importance=0.9)

        results = episodic.query(limit=10)
        assert results[0]["title"] == "high"


class TestPurge:
    def test_purge_before_deletes_older_rows_only(self):
        episodic.record_episode(kind="task", title="old", source="s", ref="1", ts="2026-01-01T00:00:00+00:00")
        episodic.record_episode(kind="task", title="new", source="s", ref="2", ts="2026-12-01T00:00:00+00:00")

        deleted = episodic.purge_before("2026-06-01T00:00:00+00:00")

        assert deleted == 1
        assert episodic.count() == 1
        assert episodic.recent(limit=5)[0]["title"] == "new"

    def test_purge_before_also_clears_fts_rows(self):
        episodic.record_episode(kind="task", title="old searchable", source="s", ref="1", ts="2026-01-01T00:00:00+00:00")
        episodic.purge_before("2026-06-01T00:00:00+00:00")
        assert episodic.query(text="searchable") == []

    def test_purge_before_empty_ts_is_noop(self):
        episodic.record_episode(kind="task", title="a", source="s", ref="1")
        assert episodic.purge_before("") == 0
        assert episodic.count() == 1


class TestConfigGate:
    def test_disabled_config_blocks_recording(self, monkeypatch):
        monkeypatch.setattr(episodic, "episodic_config", lambda *a, **k: {
            "enabled": False, "retain_days": 400, "min_importance_for_prompt": 0.4,
        })
        assert episodic.record_episode(kind="task", title="x", source="s", ref="1") is None
        assert episodic.count() == 0

    def test_episodic_config_defaults(self):
        cfg = episodic.episodic_config({})
        assert cfg == {"enabled": True, "retain_days": 400, "min_importance_for_prompt": 0.4}


class TestFormatting:
    def test_format_episode_includes_time_kind_title_summary(self):
        line = episodic.format_episode({
            "ts": "2026-07-17T10:00:00+00:00", "kind": "task", "actor": "marvi",
            "title": "Did a thing", "summary": "Some detail.",
        })
        assert "2026-07-17T10:00:00+00:00" in line
        assert "task/marvi" in line
        assert "Did a thing" in line
        assert "Some detail." in line

    def test_format_episode_without_summary_omits_dash(self):
        line = episodic.format_episode({"ts": "t", "kind": "task", "actor": "marvi", "title": "Bare"})
        assert "Bare" in line
        assert "—" not in line


class TestSessionFinalizeHook:
    def test_on_session_finalize_records_conversation_episode(self, monkeypatch):
        monkeypatch.setattr(
            episodic, "_cheap_session_summary", lambda session_id: ("A chat about widgets", "Discussed widgets."),
        )

        episodic._on_session_finalize(session_id="sess-123", platform="cli", reason="session_expired")

        rows = episodic.recent(limit=5)
        assert len(rows) == 1
        assert rows[0]["kind"] == "conversation"
        assert rows[0]["actor"] == "user"
        assert rows[0]["source"] == "session"
        assert rows[0]["ref"] == "sess-123"
        assert rows[0]["title"] == "A chat about widgets"

    def test_on_session_finalize_skips_when_no_title(self, monkeypatch):
        monkeypatch.setattr(episodic, "_cheap_session_summary", lambda session_id: ("", ""))
        episodic._on_session_finalize(session_id="sess-empty")
        assert episodic.count() == 0

    def test_on_session_finalize_ignores_missing_session_id(self):
        episodic._on_session_finalize(session_id="")
        assert episodic.count() == 0

    def test_register_session_finalize_hook_is_idempotent_and_wires_into_manager(self, monkeypatch):
        from hermes_cli.plugins import get_plugin_manager

        monkeypatch.setattr(
            episodic, "_cheap_session_summary", lambda session_id: ("Recorded via hook", ""),
        )

        episodic.register_session_finalize_hook()
        episodic.register_session_finalize_hook()  # second call must not double-register

        manager = get_plugin_manager()
        callbacks = manager._hooks.get("on_session_finalize", [])
        assert callbacks.count(episodic._on_session_finalize) == 1

        manager.invoke_hook("on_session_finalize", session_id="sess-hook", platform="cli", reason="session_expired")

        rows = episodic.recent(limit=5)
        assert len(rows) == 1
        assert rows[0]["ref"] == "sess-hook"


class TestDbSchema:
    def test_db_created_under_memory_subdir(self):
        episodic.record_episode(kind="task", title="a", source="s", ref="1")
        path = episodic._db_path()
        assert path.exists()
        assert path.parent.name == "memory"
        assert path.name == "episodic.db"
