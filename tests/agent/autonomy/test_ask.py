"""Tests for the ask-user channel (Marvi freedom spec §1.4,
``agent/autonomy/ask.py``): dedup, hard rate-limiting, quiet-in-deep-work,
budget interaction, pending-question correlation, and expiry. Everything
that touches delivery/budget/flow-gate is faked — no network, no browser,
no real LLM.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    from agent.autonomy import ask, budget

    monkeypatch.setattr(ask, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(budget, "get_hermes_home", lambda: tmp_path)
    return ask


@pytest.fixture(autouse=True)
def _fake_delivery(monkeypatch):
    """Every test gets a working, non-gated, non-blocked delivery path by
    default: a fake target, a fake job, no deep-work gating, no activity-log
    dependency. Individual tests override pieces of this to exercise the
    gating/failure paths.
    """
    created_jobs = []

    def _fake_create_job(**kwargs):
        job = {"id": f"job-{len(created_jobs)}", **kwargs}
        created_jobs.append(job)
        return job

    monkeypatch.setattr("agent.autonomy.ask.pick_delivery_target", lambda: "telegram")
    monkeypatch.setattr("cron.jobs.create_job", _fake_create_job)
    monkeypatch.setattr("agent.autonomy.ask._is_deep_work_now", lambda: False)
    monkeypatch.setattr("cron.scheduler.record_subconscious_activity", lambda **kw: None)
    return created_jobs


class TestAskUserBasics:
    def test_ask_user_creates_a_pending_record_and_delivery_job(self, _isolate, _fake_delivery):
        record = _isolate.ask_user("Is the sky blue?", context="curiosity", category="general")

        assert record is not None
        assert record["status"] == "pending"
        assert record["question"] == "Is the sky blue?"
        assert len(_fake_delivery) == 1
        assert _fake_delivery[0]["deliver"] == "telegram"

    def test_empty_question_is_a_noop(self, _isolate, _fake_delivery):
        assert _isolate.ask_user("   ") is None
        assert _fake_delivery == []

    def test_no_delivery_target_skips(self, _isolate, monkeypatch, _fake_delivery):
        monkeypatch.setattr("agent.autonomy.ask.pick_delivery_target", lambda: None)
        assert _isolate.ask_user("Anyone home?") is None
        assert _fake_delivery == []


class TestDedup:
    def test_duplicate_open_question_is_skipped(self, _isolate, _fake_delivery):
        first = _isolate.ask_user("Want a morning brief later?")
        second = _isolate.ask_user("want a morning brief later?")  # different case/whitespace-equivalent

        assert first is not None
        assert second is None
        assert len(_fake_delivery) == 1

    def test_same_question_can_be_asked_again_once_resolved(self, _isolate, _fake_delivery):
        first = _isolate.ask_user("Same question twice")
        assert first is not None

        # Mark it answered directly (simulating reconcile_pending_questions).
        pending = _isolate._load_pending()
        pending["questions"][0]["status"] = "answered"
        _isolate._save_pending(pending)

        second = _isolate.ask_user("Same question twice")
        assert second is not None
        assert len(_fake_delivery) == 2


class TestRateLimit:
    def test_max_per_day_is_a_hard_ceiling(self, _isolate, _fake_delivery, monkeypatch):
        monkeypatch.setattr(
            "agent.autonomy.budget.autonomy_config",
            lambda *a, **kw: {
                "enabled": True,
                "daily_action_budget": 100,
                "per_category": {"research": 100, "browse": 100, "ask_user": 100},
                "ask": {"max_per_day": 2, "quiet_in_deep_work": True},
            },
        )

        assert _isolate.ask_user("Q1") is not None
        assert _isolate.ask_user("Q2") is not None
        assert _isolate.ask_user("Q3") is None
        assert len(_fake_delivery) == 2

    def test_budget_exhaustion_also_blocks_asks(self, _isolate, _fake_delivery, monkeypatch):
        monkeypatch.setattr("agent.autonomy.budget.try_spend", lambda *a, **kw: False)

        assert _isolate.ask_user("Will this go through?") is None
        assert _fake_delivery == []


class TestQuietInDeepWork:
    def test_deferred_when_deep_work_detected(self, _isolate, _fake_delivery, monkeypatch):
        monkeypatch.setattr("agent.autonomy.ask._is_deep_work_now", lambda: True)

        assert _isolate.ask_user("Interrupting?") is None
        assert _fake_delivery == []

    def test_urgent_bypasses_deep_work_check(self, _isolate, _fake_delivery, monkeypatch):
        monkeypatch.setattr("agent.autonomy.ask._is_deep_work_now", lambda: True)

        record = _isolate.ask_user("Urgent!", urgent=True)
        assert record is not None
        assert len(_fake_delivery) == 1

    def test_quiet_in_deep_work_config_off_ignores_deep_work(self, _isolate, _fake_delivery, monkeypatch):
        monkeypatch.setattr("agent.autonomy.ask._is_deep_work_now", lambda: True)
        monkeypatch.setattr(
            "agent.autonomy.budget.autonomy_config",
            lambda *a, **kw: {
                "enabled": True,
                "daily_action_budget": 100,
                "per_category": {"research": 100, "browse": 100, "ask_user": 100},
                "ask": {"max_per_day": 100, "quiet_in_deep_work": False},
            },
        )

        record = _isolate.ask_user("Not urgent but config says go")
        assert record is not None


class TestPendingQuestionCorrelation:
    def test_unrelated_user_activity_is_not_treated_as_an_answer(self, _isolate, _fake_delivery, monkeypatch):
        record = _isolate.ask_user("Should I shift your morning brief?")
        assert record is not None

        monkeypatch.setattr(
            "agent.memory.episodic.query",
            lambda **kw: [{"actor": "user", "summary": "yeah, push it to 9am", "title": "chat"}],
        )

        changed = _isolate.reconcile_pending_questions()
        assert changed == 0

        rows = _isolate.list_pending_questions()
        pending = [r for r in rows if r["id"] == record["id"]][0]
        assert pending["status"] == "pending"

    def test_explicit_answer_uses_question_id(self, _isolate, _fake_delivery):
        record = _isolate.ask_user("Should I shift your morning brief?")
        answered = _isolate.answer_question(record["id"], "Yes, move it to 9am")
        assert answered["status"] == "answered"
        assert answered["answer_text"] == "Yes, move it to 9am"
        assert _isolate.answer_question(record["id"], "again") is None

    def test_reconcile_leaves_question_pending_with_no_user_activity(self, _isolate, _fake_delivery, monkeypatch):
        record = _isolate.ask_user("Still waiting?")
        monkeypatch.setattr("agent.memory.episodic.query", lambda **kw: [])

        changed = _isolate.reconcile_pending_questions()
        assert changed == 0
        rows = _isolate.list_pending_questions()
        assert rows[0]["status"] == "pending"

    def test_reconcile_never_raises_when_episodic_unavailable(self, _isolate, _fake_delivery, monkeypatch):
        _isolate.ask_user("Whatever happens, don't crash")

        def _boom(**kw):
            raise RuntimeError("episodic db locked")

        monkeypatch.setattr("agent.memory.episodic.query", _boom)
        # Must not raise.
        assert _isolate.reconcile_pending_questions() == 0

    def test_reconcile_expires_older_paraphrased_duplicate(self, _isolate, _fake_delivery):
        first = _isolate.ask_user(
            "Is the HE20 false-clear issue still happening in the Smart Room?",
            category="contradiction",
        )
        second = _isolate.ask_user(
            "Does the Smart Room still have the HE20 false-clear issue?",
            category="contradiction",
        )

        assert first is not None and second is not None
        assert _isolate.reconcile_pending_questions() == 1
        rows = {row["id"]: row for row in _isolate.list_pending_questions()}
        assert rows[first["id"]]["status"] == "expired"
        assert rows[second["id"]]["status"] == "pending"


class TestExpiry:
    def test_stale_pending_question_expires(self, _isolate, _fake_delivery):
        record = _isolate.ask_user("Ancient question")
        pending = _isolate._load_pending()
        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        pending["questions"][0]["asked_at"] = old_ts
        _isolate._save_pending(pending)

        changed = _isolate.expire_stale_questions(max_age_days=14)
        assert changed == 1
        rows = _isolate.list_pending_questions()
        assert rows[0]["status"] == "expired"

    def test_fresh_pending_question_is_not_expired(self, _isolate, _fake_delivery):
        _isolate.ask_user("Fresh question")
        changed = _isolate.expire_stale_questions(max_age_days=14)
        assert changed == 0


class TestGraphContradictionSurfacing:
    def test_surfaces_contradicts_edges_as_questions(self, _isolate, _fake_delivery, monkeypatch):
        fake_subgraph = {
            "nodes": [
                {"id": 1, "label": "works at Acme"},
                {"id": 2, "label": "works at Globex"},
            ],
            "edges": [
                {"src": 1, "dst": 2, "relation": "contradicts", "weight": 0.2, "note": "job conflict"},
            ],
        }
        monkeypatch.setattr("agent.memory.graph.top_salience_subgraph", lambda **kw: fake_subgraph)

        asked = _isolate.surface_graph_contradictions(limit=1)
        assert asked == 1
        assert len(_fake_delivery) == 1

    def test_no_contradicts_edges_asks_nothing(self, _isolate, _fake_delivery, monkeypatch):
        monkeypatch.setattr(
            "agent.memory.graph.top_salience_subgraph",
            lambda **kw: {"nodes": [], "edges": []},
        )
        assert _isolate.surface_graph_contradictions(limit=1) == 0
        assert _fake_delivery == []

    def test_resolved_graph_edge_is_not_asked_again(self, _isolate, _fake_delivery, monkeypatch):
        graph = {
            "nodes": [{"id": 1, "label": "A"}, {"id": 2, "label": "B"}],
            "edges": [{"src": 1, "dst": 2, "relation": "contradicts"}],
        }
        monkeypatch.setattr("agent.memory.graph.top_salience_subgraph", lambda **kw: graph)
        assert _isolate.surface_graph_contradictions() == 1
        record = _isolate.list_pending_questions()[0]
        assert _isolate.answer_question(record["id"], "A is current") is not None
        assert _isolate.surface_graph_contradictions() == 0

    def test_never_raises_when_graph_module_broken(self, _isolate, monkeypatch):
        monkeypatch.setattr(
            "agent.memory.graph.top_salience_subgraph",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("db locked")),
        )
        assert _isolate.surface_graph_contradictions(limit=1) == 0
