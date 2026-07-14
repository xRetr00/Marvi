"""Due-ness matrix for cron/subconscious_initiatives.py.

``due_initiatives`` is the pure-ish evaluator (rhythm/presence passed in as
plain values, not callables — see module docstring/spec) that stage-1
(cron/scripts/subconscious_snapshot.py) calls to decide which initiatives
defeat the NO_CHANGE wake gate. This suite exercises every trigger kind,
expiry, budget accounting/reset-by-date, and malformed-entry tolerance named
in the 2026-07-14 Marvi hardening pass.
"""

from __future__ import annotations

from datetime import timedelta

import pytest


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    from cron import subconscious_initiatives as initiatives

    monkeypatch.setattr(initiatives, "get_hermes_home", lambda: tmp_path)
    return initiatives


class TestNextTickTrigger:
    def test_next_tick_is_always_due_within_budget(self, _isolate):
        initiatives = _isolate
        initiatives.add_initiatives([{"detail": "follow up", "trigger": "next_tick"}])

        due = initiatives.due_initiatives()

        assert len(due) == 1
        assert due[0]["detail"] == "follow up"


class TestAtTimeTrigger:
    def test_not_due_before_target_time(self, _isolate):
        initiatives = _isolate
        from hermes_time import now as hermes_now

        future = (hermes_now() + timedelta(hours=1)).isoformat()
        initiatives.add_initiatives(
            [{"detail": "future thing", "trigger": "at_time", "trigger_value": future}]
        )

        assert initiatives.due_initiatives() == []

    def test_due_after_target_time(self, _isolate):
        initiatives = _isolate
        from hermes_time import now as hermes_now

        past = (hermes_now() - timedelta(minutes=5)).isoformat()
        initiatives.add_initiatives(
            [{"detail": "past thing", "trigger": "at_time", "trigger_value": past}]
        )

        due = initiatives.due_initiatives()

        assert len(due) == 1
        assert due[0]["detail"] == "past thing"

    def test_not_due_when_trigger_value_is_unparseable(self, _isolate):
        initiatives = _isolate
        initiatives.add_initiatives(
            [{"detail": "bad time", "trigger": "at_time", "trigger_value": "not-a-date"}]
        )

        assert initiatives.due_initiatives() == []


class TestRhythmTrigger:
    def test_due_when_injected_rhythm_matches_window(self, _isolate):
        initiatives = _isolate
        initiatives.add_initiatives(
            [{"detail": "deep work nudge", "trigger": "on_rhythm", "trigger_value": "deep_work_start"}]
        )

        assert initiatives.due_initiatives(rhythm="deep_work_start")
        assert initiatives.due_initiatives(rhythm="active_end") == []
        assert initiatives.due_initiatives(rhythm=None) == []

    def test_not_due_when_no_rhythm_supplied(self, _isolate):
        initiatives = _isolate
        initiatives.add_initiatives(
            [{"detail": "deep work nudge", "trigger": "on_rhythm", "trigger_value": "deep_work_start"}]
        )

        assert initiatives.due_initiatives() == []


class TestPresenceTrigger:
    def test_due_when_injected_presence_matches_condition(self, _isolate):
        initiatives = _isolate
        initiatives.add_initiatives(
            [{"detail": "idle nudge", "trigger": "on_presence", "trigger_value": "idle"}]
        )

        assert initiatives.due_initiatives(presence="idle")
        assert initiatives.due_initiatives(presence="coding") == []

    def test_not_due_when_no_presence_supplied(self, _isolate):
        initiatives = _isolate
        initiatives.add_initiatives(
            [{"detail": "idle nudge", "trigger": "on_presence", "trigger_value": "idle"}]
        )

        assert initiatives.due_initiatives() == []


