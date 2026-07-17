"""Tests for adaptive retrieval / retrospective reflection (Loop 4,
memory-maturity spec §Loop 4, ``agent/memory/retrieval.py``).

HERMES_HOME is isolated to a per-test tempdir by the autouse
``_hermetic_environment`` fixture in ``tests/conftest.py`` (see
``tests/agent/memory/test_decay.py`` for the same pattern this file
mirrors).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent.memory import retrieval


# ---------------------------------------------------------------------------
# Usefulness store -- weight CRUD, bounds, never-raise.
# ---------------------------------------------------------------------------


class TestWeightStore:
    def test_untracked_entry_defaults_to_one(self):
        assert retrieval.get_weight("deadbeef00000000") == retrieval.DEFAULT_WEIGHT

    def test_nudge_up_increases_weight(self):
        h = "aaaa000000000001"
        before = retrieval.get_weight(h)
        after = retrieval.nudge(h, 0.3)
        assert after > before
        assert retrieval.get_weight(h) == pytest.approx(after)

    def test_nudge_down_decreases_weight(self):
        h = "aaaa000000000002"
        retrieval.nudge(h, -0.3)
        assert retrieval.get_weight(h) < retrieval.DEFAULT_WEIGHT

    def test_nudge_clamps_to_max(self):
        h = "aaaa000000000003"
        for _ in range(50):
            retrieval.nudge(h, 1.0)
        assert retrieval.get_weight(h) == retrieval.MAX_WEIGHT

    def test_nudge_clamps_to_min_floor(self):
        h = "aaaa000000000004"
        for _ in range(50):
            retrieval.nudge(h, -1.0)
        weight = retrieval.get_weight(h)
        assert weight == retrieval.MIN_WEIGHT
        assert weight > 0.0  # never driven to zero -- the "floor"

    def test_all_weights_reflects_nudges(self):
        h1, h2 = "aaaa000000000005", "aaaa000000000006"
        retrieval.nudge(h1, 0.4)
        retrieval.nudge(h2, -0.4)
        weights = retrieval.all_weights()
        assert weights[h1] == pytest.approx(1.4)
        assert weights[h2] == pytest.approx(0.6)

    def test_never_raises_on_missing_store(self):
        assert retrieval.all_weights() == {}
        assert retrieval.get_weight("whatever") == retrieval.DEFAULT_WEIGHT

    def test_never_raises_on_corrupt_store(self):
        path = retrieval._usefulness_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")

        assert retrieval.all_weights() == {}
        assert retrieval.get_weight("h") == retrieval.DEFAULT_WEIGHT
        # nudge must still work (recovers the file rather than raising)
        result = retrieval.nudge("h", 0.2)
        assert result == pytest.approx(1.2)

    def test_never_raises_on_malformed_weights_entry(self):
        path = retrieval._usefulness_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"weights": {"good": 1.5, "bad": "not-a-number"}}),
            encoding="utf-8",
        )
        weights = retrieval.all_weights()
        assert weights["good"] == 1.5
        assert "bad" not in weights

    def test_weights_keyed_consistently_with_entry_hash(self):
        """The usefulness store must key on exactly the same hash
        tools.memory_tool.entry_hash() produces for a § entry's text, so it
        aligns 1:1 with the surfaced sidecar (Loop 3)."""
        from tools.memory_tool import entry_hash

        text = "[prefs] User prefers dark mode"
        h = entry_hash(text)
        assert len(h) == 16  # sha1 hexdigest truncated to 16 chars

        retrieval.nudge(h, 0.5)
        assert retrieval.get_weight(entry_hash(text)) == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# rank_entries -- ordering, budget-forced selection, floor, golden test.
# ---------------------------------------------------------------------------


class TestRankEntries:
    def test_empty_entries_returns_empty(self):
        assert retrieval.rank_entries([], char_limit=1000) == []

    def test_adaptive_false_returns_entries_unchanged_golden(self):
        """The golden test: when adaptive is off, ordering (and selection)
        is byte-for-byte identical to pre-Loop-4 behavior -- entries come
        back in EXACTLY their original order, regardless of any weights on
        file, and regardless of a budget that would otherwise force a
        choice."""
        from tools.memory_tool import entry_hash

        entries = ["Zulu entry", "Alpha entry", "Mike entry"]
        # Even with wildly different weights and a starving budget, the
        # disabled path must not reorder or drop anything.
        retrieval.nudge(entry_hash("Zulu entry"), -5.0)
        retrieval.nudge(entry_hash("Alpha entry"), 5.0)

        result = retrieval.rank_entries(
            entries, char_limit=1, config={"adaptive": False, "learning_rate": 0.1}
        )
        assert result == entries

    def test_adaptive_true_default_config_is_identity_when_untracked(self):
        """With no usefulness history, all entries share DEFAULT_WEIGHT, so
        the stable sort's tie-break (original order) means adaptive=True
        behaves identically to today when nothing has been learned yet."""
        entries = ["First", "Second", "Third"]
        result = retrieval.rank_entries(entries, char_limit=10_000)
        assert result == entries

    def test_higher_weight_entry_preferred_under_tight_budget(self):
        from tools.memory_tool import entry_hash

        a, b = "AAAA", "BBBB"
        retrieval.nudge(entry_hash(a), 5.0)   # clamps to MAX_WEIGHT
        retrieval.nudge(entry_hash(b), -5.0)  # clamps to MIN_WEIGHT

        # Budget fits exactly one 4-char entry, not both (+ delimiter).
        result = retrieval.rank_entries([b, a], char_limit=4)
        assert result == [a]

    def test_ties_preserve_original_recency_order(self):
        entries = ["One", "Two", "Three"]
        result = retrieval.rank_entries(entries, char_limit=10_000)
        assert result == entries  # all default weight -- stable sort keeps order

    def test_weight_reorders_ahead_of_recency(self):
        from tools.memory_tool import entry_hash

        entries = ["Old note", "Newer note", "Boosted note"]
        retrieval.nudge(entry_hash("Boosted note"), 0.5)

        result = retrieval.rank_entries(entries, char_limit=10_000)
        assert result[0] == "Boosted note"
        # untouched entries keep their relative recency order after the
        # boosted one.
        assert result[1:] == ["Old note", "Newer note"]

    def test_floor_prevents_starvation_entry_still_renders(self):
        """A heavily downweighted entry is clamped at MIN_WEIGHT (never
        zero) and, whenever the budget has room for everything (the common
        case -- entries are already bounded at write time), it still
        appears in the render; it only loses its POSITION, never its
        presence."""
        from tools.memory_tool import entry_hash

        entries = ["Alpha", "Beta", "Gamma"]
        for _ in range(20):
            retrieval.nudge(entry_hash("Beta"), -1.0)
        assert retrieval.get_weight(entry_hash("Beta")) == retrieval.MIN_WEIGHT

        result = retrieval.rank_entries(entries, char_limit=10_000)
        assert "Beta" in result
        assert set(result) == set(entries)

    def test_nothing_fits_falls_back_to_original_list(self):
        entries = ["This entry is way too long to ever fit"]
        result = retrieval.rank_entries(entries, char_limit=1)
        assert result == entries  # pathological budget -> fall back, not empty

    def test_never_raises_when_config_read_blows_up(self, monkeypatch):
        def _boom(config=None):
            raise RuntimeError("config on fire")

        monkeypatch.setattr(retrieval, "retrieval_config", _boom)
        result = retrieval.rank_entries(["A", "B"], char_limit=100)
        assert result == ["A", "B"]

    def test_never_raises_on_corrupt_usefulness_store(self):
        path = retrieval._usefulness_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json at all", encoding="utf-8")

        result = retrieval.rank_entries(["A", "B"], char_limit=100)
        assert set(result) == {"A", "B"}


# ---------------------------------------------------------------------------
# Injection -> outcome capture (the coarser, per-session approximation --
# see agent/memory/retrieval.py's module docstring for the linkage design).
# ---------------------------------------------------------------------------


class TestCapturePreviousBatchOutcome:
    def _seed_surfaced(self, hashes, ts_iso):
        from tools.memory_tool import get_memory_meta_dir

        meta_dir = get_memory_meta_dir()
        meta_dir.mkdir(parents=True, exist_ok=True)
        table = {h: {"first_seen": ts_iso, "last_surfaced": ts_iso} for h in hashes}
        (meta_dir / "surfaced.json").write_text(
            json.dumps({"entries": table}), encoding="utf-8"
        )

    def test_no_prior_surfaced_data_is_a_noop(self):
        retrieval.capture_previous_batch_outcome()  # must not raise
        assert retrieval.all_weights() == {}

    def test_clean_session_nudges_the_surfaced_batch_up(self):
        from tools.memory_tool import entry_hash

        h = entry_hash("Clean entry")
        prev_ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        self._seed_surfaced([h], prev_ts)

        retrieval.capture_previous_batch_outcome()

        assert retrieval.get_weight(h) > retrieval.DEFAULT_WEIGHT

    def test_correction_since_prev_batch_nudges_it_down(self):
        from tools.memory_tool import entry_hash
        from agent.learning import outcomes

        h = entry_hash("Bad entry")
        prev_ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        self._seed_surfaced([h], prev_ts)
        outcomes.record("escalation", "voice", "corrected", ref="t1")

        retrieval.capture_previous_batch_outcome()

        assert retrieval.get_weight(h) < retrieval.DEFAULT_WEIGHT

    def test_correction_only_nudges_the_actually_surfaced_hashes(self):
        """A correction after the most-recent batch must not affect an
        OLDER, unrelated batch's entries -- only the entries that were
        actually surfaced most recently move."""
        from tools.memory_tool import entry_hash
        from agent.learning import outcomes

        old_h = entry_hash("Older unrelated entry")
        new_h = entry_hash("Newer surfaced entry")

        old_ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        new_ts = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()

        from tools.memory_tool import get_memory_meta_dir

        meta_dir = get_memory_meta_dir()
        meta_dir.mkdir(parents=True, exist_ok=True)
        table = {
            old_h: {"first_seen": old_ts, "last_surfaced": old_ts},
            new_h: {"first_seen": new_ts, "last_surfaced": new_ts},
        }
        (meta_dir / "surfaced.json").write_text(json.dumps({"entries": table}), encoding="utf-8")

        outcomes.record("escalation", "voice", "corrected", ref="t2")

        retrieval.capture_previous_batch_outcome()

        assert retrieval.get_weight(new_h) < retrieval.DEFAULT_WEIGHT
        assert retrieval.get_weight(old_h) == retrieval.DEFAULT_WEIGHT

    def test_correction_before_prev_batch_does_not_count(self):
        """A correction recorded BEFORE the batch was even surfaced is
        stale evidence -- must not trigger a down-nudge."""
        from tools.memory_tool import entry_hash
        from agent.learning import outcomes

        old_correction_at = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        outcomes.record("escalation", "voice", "corrected", ref="t3", at=old_correction_at)

        h = entry_hash("Entry surfaced after the correction")
        prev_ts = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        self._seed_surfaced([h], prev_ts)

        retrieval.capture_previous_batch_outcome()

        assert retrieval.get_weight(h) > retrieval.DEFAULT_WEIGHT  # treated as clean

    def test_adaptive_disabled_skips_capture(self, monkeypatch):
        from tools.memory_tool import entry_hash

        monkeypatch.setattr(
            retrieval, "retrieval_config", lambda config=None: {"adaptive": False, "learning_rate": 0.1}
        )
        h = entry_hash("Should not move")
        prev_ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        self._seed_surfaced([h], prev_ts)

        retrieval.capture_previous_batch_outcome()

        assert retrieval.get_weight(h) == retrieval.DEFAULT_WEIGHT

    def test_never_raises_when_outcomes_ledger_blows_up(self, monkeypatch):
        from tools.memory_tool import entry_hash

        h = entry_hash("Degrade safely")
        prev_ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        self._seed_surfaced([h], prev_ts)

        def _boom(*args, **kwargs):
            raise RuntimeError("ledger on fire")

        monkeypatch.setattr("agent.learning.outcomes.recent", _boom)

        retrieval.capture_previous_batch_outcome()  # must not raise
        # Degrades to "not corrected" -> nudged up.
        assert retrieval.get_weight(h) > retrieval.DEFAULT_WEIGHT

    def test_never_raises_on_corrupt_surfaced_sidecar(self):
        from tools.memory_tool import get_memory_meta_dir

        meta_dir = get_memory_meta_dir()
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / "surfaced.json").write_text("{not valid json", encoding="utf-8")

        retrieval.capture_previous_batch_outcome()  # must not raise
        assert retrieval.all_weights() == {}


# ---------------------------------------------------------------------------
# MemoryStore integration -- the actual render hook + golden test.
# ---------------------------------------------------------------------------


class TestMemoryStoreRenderIntegration:
    def test_adaptive_false_golden_ordering_unchanged(self, monkeypatch):
        """End-to-end golden test: with memory.retrieval.adaptive off, the
        rendered system-prompt snapshot preserves the EXACT pre-Loop-4
        insertion order, no matter what usefulness weights are on file."""
        from tools.memory_tool import MemoryStore, entry_hash

        monkeypatch.setattr(
            retrieval, "retrieval_config", lambda config=None: {"adaptive": False, "learning_rate": 0.1}
        )

        store = MemoryStore(memory_char_limit=2000, user_char_limit=1000)
        store.add("memory", "Alpha fact")
        store.add("memory", "Beta fact")
        store.add("memory", "Gamma fact")

        # Weights that WOULD reorder things if adaptive were on.
        retrieval.nudge(entry_hash("Gamma fact"), 1.0)
        retrieval.nudge(entry_hash("Alpha fact"), -0.5)

        store.load_from_disk()
        snapshot = store.format_for_system_prompt("memory")

        assert snapshot.index("Alpha fact") < snapshot.index("Beta fact") < snapshot.index("Gamma fact")

    def test_adaptive_true_reorders_snapshot_by_weight(self):
        from tools.memory_tool import MemoryStore, entry_hash

        store = MemoryStore(memory_char_limit=2000, user_char_limit=1000)
        store.add("memory", "Alpha fact")
        store.add("memory", "Beta fact")
        store.add("memory", "Gamma fact")

        retrieval.nudge(entry_hash("Gamma fact"), 0.5)

        store.load_from_disk()
        snapshot = store.format_for_system_prompt("memory")

        assert snapshot.index("Gamma fact") < snapshot.index("Alpha fact")
        assert snapshot.index("Alpha fact") < snapshot.index("Beta fact")

    def test_ranking_never_mutates_live_store_or_drops_entries(self):
        from tools.memory_tool import MemoryStore, entry_hash

        store = MemoryStore(memory_char_limit=2000, user_char_limit=1000)
        store.add("memory", "Alpha fact")
        store.add("memory", "Beta fact")
        for _ in range(20):
            retrieval.nudge(entry_hash("Alpha fact"), -1.0)

        store.load_from_disk()
        snapshot = store.format_for_system_prompt("memory")

        assert "Alpha fact" in store.memory_entries
        assert "Beta fact" in store.memory_entries
        assert "Alpha fact" in snapshot
        assert "Beta fact" in snapshot

    def test_never_raises_when_retrieval_module_is_broken(self, monkeypatch):
        from tools.memory_tool import MemoryStore

        def _boom(*args, **kwargs):
            raise RuntimeError("retrieval on fire")

        monkeypatch.setattr("agent.memory.retrieval.rank_entries", _boom)

        store = MemoryStore(memory_char_limit=2000, user_char_limit=1000)
        store.add("memory", "Resilient fact")
        store.load_from_disk()  # must not raise

        assert "Resilient fact" in store.format_for_system_prompt("memory")
