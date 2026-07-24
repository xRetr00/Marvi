"""Tests for cron/scheduler.py's autonomy hook
(``_run_subconscious_autonomy_requests`` — Marvi freedom spec §1.2/§1.4):
parsing <research>/<ask> blocks, spawn-gated-by-budget with a fake delegate,
per-run spawn caps, writing research answers back to the narrative/graph,
and the reflection-time pending-question maintenance calls. Everything below
the parse step (budget, research spawn, ask delivery, graph writes) is
faked — no network, no browser, no real LLM, no real subagent.
"""

from __future__ import annotations

import pytest

import cron.scheduler as scheduler


class _FakeAgent:
    pass


@pytest.fixture
def _fake_extract(monkeypatch):
    """Returns a setter the test calls with (research_items, ask_items);
    installs it as cron.subconscious.extract_autonomy_requests."""
    state = {"value": ([], [])}

    def _fake(text):
        return state["value"]

    monkeypatch.setattr("cron.subconscious.extract_autonomy_requests", _fake)

    def _set(research, ask):
        state["value"] = (research, ask)

    return _set


@pytest.fixture
def _fake_maintenance(monkeypatch):
    """Stub the always-run maintenance calls so tests that don't care about
    them aren't coupled to their internals."""
    monkeypatch.setattr("agent.autonomy.ask.reconcile_pending_questions", lambda **kw: 0)
    monkeypatch.setattr("agent.autonomy.ask.expire_stale_questions", lambda **kw: 0)
    monkeypatch.setattr("agent.autonomy.ask.surface_graph_contradictions", lambda **kw: 0)


@pytest.fixture
def _fake_activity(monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler, "record_subconscious_activity", lambda **kw: calls.append(kw))
    return calls


class TestResearchSpawning:
    def test_spends_budget_and_spawns_for_each_research_request(
        self, monkeypatch, _fake_extract, _fake_maintenance, _fake_activity
    ):
        _fake_extract([{"question": "Q1", "why": "w1"}], [])

        spend_calls = []
        monkeypatch.setattr(
            "agent.autonomy.budget.try_spend", lambda cat: spend_calls.append(cat) or True
        )
        run_calls = []
        monkeypatch.setattr(
            "agent.autonomy.research.run_research_question",
            lambda question, why, **kw: run_calls.append((question, why)) or {"answer": "Yes.", "status": "completed"},
        )
        narrative_writes = []
        monkeypatch.setattr("cron.subconscious.read_narrative", lambda: "existing model")
        monkeypatch.setattr("cron.subconscious.write_narrative", lambda text: narrative_writes.append(text))
        graph_writes = []
        monkeypatch.setattr(
            "agent.memory.graph_builder.record_from_memory_entry",
            lambda text, topic=None: graph_writes.append((text, topic)),
        )

        scheduler._run_subconscious_autonomy_requests({"id": "job-1"}, _FakeAgent(), "raw text")

        assert spend_calls == ["research"]
        assert run_calls == [("Q1", "w1")]
        assert len(narrative_writes) == 1
        assert "Q1" in narrative_writes[0]
        assert "Yes." in narrative_writes[0]
        assert len(graph_writes) == 1
        assert any(row["source"] == "autonomy" for row in _fake_activity)

    def test_budget_exhausted_skips_spawn(self, monkeypatch, _fake_extract, _fake_maintenance):
        _fake_extract([{"question": "Q1"}], [])
        monkeypatch.setattr("agent.autonomy.budget.try_spend", lambda cat: False)
        run_calls = []
        monkeypatch.setattr(
            "agent.autonomy.research.run_research_question",
            lambda *a, **kw: run_calls.append(1) or None,
        )

        scheduler._run_subconscious_autonomy_requests({"id": "job-1"}, _FakeAgent(), "raw text")

        assert run_calls == []

    def test_capped_at_two_spawns_per_run_even_with_more_requests(
        self, monkeypatch, _fake_extract, _fake_maintenance
    ):
        _fake_extract(
            [{"question": f"Q{i}"} for i in range(5)],
            [],
        )
        monkeypatch.setattr("agent.autonomy.budget.try_spend", lambda cat: True)
        run_calls = []
        monkeypatch.setattr(
            "agent.autonomy.research.run_research_question",
            lambda question, why, **kw: run_calls.append(question) or None,
        )

        scheduler._run_subconscious_autonomy_requests({"id": "job-1"}, _FakeAgent(), "raw text")

        assert len(run_calls) == 2

    def test_missing_question_is_skipped_without_spending(self, monkeypatch, _fake_extract, _fake_maintenance):
        _fake_extract([{"why": "no question field"}], [])
        spend_calls = []
        monkeypatch.setattr("agent.autonomy.budget.try_spend", lambda cat: spend_calls.append(cat) or True)

        scheduler._run_subconscious_autonomy_requests({"id": "job-1"}, _FakeAgent(), "raw text")

        assert spend_calls == []

    def test_never_raises_when_research_spawn_throws(self, monkeypatch, _fake_extract, _fake_maintenance):
        _fake_extract([{"question": "Q1"}], [])
        monkeypatch.setattr("agent.autonomy.budget.try_spend", lambda cat: True)

        def _boom(*a, **kw):
            raise RuntimeError("delegate exploded")

        monkeypatch.setattr("agent.autonomy.research.run_research_question", _boom)

        # Must not raise.
        scheduler._run_subconscious_autonomy_requests({"id": "job-1"}, _FakeAgent(), "raw text")


