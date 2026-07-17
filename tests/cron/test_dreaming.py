"""Loop 2 — weekly "dreaming" cross-session consolidation (memory-maturity
spec). Covers the built-in ``Subconscious dreaming`` cron job created
idempotently alongside the tick + reflection, the bounded consolidation-input
assembly and its evidence-threshold gating, the ``source="dreaming"`` activity
attribution, and the guarded hand-off to the (parallel-built) decay pass.

All LLM behaviour is out of scope here — the dreaming job's actual reasoning is
the model's, so these tests exercise the deterministic scaffolding: job
lifecycle, prompt assembly from faked inputs, and the decay seam.
"""

from __future__ import annotations

import sys
import types

import pytest

import cron.scheduler as scheduler
import cron.subconscious as subconscious


@pytest.fixture(autouse=True)
def _isolated_cron_store():
    """Route cron storage to the per-test HERMES_HOME (the hermetic fixture
    isolates HERMES_HOME, but ``cron.jobs`` resolves its store from a
    module-level path captured at import). Without this, ``enable()`` would
    read/write the developer's real ~/.hermes/cron/jobs.json."""
    from cron.jobs import use_cron_store
    from hermes_constants import get_hermes_home

    with use_cron_store(get_hermes_home()):
        yield


# ---------------------------------------------------------------------------
# Job creation idempotency + three-job lifecycle
# ---------------------------------------------------------------------------


def _jobs_by_name():
    from cron.jobs import list_jobs

    jobs = list_jobs(include_disabled=True)
    return {str(j.get("name") or ""): j for j in jobs}


class TestJobLifecycle:
    def test_enable_creates_the_dreaming_job(self):
        subconscious.enable()
        jobs = _jobs_by_name()
        assert subconscious.DREAMING_JOB_NAME in jobs
        dreaming = jobs[subconscious.DREAMING_JOB_NAME]
        # Default weekly schedule (Sunday 04:00), after the 03:30 reflection.
        assert dreaming.get("schedule_display") == subconscious.DEFAULT_DREAMING_SCHEDULE
        assert set(dreaming.get("enabled_toolsets") or []) == set(subconscious._DREAMING_TOOLSETS)

    def test_reenable_does_not_duplicate_any_of_the_three_jobs(self):
        subconscious.enable()
        first = _jobs_by_name()
        subconscious.enable()
        second = _jobs_by_name()

        from cron.jobs import list_jobs

        names = [str(j.get("name") or "") for j in list_jobs(include_disabled=True)]
        # Each built-in name appears exactly once after a second enable().
        for name in (subconscious.JOB_NAME, subconscious.REFLECTION_JOB_NAME, subconscious.DREAMING_JOB_NAME):
            assert names.count(name) == 1
        # And the tracked ids are stable across re-enable.
        assert first[subconscious.DREAMING_JOB_NAME]["id"] == second[subconscious.DREAMING_JOB_NAME]["id"]

    def test_disable_pauses_all_three_jobs(self):
        subconscious.enable()
        subconscious.disable()
        jobs = _jobs_by_name()
        for name in (subconscious.JOB_NAME, subconscious.REFLECTION_JOB_NAME, subconscious.DREAMING_JOB_NAME):
            assert jobs[name].get("state") == "paused"

    def test_status_reports_dreaming_job(self):
        subconscious.enable()
        status = subconscious.status()
        assert status.get("dreaming_job_id")
        assert status.get("dreaming_schedule") == subconscious.DEFAULT_DREAMING_SCHEDULE
        assert status.get("dreaming_job_state") == "scheduled"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestDreamingConfig:
    def test_defaults(self):
        cfg = {}
        conf = subconscious.dreaming_config(cfg)
        assert conf["schedule"] == subconscious.DEFAULT_DREAMING_SCHEDULE
        assert conf["promote_min_occurrences"] == subconscious.DEFAULT_DREAMING_PROMOTE_MIN_OCCURRENCES

    def test_overrides(self):
        cfg = {"memory": {"dreaming": {"schedule": "0 5 * * 0", "promote_min_occurrences": 5}}}
        conf = subconscious.dreaming_config(cfg)
        assert conf["schedule"] == "0 5 * * 0"
        assert conf["promote_min_occurrences"] == 5