class TestExpiry:
    def test_expired_initiative_is_marked_and_excluded(self, _isolate):
        initiatives = _isolate
        from hermes_time import now as hermes_now

        past = (hermes_now() - timedelta(hours=1)).isoformat()
        created = initiatives.add_initiatives(
            [{"detail": "stale follow up", "trigger": "next_tick", "expires_at": past}]
        )

        due = initiatives.due_initiatives()

        assert due == []
        rows = initiatives.list_initiatives()
        assert rows[0]["id"] == created[0]["id"]
        assert rows[0]["status"] == "expired"

    def test_non_expired_initiative_stays_pending(self, _isolate):
        initiatives = _isolate
        from hermes_time import now as hermes_now

        future = (hermes_now() + timedelta(hours=1)).isoformat()
        initiatives.add_initiatives(
            [{"detail": "fresh follow up", "trigger": "next_tick", "expires_at": future}]
        )

        due = initiatives.due_initiatives()

        assert len(due) == 1
        assert initiatives.list_initiatives()[0]["status"] == "pending"


class TestBudget:
    def test_due_list_is_capped_at_daily_budget(self, _isolate):
        initiatives = _isolate
        initiatives.add_initiatives(
            [{"detail": f"item {i}", "trigger": "next_tick"} for i in range(initiatives.MAX_NEW_PER_RUN)]
        )

        due = initiatives.due_initiatives()

        assert len(due) == initiatives.MAX_EXECUTIONS_PER_DAY

    def test_budget_used_reduces_remaining_due_slots(self, _isolate):
        initiatives = _isolate
        created = initiatives.add_initiatives(
            [{"detail": f"item {i}", "trigger": "next_tick"} for i in range(initiatives.MAX_NEW_PER_RUN)]
        )

        initiatives.apply_results([{"id": created[0]["id"], "outcome": "done"}])

        due = initiatives.due_initiatives()

        assert len(due) == initiatives.MAX_EXECUTIONS_PER_DAY - 1

    def test_budget_counter_resets_on_a_new_date(self, _isolate, monkeypatch):
        initiatives = _isolate
        created = initiatives.add_initiatives([{"detail": "item", "trigger": "next_tick"}])
        initiatives.apply_results([{"id": created[0]["id"], "outcome": "done"}])

        state = initiatives.load_state()
        assert state["budget"]["used"] == 1

        # Simulate the next calendar day by rewriting the persisted budget
        # date directly (load_state resets `used` to 0 whenever the stored
        # date no longer matches today's date).
        import json

        raw = json.loads(initiatives.initiatives_path().read_text(encoding="utf-8"))
        raw["budget"]["date"] = "2000-01-01"
        initiatives.initiatives_path().write_text(json.dumps(raw), encoding="utf-8")

        refreshed = initiatives.load_state()

        assert refreshed["budget"]["used"] == 0
        assert refreshed["budget"]["date"] != "2000-01-01"

    def test_budget_used_never_exceeds_max(self, _isolate):
        initiatives = _isolate
        created = initiatives.add_initiatives(
            [{"detail": f"item {i}", "trigger": "next_tick"} for i in range(initiatives.MAX_NEW_PER_RUN)]
        )

        initiatives.apply_results(
            [{"id": row["id"], "outcome": "done"} for row in created]
        )

        state = initiatives.load_state()
        assert state["budget"]["used"] == initiatives.MAX_EXECUTIONS_PER_DAY


