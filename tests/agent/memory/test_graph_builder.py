"""Tests for graph population (graph-mind spec §2.3,
``docs/superpowers/specs/2026-07-20-marvi-freedom-and-graph-mind-spec.md``).

Covers the cheap always-on path (``record_from_episode`` /
``record_from_memory_entry`` -- no LLM) and the bounded, idempotent,
aux-model-assisted batch pass (``build_graph_from_memory``, with the
auxiliary LLM call faked -- no real network/model calls in this suite).
"""

from __future__ import annotations

import json

from agent.memory import episodic, graph, graph_builder


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


def _fake_call_llm_factory(payload):
    calls = {"n": 0}

    def _fake_call_llm(*, task, messages, **kwargs):
        calls["n"] += 1
        content = payload if isinstance(payload, str) else json.dumps(payload)
        return _FakeResponse(content)

    return _fake_call_llm, calls


class TestRecordFromEpisode:
    def test_creates_event_node_with_mention_edges(self):
        node_id = graph_builder.record_from_episode(
            {"id": 7, "title": "Fixed the build", "summary": "CI was red.", "entities": ["ci", "build"]}
        )
        assert node_id is not None
        node = graph.get_node(node_id)
        assert node["type"] == "event"
        assert node["label"] == "Fixed the build"
        assert node["source_ref"] == "episode:7"

        neigh = graph.neighbors(node_id)
        labels = {n["label"] for n in neigh["nodes"]}
        assert labels == {"ci", "build"}
        assert all(e["relation"] == "mentions" for e in neigh["edges"])

    def test_missing_title_is_a_noop(self):
        assert graph_builder.record_from_episode({"id": 1, "title": ""}) is None
        assert graph.count() == 0

    def test_no_entities_still_creates_event_node(self):
        node_id = graph_builder.record_from_episode({"id": 2, "title": "Something happened"})
        assert node_id is not None
        assert graph.count() == 1

    def test_disabled_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(
            graph, "graph_config",
            lambda config=None: {"enabled": False, "max_nodes": 5000, "inject_neighborhood": True, "build_in_reflection": True},
        )
        assert graph_builder.record_from_episode({"id": 1, "title": "x"}) is None


class TestRecordFromMemoryEntry:
    def test_creates_fact_node_with_topic_edge(self):
        node_id = graph_builder.record_from_memory_entry("[preferences/food] User likes spicy food.")
        assert node_id is not None
        node = graph.get_node(node_id)
        assert node["type"] == "fact"
        assert "spicy" in node["summary"]

        neigh = graph.neighbors(node_id)
        assert len(neigh["nodes"]) == 1
        assert neigh["nodes"][0]["type"] == "topic"
        assert neigh["nodes"][0]["label"] == "preferences/food"
        assert neigh["edges"][0]["relation"] == "part_of"

    def test_explicit_topic_overrides_parsed_topic(self):
        graph_builder.record_from_memory_entry("plain entry, no bracket topic", topic="custom-topic")
        results = graph.query(type="topic")
        assert any(n["label"] == "custom-topic" for n in results)

    def test_empty_text_is_a_noop(self):
        assert graph_builder.record_from_memory_entry("") is None
        assert graph.count() == 0

    def test_is_idempotent_by_source_ref(self):
        first = graph_builder.record_from_memory_entry("[topic] same text")
        second = graph_builder.record_from_memory_entry("[topic] same text")
        assert first == second
        assert graph.count() == 2  # one fact node + one topic node, not duplicated


