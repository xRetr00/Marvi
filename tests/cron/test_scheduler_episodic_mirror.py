"""Activity-feed -> episodic memory mirror (Loop 1, memory-maturity spec
§1.2): ``cron.scheduler._append_activity_record`` mirrors MEANINGFUL
activity-log entries (outcome message/suggestion) into episodic memory,
mapping activity-log ``source`` to episode ``kind``, and skips quiet
passes plus the distiller (which records its own richer episode)."""

from __future__ import annotations

import cron.scheduler as scheduler
from agent.memory import episodic


def _record(**overrides):
    base = {
        "at": "2026-07-17T10:00:00+00:00",
        "source": "tick",
        "job_id": "job-1",
        "outcome": "message",
        "summary": "Hello from Marvi",
        "diff": None,
        "thought": "Hello from Marvi",
        "narrative_updated": False,
    }
    base.update(overrides)
    return base


class TestMeaningfulOutcomesRecorded:
    def test_message_outcome_creates_episode(self):
        scheduler._append_activity_record(_record(outcome="message"))
        rows = episodic.recent(limit=5)
        assert len(rows) == 1
        assert rows[0]["summary"] == "Hello from Marvi"

    def test_suggestion_outcome_maps_to_learning_kind(self):
        scheduler._append_activity_record(_record(outcome="suggestion", summary="New automation idea", source="tick"))
        rows = episodic.recent(limit=5)
        assert len(rows) == 1
        assert rows[0]["kind"] == "learning"


class TestQuietOutcomesSkipped:
    def test_no_change_outcome_is_not_recorded(self):
        scheduler._append_activity_record(_record(outcome="no_change"))
        assert episodic.count() == 0

    def test_diff_silent_outcome_is_not_recorded(self):
        scheduler._append_activity_record(_record(outcome="diff_silent"))
        assert episodic.count() == 0

    def test_error_outcome_is_not_recorded(self):
        scheduler._append_activity_record(_record(outcome="error", summary="boom"))
        assert episodic.count() == 0


class TestSourceToKindMapping:
    def test_world_source_maps_to_room(self):
        scheduler._append_activity_record(_record(source="world", outcome="message"))
        assert episodic.recent(limit=1)[0]["kind"] == "room"

    def test_goblin_source_maps_to_proactive(self):
        scheduler._append_activity_record(_record(source="goblin", outcome="message"))
        assert episodic.recent(limit=1)[0]["kind"] == "proactive"

    def test_reflection_source_maps_to_task(self):
        scheduler._append_activity_record(_record(source="reflection", outcome="message"))
        assert episodic.recent(limit=1)[0]["kind"] == "task"

    def test_tick_source_maps_to_proactive(self):
        scheduler._append_activity_record(_record(source="tick", outcome="message"))
        assert episodic.recent(limit=1)[0]["kind"] == "proactive"

    def test_distiller_source_is_never_mirrored_here(self):
        """The distiller records its own richer episode directly (see
        tools/presence/distill.py); mirroring it here too would double-record
        the same run under a less informative summary."""
        scheduler._append_activity_record(_record(source="distiller", outcome="message"))
        assert episodic.count() == 0


class TestIdempotency:
    def test_same_job_id_and_timestamp_is_not_duplicated(self):
        record = _record()
        scheduler._append_activity_record(dict(record))
        scheduler._append_activity_record(dict(record))
        assert episodic.count() == 1

    def test_different_timestamp_is_a_new_episode(self):
        scheduler._append_activity_record(_record(at="2026-07-17T10:00:00+00:00"))
        scheduler._append_activity_record(_record(at="2026-07-17T11:00:00+00:00"))
        assert episodic.count() == 2


class TestMirrorNeverBreaksActivityLogging:
    def test_mirror_failure_does_not_prevent_jsonl_write(self, monkeypatch):
        def _boom(record):
            raise RuntimeError("mirror exploded")

        monkeypatch.setattr(scheduler, "_mirror_activity_to_episodic", _boom)

        # Must not raise, and the activity-log line must still be written.
        scheduler._append_activity_record(_record())

        path = scheduler._subconscious_activity_path()
        assert path.exists()
        assert "Hello from Marvi" in path.read_text(encoding="utf-8")