# ---------------------------------------------------------------------------
# Input assembly + evidence-threshold gating
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_inputs(monkeypatch):
    """Stub every consolidation input source with small deterministic fakes so
    the assembly is exercised without touching real stores/LLMs."""
    # Episodic: two recent episodes.
    fake_episodes = [
        {"ts": "2026-07-14T09:00:00+00:00", "kind": "task", "actor": "marvi",
         "title": "Deployed NeuDocs", "summary": "Shipped the docs site"},
        {"ts": "2026-07-13T09:00:00+00:00", "kind": "conversation", "actor": "user",
         "title": "Asked about billing", "summary": "How much is the plan"},
    ]
    fake_epi = types.SimpleNamespace(
        query=lambda **kw: list(fake_episodes),
        format_episode=lambda ep: f"- [{ep['ts']}] ({ep['kind']}) {ep['title']}",
    )
    monkeypatch.setitem(sys.modules, "agent.memory.episodic", fake_epi)

    # Outcomes ledger.
    fake_out = types.SimpleNamespace(
        counts=lambda **kw: {"corrected": 2, "accepted": 1},
        recent=lambda **kw: [
            {"at": "2026-07-14T10:00:00+00:00", "loop": "escalation", "event": "corrected", "category": "email"},
        ],
    )
    monkeypatch.setitem(sys.modules, "agent.learning.outcomes", fake_out)

    # Session search (browse shape) — read-only.
    import json as _json

    def _fake_session_search(**kw):
        return _json.dumps({
            "success": True, "mode": "browse",
            "results": [{"title": "Weekly review", "started_at": "2026-07-12", "preview": "Went over goals"}],
            "count": 1,
        })

    fake_ss = types.SimpleNamespace(session_search=_fake_session_search)
    monkeypatch.setitem(sys.modules, "tools.session_search_tool", fake_ss)

    # Semantic memory (read-only MemoryStore snapshot).
    class _FakeStore:
        def load_from_disk(self):
            return None

        def format_for_system_prompt(self, target):
            return f"[{target} memory block]"

    fake_mem = types.SimpleNamespace(MemoryStore=_FakeStore)
    monkeypatch.setitem(sys.modules, "tools.memory_tool", fake_mem)

    # Active goals formatter.
    monkeypatch.setitem(
        sys.modules, "agent.goal_store",
        types.SimpleNamespace(format_active_goals_for_prompt=lambda: "## Active goals\nGoal A"),
    )
    return fake_episodes


class TestInputAssembly:
    def test_blocks_include_all_bounded_sources(self, fake_inputs):
        blocks = subconscious.build_dreaming_context({})
        joined = "\n\n".join(blocks)
        assert "## Episodes (last 7 days)" in joined
        assert "Deployed NeuDocs" in joined
        assert "## Recent sessions" in joined
        assert "Weekly review" in joined
        assert "## Semantic memory" in joined
        assert "[user memory block]" in joined
        assert "## Outcomes ledger" in joined
        assert "corrected=2" in joined
        assert "## Active goals" in joined

    def test_episode_query_is_bounded_to_seven_days(self, monkeypatch, fake_inputs):
        captured = {}

        def _capture_query(**kw):
            captured.update(kw)
            return []

        fake_epi = sys.modules["agent.memory.episodic"]
        monkeypatch.setattr(fake_epi, "query", _capture_query)
        subconscious.build_dreaming_context({})
        # A 7-day since bound and a hard limit must both be passed.
        assert "since" in captured and captured["since"]
        assert captured.get("limit") and captured["limit"] <= 200

    def test_a_failing_source_degrades_to_unavailable_not_crash(self, monkeypatch, fake_inputs):
        def _boom(**kw):
            raise RuntimeError("episodic exploded")

        monkeypatch.setattr(sys.modules["agent.memory.episodic"], "query", _boom)
        blocks = subconscious.build_dreaming_context({})
        joined = "\n\n".join(blocks)
        assert "Recent episodes unavailable." in joined
        # Other blocks still present.
        assert "Weekly review" in joined


