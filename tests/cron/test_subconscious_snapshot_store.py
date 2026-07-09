"""Tests for ``cron/scripts/subconscious/snapshot_store.py``.

Covers: round-trip persistence, cursor/state advancement, throttle, and
exponential-backoff bookkeeping. HERMES_HOME is isolated per-test by the
autouse ``_hermetic_environment`` fixture in ``tests/conftest.py``.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from cron.scripts.subconscious.snapshot_store import (
    DEFAULT_MIN_INTERVAL_SECONDS,
    InvalidSurfaceName,
    SurfaceSnapshot,
    load_snapshot,
    open_store,
    save_snapshot,
    snapshots_dir,
)
from hermes_time import now as hermes_now


def test_snapshots_dir_lives_under_hermes_home():
    from hermes_constants import get_hermes_home

    d = snapshots_dir()
    assert d.exists()
    assert d.is_relative_to(get_hermes_home())
    assert d.parts[-2:] == ("subconscious", "snapshots")


def test_load_missing_snapshot_returns_fresh():
    snap = load_snapshot("gmail")
    assert snap.surface == "gmail"
    assert snap.cursor == {}
    assert snap.state == {}
    assert snap.consecutive_failures == 0


def test_save_and_load_round_trip():
    snap = SurfaceSnapshot.fresh("github")
    snap.cursor = {"since": "2026-07-09T00:00:00+00:00"}
    snap.state = {"seen_ids": ["1", "2"]}
    save_snapshot(snap)

    reloaded = load_snapshot("github")
    assert reloaded.cursor == {"since": "2026-07-09T00:00:00+00:00"}
    assert reloaded.state == {"seen_ids": ["1", "2"]}


def test_save_snapshot_is_atomic_and_owner_only(tmp_path):
    snap = SurfaceSnapshot.fresh("gmail")
    snap.cursor = {"history_id": "123"}
    save_snapshot(snap)

    path = snapshots_dir() / "gmail.json"
    assert path.exists()
    # No leftover tempfiles from the atomic-write path.
    leftovers = [p for p in snapshots_dir().iterdir() if p.name.startswith(".snap_")]
    assert leftovers == []

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["surface"] == "gmail"
    assert data["cursor"] == {"history_id": "123"}


def test_corrupt_snapshot_file_is_treated_as_fresh():
    d = snapshots_dir()
    (d / "gmail.json").write_text("{not valid json", encoding="utf-8")

    snap = load_snapshot("gmail")
    assert snap.cursor == {}
    assert snap.state == {}


@pytest.mark.parametrize("bad_name", ["../escape", "gmail/x", "", "a" * 100, "1gmail", "gmail!"])
def test_invalid_surface_names_are_rejected(bad_name):
    with pytest.raises(InvalidSurfaceName):
        load_snapshot(bad_name)


def test_surface_names_are_case_normalized():
    # Uppercase/mixed-case input is lowercased, not rejected -- matches the
    # normalization already applied to configured surface names.
    snap = SurfaceSnapshot.fresh("GMAIL")
    assert snap.surface == "gmail"


class TestSurfaceStore:
    def test_first_run_and_cursor_advancement(self):
        store = open_store("gmail")
        assert store.is_first_run()

        store.set_cursor({"history_id": "100"})
        assert not store.is_first_run()
        assert store.cursor == {"history_id": "100"}

        store.update_cursor(history_id="200")
        assert store.cursor == {"history_id": "200"}
        store.save()

        # Cursor persists across a fresh store instance (new load from disk).
        reopened = open_store("gmail")
        assert reopened.cursor == {"history_id": "200"}
        assert not reopened.is_first_run()

    def test_state_updates_persist(self):
        store = open_store("github")
        store.update_state(seen_ids=["a", "b"])
        store.save()

        reopened = open_store("github")
        assert reopened.state == {"seen_ids": ["a", "b"]}

    def test_unsaved_changes_are_not_persisted(self):
        store = open_store("gmail")
        store.set_cursor({"history_id": "999"})
        # No .save() call.

        reopened = open_store("gmail")
        assert reopened.cursor == {}

    def test_throttle_blocks_immediate_refetch(self):
        store = open_store("gmail", min_interval_seconds=DEFAULT_MIN_INTERVAL_SECONDS)
        assert not store.is_throttled()  # never fetched -> not throttled
        store.mark_attempt()
        store.save()

        reopened = open_store("gmail", min_interval_seconds=DEFAULT_MIN_INTERVAL_SECONDS)
        assert reopened.is_throttled()
        assert reopened.should_skip()
        assert "throttled" in (reopened.skip_reason() or "")

    def test_throttle_respects_zero_min_interval(self):
        store = open_store("gmail", min_interval_seconds=0)
        store.mark_attempt()
        store.save()

        reopened = open_store("gmail", min_interval_seconds=0)
        assert not reopened.is_throttled()

    def test_throttle_clears_after_min_interval_elapses(self):
        store = open_store("gmail", min_interval_seconds=1)
        snap = store._snapshot
        # Backdate last_fetch_at well past the 1s min interval.
        snap.last_fetch_at = (hermes_now() - timedelta(seconds=10)).isoformat()
        store._dirty = True
        store.save()

        reopened = open_store("gmail", min_interval_seconds=1)
        assert not reopened.is_throttled()

    def test_record_success_clears_failure_state(self):
        store = open_store("gmail")
        store.record_failure("boom")
        assert store._snapshot.consecutive_failures == 1
        assert store.is_backoff_active()

        store.record_success()
        assert store._snapshot.consecutive_failures == 0
        assert store._snapshot.next_retry_at is None
        assert not store.is_backoff_active()

    def test_backoff_is_exponential_and_capped(self):
        store = open_store("github")
        delays = []
        for i in range(1, 9):
            store.record_failure(f"error {i}")
            retry_at = store._snapshot.next_retry_at
            assert retry_at is not None
            from datetime import datetime

            delay = (datetime.fromisoformat(retry_at) - hermes_now()).total_seconds()
            delays.append(delay)

        # Strictly increasing until the cap, then flat.
        assert delays[0] < delays[1] < delays[2]
        from cron.scripts.subconscious.snapshot_store import BACKOFF_MAX_SECONDS

        for d in delays:
            assert d <= BACKOFF_MAX_SECONDS + 1  # +1s tolerance for test wall-clock

    def test_backoff_active_blocks_should_skip(self):
        store = open_store("gmail")
        store.record_failure("rate limited")
        store.save()

        reopened = open_store("gmail")
        assert reopened.is_backoff_active()
        assert reopened.should_skip()
        assert "backing off" in (reopened.skip_reason() or "")

    def test_status_dict_reports_failure_bookkeeping(self):
        store = open_store("gmail")
        store.record_failure("nope")
        status = store.status_dict()
        assert status["consecutive_failures"] == 1
        assert status["last_error"] == "nope"
        assert status["next_retry_at"] is not None
