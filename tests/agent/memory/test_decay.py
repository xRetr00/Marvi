"""Tests for memory decay + contamination control (Loop 3, memory-maturity
spec §Loop 3, ``agent/memory/decay.py``).

HERMES_HOME is isolated to a per-test tempdir by the autouse
``_hermetic_environment`` fixture in ``tests/conftest.py``. ``cron.suggestions``
caches its storage paths (``CRON_DIR``/``SUGGESTIONS_FILE``) at import time,
so — mirroring ``tests/cron/test_suggestions.py`` — the autouse
``_reload_suggestions_module`` fixture below reloads it after HERMES_HOME is
set, keeping suggestion state from leaking between test functions that share
a process.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.memory import decay


def _empty_result():
    return {"archived": 0, "merged": 0, "merge_suggestions": 0, "contradictions_flagged": 0, "errors": 0}


@pytest.fixture(autouse=True)
def _reload_suggestions_module(_hermetic_environment):
    """Keep cron.suggestions' module-level paths in sync with this test's
    isolated HERMES_HOME (see tests/cron/test_suggestions.py's `store`
    fixture for the same pattern)."""
    import hermes_constants

    importlib.reload(hermes_constants)
    import cron.suggestions as suggestions_module

    importlib.reload(suggestions_module)
    return suggestions_module


# ---------------------------------------------------------------------------
# (a) Recency/usage scoring math -- pure functions, no I/O.
# ---------------------------------------------------------------------------


class TestRelevanceScore:
    def test_fresh_entry_scores_at_the_top(self):
        assert decay.relevance_score(0, 0) == pytest.approx(1.0)

    def test_score_decreases_monotonically_with_age_and_idle_time(self):
        s0 = decay.relevance_score(0, 0)
        s30 = decay.relevance_score(30, 30)
        s365 = decay.relevance_score(365, 365)
        assert s0 > s30 > s365

    def test_recently_surfaced_scores_higher_than_equally_old_but_idle(self):
        old_and_idle = decay.relevance_score(400, 400)
        old_but_recently_surfaced = decay.relevance_score(400, 0)
        assert old_but_recently_surfaced > old_and_idle

    def test_score_is_bounded_and_negative_inputs_are_clamped(self):
        assert 0.0 <= decay.relevance_score(10_000, 10_000) <= 1.0
        assert decay.relevance_score(-5, -5) == decay.relevance_score(0, 0)


# ---------------------------------------------------------------------------
# (b) Dedup heuristic math -- pure functions.
# ---------------------------------------------------------------------------


class TestTextSimilarityHeuristic:
    def test_identical_text_has_similarity_1(self):
        assert decay.text_similarity("User prefers dark mode", "User prefers dark mode") == 1.0

    def test_unrelated_text_has_low_similarity(self):
        assert decay.text_similarity("User prefers dark mode", "Project uses FastAPI for the API") < 0.5

    def test_containment_true_when_shorter_is_a_prefix_of_longer(self):
        a = "[preferences] User prefers dark mode"
        b = "[preferences] User prefers dark mode in the code editor"
        assert decay.is_containment_duplicate(a, b) is True

    def test_containment_false_for_two_entries_with_distinct_information(self):
        a = "[preferences] User prefers dark mode"
        b = "[preferences] User prefers Vim keybindings"
        assert decay.is_containment_duplicate(a, b) is False


# ---------------------------------------------------------------------------
# (c) Contradiction heuristic -- precision-focused positive/negative cases.
# ---------------------------------------------------------------------------


class TestContradictionDetection:
    def test_positive_same_subject_differing_value(self):
        reason = decay.detect_contradiction(
            "[work] User works at Acme Corp", "[work] User works at Globex Inc"
        )
        assert reason is not None

    def test_positive_shared_subject_with_negation(self):
        reason = decay.detect_contradiction(
            "[notifications] User likes email notifications",
            "[notifications] User doesn't like email notifications",
        )
        assert reason is not None

    def test_negative_different_topics_never_compared(self):
        # Same "conflict shape" (differing value) but different [topic] --
        # the precision guard refuses to compare across topics.
        reason = decay.detect_contradiction(
            "[work] User works at Acme Corp", "[home] User lives in Seattle"
        )
        assert reason is None

    def test_negative_unrelated_entries_in_same_topic(self):
        reason = decay.detect_contradiction(
            "[misc] User likes pizza on Fridays", "[misc] Project uses Python 3.12"
        )
        assert reason is None

    def test_negative_identical_entries_not_flagged(self):
        text = "[work] User works at Acme Corp"
        assert decay.detect_contradiction(text, text) is None


# ---------------------------------------------------------------------------
# (a) Recency pass integration -- archive threshold + min-age gate.
# ---------------------------------------------------------------------------


class TestRecencyPassIntegration:
    def test_never_tracked_entry_is_seeded_not_archived(self):
        from tools.memory_tool import MemoryStore, get_surfaced_meta

        store = MemoryStore()
        store.add("memory", "Stale fact")
        store.load_from_disk()

        cfg = decay.decay_config()
        result = _empty_result()
        decay._run_recency_pass(store, cfg, result)

        assert result["archived"] == 0
        assert "Stale fact" in store._entries_for("memory")
        assert get_surfaced_meta("Stale fact") != {}  # seeded for next time

    def test_young_entry_not_archived_even_with_a_forced_low_score(self, monkeypatch):
        """The min_age_days gate must independently block archiving --
        a young entry stays hot even if its computed relevance is low."""
        from tools.memory_tool import MemoryStore

        store = MemoryStore()
        store.add("memory", "Young fact")
        store.load_from_disk()

        monkeypatch.setattr(decay, "relevance_score", lambda age, idle: 0.01)

        cfg = decay.decay_config()
        result = _empty_result()
        decay._run_recency_pass(store, cfg, result)  # seeds first_seen = now
        decay._run_recency_pass(store, cfg, result)  # now scored, but age ~0

        assert result["archived"] == 0
        assert "Young fact" in store._entries_for("memory")

    def test_old_entry_with_low_score_is_archived(self, monkeypatch):
        from tools.memory_tool import MemoryStore, entry_hash, get_memory_meta_dir

        store = MemoryStore()
        store.add("memory", "Old stale fact")
        store.load_from_disk()

        meta_dir = get_memory_meta_dir()
        meta_dir.mkdir(parents=True, exist_ok=True)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        h = entry_hash("Old stale fact")
        (meta_dir / "surfaced.json").write_text(
            json.dumps({"entries": {h: {"first_seen": old_ts, "last_surfaced": old_ts}}}),
            encoding="utf-8",
        )

        monkeypatch.setattr(decay, "relevance_score", lambda age, idle: 0.01)

        cfg = decay.decay_config()
        result = _empty_result()
        decay._run_recency_pass(store, cfg, result)

        assert result["archived"] == 1
        assert "Old stale fact" not in store._entries_for("memory")

    def test_old_but_high_scoring_entry_is_not_archived(self):
        """An entry that's old but still relevant (kept fresh by
        surfacing) must not be archived just because of its age."""
        from tools.memory_tool import MemoryStore, entry_hash, get_memory_meta_dir

        store = MemoryStore()
        store.add("memory", "Old but actively used fact")
        store.load_from_disk()

        meta_dir = get_memory_meta_dir()
        meta_dir.mkdir(parents=True, exist_ok=True)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        now_ts = datetime.now(timezone.utc).isoformat()
        h = entry_hash("Old but actively used fact")
        (meta_dir / "surfaced.json").write_text(
            json.dumps({"entries": {h: {"first_seen": old_ts, "last_surfaced": now_ts}}}),
            encoding="utf-8",
        )

        cfg = decay.decay_config()
        result = _empty_result()
        decay._run_recency_pass(store, cfg, result)

        assert result["archived"] == 0
        assert "Old but actively used fact" in store._entries_for("memory")


# ---------------------------------------------------------------------------
# (b) Dedup pass integration -- autonomous merge vs info-dropping suggestion.
# ---------------------------------------------------------------------------


class TestDedupPassIntegration:
    def test_pure_duplicate_is_merged_autonomously(self):
        from tools.memory_tool import MemoryStore, list_archived

        store = MemoryStore()
        store.add("memory", "[prefs] User prefers dark mode")
        store.add("memory", "[prefs] User prefers dark mode now")
        store.load_from_disk()

        cfg = decay.decay_config()
        result = _empty_result()
        decay._run_dedup_pass(store, cfg, result)

        assert result["merged"] == 1
        assert result["merge_suggestions"] == 0
        remaining = store._entries_for("memory")
        assert len(remaining) == 1
        assert remaining[0] == "[prefs] User prefers dark mode now"  # kept the longer, more informative entry

        archived = list_archived(target="memory")
        assert len(archived) == 1
        assert archived[0]["text"] == "[prefs] User prefers dark mode"

    def test_info_dropping_merge_is_proposed_not_autonomous(self):
        from tools.memory_tool import MemoryStore
        from cron.suggestions import list_pending

        store = MemoryStore()
        store.add("memory", "[prefs] User prefers dark mode and uses vim keybindings daily")
        store.add("memory", "[prefs] User prefers dark mode and uses emacs keybindings daily")
        store.load_from_disk()

        cfg = decay.decay_config()
        result = _empty_result()
        decay._run_dedup_pass(store, cfg, result)

        assert result["merged"] == 0
        assert result["merge_suggestions"] == 1
        # Nothing dropped autonomously -- both entries remain live.
        assert len(store._entries_for("memory")) == 2

        pending = list_pending()
        memory_suggestions = [s for s in pending if s["kind"] == "memory"]
        assert len(memory_suggestions) == 1
        assert memory_suggestions[0]["memory_spec"]["op"] == "merge"

    def test_dissimilar_entries_are_left_alone(self):
        from tools.memory_tool import MemoryStore
        from cron.suggestions import list_pending

        store = MemoryStore()
        store.add("memory", "[prefs] User prefers dark mode")
        store.add("memory", "[work] Project uses FastAPI on the backend")
        store.load_from_disk()

        cfg = decay.decay_config()
        result = _empty_result()
        decay._run_dedup_pass(store, cfg, result)

        assert result == _empty_result()
        assert len(store._entries_for("memory")) == 2
        assert list_pending() == []


# ---------------------------------------------------------------------------
# (c) Contradiction pass integration.
# ---------------------------------------------------------------------------


class TestContradictionPassIntegration:
    def test_conflicting_entries_flagged_and_never_auto_resolved(self):
        from tools.memory_tool import MemoryStore
        from cron.suggestions import list_pending

        store = MemoryStore()
        store.add("memory", "[work] User works at Acme Corp")
        store.add("memory", "[work] User works at Globex Inc")
        store.load_from_disk()

        cfg = decay.decay_config()
        result = _empty_result()
        decay._run_contradiction_pass(store, cfg, result)

        assert result["contradictions_flagged"] == 1
        pending = list_pending()
        memory_suggestions = [s for s in pending if s["kind"] == "memory"]
        assert len(memory_suggestions) == 1
        assert memory_suggestions[0]["memory_spec"]["op"] == "contradiction"
        # Never auto-resolved: both entries remain in the hot store, untouched.
        assert len(store._entries_for("memory")) == 2

    def test_non_conflicting_entries_produce_no_suggestion(self):
        from tools.memory_tool import MemoryStore
        from cron.suggestions import list_pending

        store = MemoryStore()
        store.add("memory", "[work] User works at Acme Corp")
        store.add("memory", "[home] User lives in Seattle")
        store.load_from_disk()

        cfg = decay.decay_config()
        result = _empty_result()
        decay._run_contradiction_pass(store, cfg, result)

        assert result["contradictions_flagged"] == 0
        assert list_pending() == []


# ---------------------------------------------------------------------------
# Ask-user routing addition (Marvi freedom spec §1.4) — additive alongside
# the existing suggestions-inbox path above, never instead of it.
# ---------------------------------------------------------------------------


class TestContradictionAskUserRouting:
    def test_flagged_contradiction_also_calls_ask_user(self, monkeypatch):
        from tools.memory_tool import MemoryStore

        calls = []
        monkeypatch.setattr(
            "agent.autonomy.ask.ask_user",
            lambda question, context="", category="general", **kw: calls.append(
                (question, context, category)
            )
            or {"id": "q1"},
        )

        store = MemoryStore()
        store.add("memory", "[work] User works at Acme Corp")
        store.add("memory", "[work] User works at Globex Inc")
        store.load_from_disk()

        cfg = decay.decay_config()
        result = _empty_result()
        decay._run_contradiction_pass(store, cfg, result)

        assert result["contradictions_flagged"] == 1
        assert len(calls) == 1
        question, context, category = calls[0]
        assert "Acme Corp" in question
        assert "Globex Inc" in question
        assert category == "contradiction"

    def test_ask_user_failure_never_breaks_the_suggestion_path(self, monkeypatch):
        """A broken/missing agent.autonomy.ask must never take down the
        existing, already-working suggestions-inbox contradiction flow."""
        from tools.memory_tool import MemoryStore
        from cron.suggestions import list_pending

        def _boom(*args, **kwargs):
            raise RuntimeError("autonomy module unavailable")

        monkeypatch.setattr("agent.autonomy.ask.ask_user", _boom)

        store = MemoryStore()
        store.add("memory", "[work] User works at Acme Corp")
        store.add("memory", "[work] User works at Globex Inc")
        store.load_from_disk()

        cfg = decay.decay_config()
        result = _empty_result()
        decay._run_contradiction_pass(store, cfg, result)

        assert result["contradictions_flagged"] == 1
        memory_suggestions = [s for s in list_pending() if s["kind"] == "memory"]
        assert len(memory_suggestions) == 1


# ---------------------------------------------------------------------------
# Restore round-trip + never-hard-delete invariant.
# ---------------------------------------------------------------------------


class TestRestoreRoundTrip:
    def test_archive_then_restore_brings_the_entry_back(self):
        from tools.memory_tool import MemoryStore, archive_entry, list_archived, restore_entry

        store = MemoryStore()
        store.add("memory", "User prefers tabs over spaces")
        store.load_from_disk()

        record = archive_entry(store, "memory", "User prefers tabs over spaces", reason="test")
        assert record is not None
        assert "User prefers tabs over spaces" not in store._entries_for("memory")

        result = restore_entry(record["id"])
        assert result["success"] is True
        assert result["text"] == "User prefers tabs over spaces"

        fresh_store = MemoryStore()
        fresh_store.load_from_disk()
        assert "User prefers tabs over spaces" in fresh_store.memory_entries
        assert list_archived(target="memory") == []

    def test_restore_unknown_id_fails_cleanly(self):
        from tools.memory_tool import restore_entry

        result = restore_entry("memory:doesnotexist")
        assert result["success"] is False
        assert "error" in result


class TestNeverHardDeleteInvariant:
    def test_archived_text_is_always_recoverable_from_disk(self):
        from tools.memory_tool import MemoryStore, list_archived

        store = MemoryStore()
        store.add("memory", "[prefs] User prefers dark mode")
        store.add("memory", "[prefs] User prefers dark mode now")
        store.load_from_disk()

        cfg = decay.decay_config()
        result = _empty_result()
        decay._run_dedup_pass(store, cfg, result)

        archived = list_archived(target="memory")
        assert len(archived) == 1
        dropped_text = archived[0]["text"]
        assert dropped_text not in store._entries_for("memory")
        # The exact text is still on disk in the archive -- nothing was
        # permanently destroyed.
        assert dropped_text == "[prefs] User prefers dark mode"
        assert archived[0]["archived_at"]

    def test_run_decay_pass_never_deletes_only_archives(self):
        from tools.memory_tool import MemoryStore, list_archived

        store = MemoryStore()
        store.add("memory", "[prefs] User prefers dark mode")
        store.add("memory", "[prefs] User prefers dark mode now")
        store.load_from_disk()

        decay.run_decay_pass()

        fresh_store = MemoryStore()
        fresh_store.load_from_disk()
        total_live = len(fresh_store.memory_entries)
        total_archived = len(list_archived(target="memory"))
        assert total_live + total_archived == 2  # nothing vanished


# ---------------------------------------------------------------------------
# run_decay_pass orchestration -- never raises, disabled -> no-op.
# ---------------------------------------------------------------------------


class TestRunDecayPassOrchestration:
    def test_disabled_config_is_a_full_noop(self, monkeypatch):
        monkeypatch.setattr(
            decay,
            "decay_config",
            lambda config=None: {
                "enabled": False,
                "archive_threshold": 0.2,
                "min_age_days": 60,
                "dedup_similarity": 0.85,
            },
        )

        result = decay.run_decay_pass()

        assert result == {
            "enabled": False,
            "archived": 0,
            "merged": 0,
            "merge_suggestions": 0,
            "contradictions_flagged": 0,
            "errors": 0,
        }

    def test_never_raises_on_a_completely_empty_store(self):
        result = decay.run_decay_pass()
        assert result["enabled"] is True
        assert result["errors"] == 0

    def test_never_raises_on_corrupt_sidecar_and_archive_files(self):
        from tools.memory_tool import MemoryStore, get_memory_meta_dir

        store = MemoryStore()
        store.add("memory", "A fact")
        store.load_from_disk()

        meta_dir = get_memory_meta_dir()
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / "surfaced.json").write_text("{not valid json", encoding="utf-8")
        (meta_dir / "archive").mkdir(parents=True, exist_ok=True)
        (meta_dir / "archive" / "archive.json").write_text("also not json", encoding="utf-8")

        result = decay.run_decay_pass()

        assert result["errors"] == 0  # corrupt files degrade to empty state, not a crash

    def test_never_raises_when_config_read_blows_up(self, monkeypatch):
        def _boom(config=None):
            raise RuntimeError("config on fire")

        monkeypatch.setattr(decay, "decay_config", _boom)

        result = decay.run_decay_pass()

        assert result["errors"] == 1
        assert result["enabled"] is False

    def test_one_failing_step_does_not_prevent_the_others(self, monkeypatch):
        from tools.memory_tool import MemoryStore

        store = MemoryStore()
        store.add("memory", "[work] User works at Acme Corp")
        store.add("memory", "[work] User works at Globex Inc")
        store.load_from_disk()

        def _boom(store, cfg, result):
            raise RuntimeError("recency pass exploded")

        monkeypatch.setattr(decay, "_run_recency_pass", _boom)

        result = decay.run_decay_pass()

        assert result["errors"] == 1
        # The contradiction pass still ran despite the recency pass blowing up.
        assert result["contradictions_flagged"] == 1

    def test_end_to_end_exercises_all_three_steps(self):
        from tools.memory_tool import MemoryStore

        store = MemoryStore()
        store.add("memory", "[prefs] User prefers dark mode")
        store.add("memory", "[prefs] User prefers dark mode now")
        store.add("memory", "[work] User works at Acme Corp")
        store.add("memory", "[work] User works at Globex Inc")
        store.load_from_disk()

        result = decay.run_decay_pass()

        assert result["enabled"] is True
        assert result["merged"] == 1
        assert result["contradictions_flagged"] == 1
        assert result["errors"] == 0