class TestBuildGraphFromMemory:
    def test_disabled_returns_immediately_without_any_items(self, monkeypatch):
        monkeypatch.setattr(
            graph, "graph_config",
            lambda config=None: {"enabled": False, "max_nodes": 5000, "inject_neighborhood": True, "build_in_reflection": True},
        )
        result = graph_builder.build_graph_from_memory()
        assert result["enabled"] is False
        assert result["items_processed"] == 0

    def test_no_pending_items_skips_the_aux_call(self, monkeypatch):
        fake, calls = _fake_call_llm_factory({"nodes": [], "edges": []})
        monkeypatch.setattr("agent.auxiliary_client.call_llm", fake)
        result = graph_builder.build_graph_from_memory()
        assert result["items_processed"] == 0
        assert calls["n"] == 0

    def test_extracts_high_confidence_nodes_and_edges(self, monkeypatch):
        episodic.record_episode(kind="task", title="Deployed NeuDocs", source="test", ref="r1")
        payload = {
            "nodes": [
                {"type": "project", "label": "NeuDocs", "summary": "A docs tool."},
                {"type": "person", "label": "Shereef", "summary": "The user."},
            ],
            "edges": [
                {"src": "Shereef", "dst": "NeuDocs", "relation": "works_on", "confidence": 0.9},
            ],
        }
        fake, calls = _fake_call_llm_factory(payload)
        monkeypatch.setattr("agent.auxiliary_client.call_llm", fake)

        result = graph_builder.build_graph_from_memory()
        assert calls["n"] == 1
        assert result["items_processed"] == 1
        assert result["nodes"] == 2
        assert result["edges"] == 1

        node = graph.find_node("NeuDocs")
        assert node is not None

    def test_low_confidence_edges_are_dropped(self, monkeypatch):
        episodic.record_episode(kind="task", title="Some task", source="test", ref="r1")
        payload = {
            "nodes": [
                {"type": "fact", "label": "a"},
                {"type": "fact", "label": "b"},
            ],
            "edges": [
                {"src": "a", "dst": "b", "relation": "related_to", "confidence": 0.2},
            ],
        }
        fake, _calls = _fake_call_llm_factory(payload)
        monkeypatch.setattr("agent.auxiliary_client.call_llm", fake)

        result = graph_builder.build_graph_from_memory()
        assert result["edges"] == 0

    def test_contradicts_edge_is_written_regardless_of_confidence(self, monkeypatch):
        episodic.record_episode(kind="task", title="Some task", source="test", ref="r1")
        payload = {
            "nodes": [
                {"type": "fact", "label": "works remotely"},
                {"type": "fact", "label": "works in office"},
            ],
            "edges": [
                {
                    "src": "works remotely", "dst": "works in office", "relation": "contradicts",
                    "confidence": 0.3, "note": "conflicting facts",
                },
            ],
        }
        fake, _calls = _fake_call_llm_factory(payload)
        monkeypatch.setattr("agent.auxiliary_client.call_llm", fake)

        result = graph_builder.build_graph_from_memory()
        assert result["edges"] == 1
        assert result["flagged_contradictions"] == 1

    def test_idempotent_cursor_skips_already_ingested_episodes(self, monkeypatch):
        episodic.record_episode(kind="task", title="First task", source="test", ref="r1")
        fake, calls = _fake_call_llm_factory({"nodes": [], "edges": []})
        monkeypatch.setattr("agent.auxiliary_client.call_llm", fake)

        first = graph_builder.build_graph_from_memory()
        assert first["items_processed"] == 1
        assert calls["n"] == 1

        # No new episodes -- the cursor should skip the already-ingested one,
        # so the aux call is never even attempted a second time.
        second = graph_builder.build_graph_from_memory()
        assert second["items_processed"] == 0
        assert calls["n"] == 1

        # A genuinely new episode is picked up on the next call.
        episodic.record_episode(kind="task", title="Second task", source="test", ref="r2")
        third = graph_builder.build_graph_from_memory()
        assert third["items_processed"] == 1
        assert calls["n"] == 2

    def test_aux_call_failure_leaves_cursor_unadvanced(self, monkeypatch):
        episodic.record_episode(kind="task", title="A task", source="test", ref="r1")

        def _boom(*, task, messages, **kwargs):
            raise RuntimeError("no provider configured")

        monkeypatch.setattr("agent.auxiliary_client.call_llm", _boom)

        result = graph_builder.build_graph_from_memory()
        assert result["errors"] == 1
        assert result["items_processed"] == 0

        # The failed batch's items were NOT marked ingested, so a retry with a
        # working aux call still picks them up.
        fake, calls = _fake_call_llm_factory({"nodes": [], "edges": []})
        monkeypatch.setattr("agent.auxiliary_client.call_llm", fake)
        retry = graph_builder.build_graph_from_memory()
        assert retry["items_processed"] == 1
        assert calls["n"] == 1

    def test_malformed_json_output_does_not_raise(self, monkeypatch):
        episodic.record_episode(kind="task", title="A task", source="test", ref="r1")
        fake, _calls = _fake_call_llm_factory("not json at all")
        monkeypatch.setattr("agent.auxiliary_client.call_llm", fake)

        result = graph_builder.build_graph_from_memory()
        assert result["nodes"] == 0
        assert result["edges"] == 0
        # The cursor still advances -- a malformed response shouldn't loop forever.
        assert result["items_processed"] == 1