class TestThresholdGating:
    def test_default_threshold_appears_in_guidance(self, fake_inputs):
        blocks = subconscious.build_dreaming_context({})
        joined = "\n\n".join(blocks)
        assert "## Consolidation guidance" in joined
        assert "at least 3 times" in joined

    def test_custom_threshold_flows_into_prompt(self, fake_inputs):
        cfg = {"memory": {"dreaming": {"promote_min_occurrences": 5}}}
        blocks = subconscious.build_dreaming_context(cfg)
        joined = "\n\n".join(blocks)
        assert "at least 5 times" in joined

    def test_static_prompt_instructs_evidence_bar_and_direct_writes(self):
        # The consolidation prompt itself carries the "promote only with real
        # evidence" contract and the "write high-confidence facts directly"
        # instruction (LLM behaviour is faked, so we assert on the contract).
        prompt = subconscious._DREAMING_PROMPT
        assert "evidence" in prompt.lower()
        assert "durable memory" in prompt.lower()
        assert "suggestions inbox" in prompt.lower()
        assert "<narrative>" in prompt


# ---------------------------------------------------------------------------
# Activity source attribution
# ---------------------------------------------------------------------------


class TestSourceAttribution:
    def test_dreaming_job_maps_to_dreaming_source(self):
        job = {"name": subconscious.DREAMING_JOB_NAME, "id": "job-dream"}
        assert scheduler._activity_source_for_job(job) == "dreaming"

    def test_dreaming_source_maps_to_task_episode_kind(self):
        assert scheduler._EPISODIC_SOURCE_TO_KIND.get("dreaming") == "task"

    def test_dreaming_activity_mirrors_to_episodic(self):
        from agent.memory import episodic

        scheduler.record_subconscious_activity(
            source="dreaming", outcome="message", job_id="job-dream",
            summary="Consolidated the week",
        )
        rows = episodic.recent(limit=5)
        assert any(r.get("summary") == "Consolidated the week" and r.get("kind") == "task" for r in rows)


# ---------------------------------------------------------------------------
# Decay seam (Loop 3 hand-off) — guarded import
# ---------------------------------------------------------------------------


class TestDecaySeam:
    def test_decay_pass_is_called_when_module_present(self, monkeypatch):
        called = {"n": 0}
        fake_decay = types.SimpleNamespace(run_decay_pass=lambda: called.__setitem__("n", called["n"] + 1))
        monkeypatch.setitem(sys.modules, "agent.memory.decay", fake_decay)
        subconscious.run_decay_pass_after_dreaming()
        assert called["n"] == 1

    def test_missing_decay_module_is_skipped_not_raised(self, monkeypatch):
        # sys.modules[name] = None makes `from agent.memory.decay import ...`
        # raise ImportError — simulating the module not existing yet.
        monkeypatch.setitem(sys.modules, "agent.memory.decay", None)
        # Must not raise.
        subconscious.run_decay_pass_after_dreaming()

    def test_decay_pass_failure_is_swallowed(self, monkeypatch):
        def _boom():
            raise RuntimeError("decay exploded")

        fake_decay = types.SimpleNamespace(run_decay_pass=_boom)
        monkeypatch.setitem(sys.modules, "agent.memory.decay", fake_decay)
        # A failure inside the decay pass must not break the dreaming job.
        subconscious.run_decay_pass_after_dreaming()
