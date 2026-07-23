"""Tests for the graph-mind hooks added to cron/subconscious.py:

- the reflection job's graph-build hook inside ``build_runtime_context``
  (graph-mind spec §2.3 — calls ``agent.memory.graph_builder.build_graph_from_memory``
  behind ``memory.graph.build_in_reflection``);
- the dreaming job's graph-maintenance hook appended after the existing
  decay seam in ``run_decay_pass_after_dreaming`` (spec §2.3 last bullet —
  merge duplicates, then prune beyond ``memory.graph.max_nodes``).

Both hooks are guarded/never-raise by design; these tests exercise that
contract with fakes, mirroring ``tests/cron/test_dreaming.py``'s
``TestDecaySeam`` style for the analogous decay hand-off.
"""

from __future__ import annotations

import sys
import types

import pytest

import cron.subconscious as subconscious
from agent.memory import graph


@pytest.fixture(autouse=True)
def _isolated_cron_store():
    from cron.jobs import use_cron_store
    from hermes_constants import get_hermes_home

    with use_cron_store(get_hermes_home()):
        yield


# ---------------------------------------------------------------------------
# Reflection graph-build hook
# ---------------------------------------------------------------------------


class TestReflectionGraphBuildHook:
    def test_build_runtime_context_calls_the_graph_builder_for_reflection(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(
            "agent.memory.graph_builder.build_graph_from_memory",
            lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"enabled": True},
        )
        monkeypatch.setattr(
            graph, "graph_config",
            lambda config=None: {"enabled": True, "max_nodes": 5000, "inject_neighborhood": True, "build_in_reflection": True},
        )

        subconscious.build_runtime_context(subconscious.REFLECTION_JOB_NAME)

        assert called["n"] == 1

    def test_build_in_reflection_false_skips_the_builder(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(
            "agent.memory.graph_builder.build_graph_from_memory",
            lambda *a, **k: called.__setitem__("n", called["n"] + 1),
        )
        monkeypatch.setattr(
            graph, "graph_config",
            lambda config=None: {"enabled": True, "max_nodes": 5000, "inject_neighborhood": True, "build_in_reflection": False},
        )

        subconscious.build_runtime_context(subconscious.REFLECTION_JOB_NAME)

        assert called["n"] == 0

    def test_tick_job_never_triggers_the_graph_builder(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(
            "agent.memory.graph_builder.build_graph_from_memory",
            lambda *a, **k: called.__setitem__("n", called["n"] + 1),
        )

        subconscious.build_runtime_context(subconscious.JOB_NAME)

        assert called["n"] == 0

    def test_graph_builder_failure_does_not_break_reflection_context(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("graph builder exploded")

        monkeypatch.setattr("agent.memory.graph_builder.build_graph_from_memory", _boom)
        monkeypatch.setattr(
            graph, "graph_config",
            lambda config=None: {"enabled": True, "max_nodes": 5000, "inject_neighborhood": True, "build_in_reflection": True},
        )

        # Must not raise, and must still return the rest of the reflection context.
        context = subconscious.build_runtime_context(subconscious.REFLECTION_JOB_NAME)
        assert "Durable narrative" in context

    def test_missing_graph_module_is_skipped_not_raised(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "agent.memory.graph_builder", None)

        context = subconscious.build_runtime_context(subconscious.REFLECTION_JOB_NAME)
        assert "Durable narrative" in context


# ---------------------------------------------------------------------------
# Dreaming graph-maintenance hook (after the decay seam)
# ---------------------------------------------------------------------------


class TestDreamingGraphMaintenanceHook:
    def test_dreaming_decay_pass_triggers_graph_maintenance(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(subconscious, "_run_graph_dreaming_maintenance", lambda: called.__setitem__("n", called["n"] + 1))
        monkeypatch.setitem(sys.modules, "agent.memory.decay", types.SimpleNamespace(run_decay_pass=lambda: None))

        subconscious.run_decay_pass_after_dreaming()

        assert called["n"] == 1

    def test_graph_maintenance_runs_even_when_decay_module_missing(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(subconscious, "_run_graph_dreaming_maintenance", lambda: called.__setitem__("n", called["n"] + 1))
        monkeypatch.setitem(sys.modules, "agent.memory.decay", None)

        subconscious.run_decay_pass_after_dreaming()

        assert called["n"] == 1

    def test_graph_maintenance_failure_is_swallowed(self, monkeypatch):
        def _boom():
            raise RuntimeError("graph maintenance exploded")

        monkeypatch.setattr(subconscious, "_run_graph_dreaming_maintenance", _boom)
        monkeypatch.setitem(sys.modules, "agent.memory.decay", types.SimpleNamespace(run_decay_pass=lambda: None))

        # Must not raise.
        subconscious.run_decay_pass_after_dreaming()

    def test_maintenance_body_merges_duplicates_then_prunes(self, monkeypatch):
        keep = graph.upsert_node(type="project", label="NeuDocs", salience=0.9)
        drop = graph.upsert_node(type="project", label="NeuDoc", salience=0.2)
        low = graph.upsert_node(type="fact", label="low-salience fact", salience=0.01)
        mid = graph.upsert_node(type="fact", label="mid-salience fact", salience=0.4)

        monkeypatch.setattr(
            graph, "graph_config",
            lambda config=None: {"enabled": True, "max_nodes": 2, "inject_neighborhood": True, "build_in_reflection": True},
        )

        subconscious._run_graph_dreaming_maintenance()

        # The near-duplicate project node was merged away (not just pruned).
        assert graph.get_node(drop) is None
        assert graph.get_node(keep) is not None
        # After the merge (4 -> 3 nodes), pruning brought the store down to
        # the configured cap by archiving the lowest-salience survivor.
        assert graph.count() == 2
        assert graph.get_node(low) is None
        assert graph.get_node(mid) is not None
        assert any(a["label"] == "low-salience fact" for a in graph.archived())

    def test_maintenance_body_disabled_does_not_raise(self, monkeypatch):
        graph.upsert_node(type="fact", label="x")
        monkeypatch.setattr(
            graph, "graph_config",
            lambda config=None: {"enabled": False, "max_nodes": 5000, "inject_neighborhood": True, "build_in_reflection": True},
        )

        # Must return early without touching the store or raising.
        subconscious._run_graph_dreaming_maintenance()

    def test_missing_graph_module_is_skipped_not_raised(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "agent.memory.graph", None)

        # Must not raise.
        subconscious._run_graph_dreaming_maintenance()