class TestGraphNeighborhoodForContext:
    def test_returns_empty_for_no_entities(self):
        assert graph_builder.graph_neighborhood_for_context([]) == ""

    def test_returns_empty_when_entity_has_no_graph_node(self):
        assert graph_builder.graph_neighborhood_for_context(["nothing-here"]) == ""

    def test_returns_formatted_block_for_matching_entity(self):
        center = graph.upsert_node(type="project", label="NeuDocs")
        other = graph.upsert_node(type="org", label="bakery-job")
        graph.add_edge(center, other, "funds")

        text = graph_builder.graph_neighborhood_for_context(["NeuDocs"])
        assert "Relevant connections" in text
        assert "NeuDocs" in text
        assert "funds" in text

    def test_disabled_returns_empty(self, monkeypatch):
        graph.upsert_node(type="project", label="NeuDocs")
        monkeypatch.setattr(
            graph, "graph_config",
            lambda config=None: {"enabled": False, "max_nodes": 5000, "inject_neighborhood": True, "build_in_reflection": True},
        )
        assert graph_builder.graph_neighborhood_for_context(["NeuDocs"]) == ""

    def test_inject_neighborhood_false_returns_empty(self, monkeypatch):
        center = graph.upsert_node(type="project", label="NeuDocs")
        other = graph.upsert_node(type="org", label="bakery-job")
        graph.add_edge(center, other, "funds")
        monkeypatch.setattr(
            graph, "graph_config",
            lambda config=None: {"enabled": True, "max_nodes": 5000, "inject_neighborhood": False, "build_in_reflection": True},
        )
        assert graph_builder.graph_neighborhood_for_context(["NeuDocs"]) == ""


class TestMergeDuplicateGraphNodes:
    def test_merges_near_duplicate_labels_within_same_type(self):
        keep = graph.upsert_node(type="project", label="NeuDocs", salience=0.9)
        drop = graph.upsert_node(type="project", label="NeuDoc", salience=0.2)

        merged = graph_builder.merge_duplicate_graph_nodes()
        assert merged == 1
        assert graph.get_node(drop) is None
        assert graph.get_node(keep) is not None

    def test_does_not_merge_dissimilar_labels(self):
        graph.upsert_node(type="project", label="NeuDocs")
        graph.upsert_node(type="project", label="Marvi")

        merged = graph_builder.merge_duplicate_graph_nodes()
        assert merged == 0
        assert graph.count() == 2

    def test_does_not_merge_across_types(self):
        graph.upsert_node(type="project", label="Marvi")
        graph.upsert_node(type="person", label="Marvi's user")

        merged = graph_builder.merge_duplicate_graph_nodes()
        assert merged == 0

    def test_disabled_returns_zero(self, monkeypatch):
        graph.upsert_node(type="project", label="NeuDocs")
        graph.upsert_node(type="project", label="NeuDoc")
        monkeypatch.setattr(
            graph, "graph_config",
            lambda config=None: {"enabled": False, "max_nodes": 5000, "inject_neighborhood": True, "build_in_reflection": True},
        )
        assert graph_builder.merge_duplicate_graph_nodes() == 0