class TestMalformedEntries:
    def test_add_initiatives_ignores_blank_detail(self, _isolate):
        initiatives = _isolate
        created = initiatives.add_initiatives([{"detail": "   ", "trigger": "next_tick"}])

        assert created == []
        assert initiatives.list_initiatives() == []

    def test_add_initiatives_ignores_invalid_trigger(self, _isolate):
        initiatives = _isolate
        created = initiatives.add_initiatives([{"detail": "bad trigger", "trigger": "on_full_moon"}])

        assert created == []

    def test_add_initiatives_ignores_missing_detail_key(self, _isolate):
        initiatives = _isolate
        created = initiatives.add_initiatives([{"trigger": "next_tick"}])

        assert created == []

    def test_add_initiatives_caps_at_max_per_run(self, _isolate):
        initiatives = _isolate
        created = initiatives.add_initiatives(
            [{"detail": f"item {i}", "trigger": "next_tick"} for i in range(10)]
        )

        assert len(created) == initiatives.MAX_NEW_PER_RUN

    def test_add_initiatives_dedupes_pending_entries_by_dedup_key(self, _isolate):
        initiatives = _isolate
        initiatives.add_initiatives([{"detail": "same thing", "trigger": "next_tick", "dedup_key": "x"}])
        created_again = initiatives.add_initiatives(
            [{"detail": "same thing but reworded", "trigger": "next_tick", "dedup_key": "x"}]
        )

        assert created_again == []
        assert len(initiatives.list_initiatives()) == 1

    def test_malformed_row_already_in_storage_is_tolerated_by_due_initiatives(self, _isolate):
        """A row written directly to storage (bypassing add_initiatives'
        validation — e.g. a future format change or hand-edited file) with an
        unrecognized trigger must not crash due_initiatives; it's simply
        never due."""
        initiatives = _isolate
        state = initiatives.load_state()
        state["initiatives"].append(
            {
                "id": "malformed1",
                "detail": "mystery trigger",
                "trigger": "on_full_moon",
                "trigger_value": None,
                "expires_at": None,
                "status": "pending",
            }
        )
        initiatives._save(state)

        assert initiatives.due_initiatives() == []

    def test_load_state_recovers_from_corrupt_json(self, _isolate):
        initiatives = _isolate
        initiatives.initiatives_path().parent.mkdir(parents=True, exist_ok=True)
        initiatives.initiatives_path().write_text("{not valid json", encoding="utf-8")

        state = initiatives.load_state()

        assert state["initiatives"] == []
        assert state["budget"]["used"] == 0

    def test_load_state_recovers_from_non_dict_json(self, _isolate):
        initiatives = _isolate
        initiatives.initiatives_path().parent.mkdir(parents=True, exist_ok=True)
        initiatives.initiatives_path().write_text("[1, 2, 3]", encoding="utf-8")

        state = initiatives.load_state()

        assert state["initiatives"] == []


class TestCancel:
    def test_cancel_initiative_marks_cancelled_and_excludes_from_due(self, _isolate):
        initiatives = _isolate
        created = initiatives.add_initiatives([{"detail": "cancel me", "trigger": "next_tick"}])

        assert initiatives.cancel_initiative(created[0]["id"]) is True
        assert initiatives.due_initiatives() == []
        assert initiatives.list_initiatives(status="cancelled")[0]["id"] == created[0]["id"]

    def test_cancel_unknown_id_returns_false(self, _isolate):
        initiatives = _isolate

        assert initiatives.cancel_initiative("does-not-exist") is False


class TestApplyResults:
    def test_retry_outcome_leaves_initiative_pending(self, _isolate):
        initiatives = _isolate
        created = initiatives.add_initiatives([{"detail": "retry me", "trigger": "next_tick"}])

        initiatives.apply_results([{"id": created[0]["id"], "outcome": "retry"}])

        row = initiatives.list_initiatives()[0]
        assert row["status"] == "pending"
        state = initiatives.load_state()
        assert state["budget"]["used"] == 0

    def test_unknown_outcome_is_ignored(self, _isolate):
        initiatives = _isolate
        created = initiatives.add_initiatives([{"detail": "weird outcome", "trigger": "next_tick"}])

        initiatives.apply_results([{"id": created[0]["id"], "outcome": "teleport"}])

        assert initiatives.list_initiatives()[0]["status"] == "pending"

    def test_result_for_unknown_id_is_ignored(self, _isolate):
        initiatives = _isolate
        initiatives.add_initiatives([{"detail": "item", "trigger": "next_tick"}])

        # Should not raise even though the id doesn't exist.
        initiatives.apply_results([{"id": "ghost", "outcome": "done"}])

        assert initiatives.list_initiatives()[0]["status"] == "pending"