class TestAskSpawning:
    def test_asks_for_each_ask_request_up_to_cap(self, monkeypatch, _fake_extract, _fake_maintenance):
        _fake_extract([], [{"question": f"A{i}", "why": "w"} for i in range(5)])
        ask_calls = []
        monkeypatch.setattr(
            "agent.autonomy.ask.ask_user",
            lambda question, why="", category="general", **kw: ask_calls.append(question) or {"id": "x"},
        )

        scheduler._run_subconscious_autonomy_requests({"id": "job-1"}, _FakeAgent(), "raw text")

        assert len(ask_calls) == 2

    def test_ask_user_declining_does_not_count_toward_cap_incorrectly(
        self, monkeypatch, _fake_extract, _fake_maintenance
    ):
        """Even if ask_user returns None (skipped) for some, the loop still
        respects the per-run cap on ATTEMPTS, not just successes."""
        _fake_extract([], [{"question": f"A{i}"} for i in range(5)])
        attempted = []
        monkeypatch.setattr(
            "agent.autonomy.ask.ask_user",
            lambda question, why="", category="general", **kw: attempted.append(question) or None,
        )

        scheduler._run_subconscious_autonomy_requests({"id": "job-1"}, _FakeAgent(), "raw text")

        assert len(attempted) == 2


class TestMaintenanceAlwaysRuns:
    def test_reconcile_and_expire_and_contradictions_run_every_time(self, monkeypatch, _fake_extract):
        _fake_extract([], [])
        calls = {"reconcile": 0, "expire": 0, "contradictions": 0}
        monkeypatch.setattr(
            "agent.autonomy.ask.reconcile_pending_questions",
            lambda **kw: calls.__setitem__("reconcile", calls["reconcile"] + 1) or 0,
        )
        monkeypatch.setattr(
            "agent.autonomy.ask.expire_stale_questions",
            lambda **kw: calls.__setitem__("expire", calls["expire"] + 1) or 0,
        )
        monkeypatch.setattr(
            "agent.autonomy.ask.surface_graph_contradictions",
            lambda **kw: calls.__setitem__("contradictions", calls["contradictions"] + 1) or 0,
        )

        scheduler._run_subconscious_autonomy_requests({"id": "job-1"}, _FakeAgent(), "raw text")

        assert calls == {"reconcile": 1, "expire": 1, "contradictions": 1}

    def test_maintenance_failure_never_raises(self, monkeypatch, _fake_extract):
        _fake_extract([], [])
        monkeypatch.setattr(
            "agent.autonomy.ask.reconcile_pending_questions",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        scheduler._run_subconscious_autonomy_requests({"id": "job-1"}, _FakeAgent(), "raw text")


class TestExtractionFailureNeverRaises:
    def test_broken_extract_function_is_swallowed(self, monkeypatch, _fake_maintenance):
        monkeypatch.setattr(
            "cron.subconscious.extract_autonomy_requests",
            lambda text: (_ for _ in ()).throw(RuntimeError("regex exploded")),
        )
        scheduler._run_subconscious_autonomy_requests({"id": "job-1"}, _FakeAgent(), "raw text")
