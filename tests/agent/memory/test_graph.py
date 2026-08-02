"""Tests for the graph memory store (graph-mind spec §2.2,
``docs/superpowers/specs/2026-07-20-marvi-freedom-and-graph-mind-spec.md``).

HERMES_HOME is isolated to a per-test tempdir by the autouse
``_hermetic_environment`` fixture in ``tests/conftest.py``, so every test
here gets a fresh ``graph.db``.
"""

from __future__ import annotations

from agent.memory import graph


def _make_star():
    """NeuDocs --funds--> bakery-job, NeuDocs --works_on--> Shereef."""
    center = graph.upsert_node(type="project", label="NeuDocs")
    a = graph.upsert_node(type="org", label="bakery-job")
    b = graph.upsert_node(type="person", label="Shereef")
    graph.add_edge(center, a, "funds")
    graph.add_edge(center, b, "works_on")
    return center, a, b


class TestUpsertNode:
    def test_upsert_creates_a_new_node_and_returns_an_id(self):
        node_id = graph.upsert_node(type="project", label="NeuDocs", summary="A docs tool.")
        assert isinstance(node_id, int)
        node = graph.get_node(node_id)
        assert node["type"] == "project"
        assert node["label"] == "NeuDocs"
        assert node["summary"] == "A docs tool."

    def test_upsert_dedups_by_type_and_normalized_label(self):
        first = graph.upsert_node(type="project", label="NeuDocs")
        second = graph.upsert_node(type="project", label="  neudocs  ")
        assert first == second
        assert graph.count() == 1

    def test_same_label_different_type_is_a_distinct_node(self):
        first = graph.upsert_node(type="project", label="Marvi")
        second = graph.upsert_node(type="person", label="Marvi")
        assert first != second
        assert graph.count() == 2

    def test_repeat_upsert_bumps_salience_and_keeps_new_summary(self):
        node_id = graph.upsert_node(type="topic", label="bakery", summary="", salience=0.5)
        graph.upsert_node(type="topic", label="bakery", summary="Runs a bakery job.", salience=0.5)
        node = graph.get_node(node_id)
        assert node["summary"] == "Runs a bakery job."
        assert node["salience"] > 0.5

    def test_repeat_upsert_without_new_summary_keeps_existing_summary(self):
        node_id = graph.upsert_node(type="topic", label="bakery", summary="Runs a bakery job.")
        graph.upsert_node(type="topic", label="bakery")
        node = graph.get_node(node_id)
        assert node["summary"] == "Runs a bakery job."

    def test_upsert_rejects_empty_label(self):
        assert graph.upsert_node(type="fact", label="   ") is None
        assert graph.count() == 0

    def test_upsert_falls_back_to_fact_for_invalid_type(self):
        node_id = graph.upsert_node(type="not-a-type", label="something")
        node = graph.get_node(node_id)
        assert node["type"] == "fact"

    def test_upsert_disabled_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(
            graph, "graph_config",
            lambda config=None: {"enabled": False, "max_nodes": 5000, "inject_neighborhood": True, "build_in_reflection": True},
        )
        assert graph.upsert_node(type="fact", label="x") is None
        assert graph.count() == 0


class TestAddEdge:
    def test_add_edge_creates_edge_between_two_nodes(self):
        a = graph.upsert_node(type="project", label="NeuDocs")
        b = graph.upsert_node(type="project", label="Marvi")
        edge_id = graph.add_edge(a, b, "built_with")
        assert isinstance(edge_id, int)
        neigh = graph.neighbors(a)
        assert len(neigh["edges"]) == 1
        assert neigh["edges"][0]["relation"] == "built_with"

    def test_add_edge_dedups_by_src_dst_relation_and_bumps_weight(self):
        a = graph.upsert_node(type="project", label="NeuDocs")
        b = graph.upsert_node(type="project", label="Marvi")
        first = graph.add_edge(a, b, "built_with", weight=1.0)
        second = graph.add_edge(a, b, "built_with", weight=1.0)
        assert first == second
        neigh = graph.neighbors(a)
        assert len(neigh["edges"]) == 1
        assert neigh["edges"][0]["weight"] == 2.0

    def test_add_edge_rejects_self_loop(self):
        a = graph.upsert_node(type="project", label="NeuDocs")
        assert graph.add_edge(a, a, "related_to") is None

    def test_add_edge_rejects_missing_endpoints(self):
        a = graph.upsert_node(type="project", label="NeuDocs")
        assert graph.add_edge(a, 999999, "related_to") is None

    def test_add_edge_accepts_extensible_relation_name(self):
        a = graph.upsert_node(type="project", label="NeuDocs")
        b = graph.upsert_node(type="project", label="Marvi")
        # Not in the canonical VALID_RELATIONS set -- extensible enum (spec §2.2).
        edge_id = graph.add_edge(a, b, "inspired_by")
        assert edge_id is not None

    def test_contradicts_edge_defaults_to_low_weight(self):
        a = graph.upsert_node(type="fact", label="works remotely")
        b = graph.upsert_node(type="fact", label="works in office")
        edge_id = graph.add_edge(a, b, "contradicts", weight=None, note="conflicting facts")
        assert edge_id is not None
        neigh = graph.neighbors(a)
        edge = neigh["edges"][0]
        assert edge["weight"] == graph._CONTRADICTS_DEFAULT_WEIGHT
        assert edge["note"] == "conflicting facts"


class TestNodeMutations:
    def test_edit_updates_node_and_search_index(self):
        node_id = graph.upsert_node(type="fact", label="Old label", summary="Old summary", salience=0.2)

        updated = graph.edit_node(
            node_id,
            type="project",
            label="New label",
            summary="New summary",
            salience=0.8,
        )

        assert updated is not None
        assert updated["type"] == "project"
        assert updated["label"] == "New label"
        assert updated["summary"] == "New summary"
        assert updated["salience"] == 0.8
        assert graph.query(text="New summary")[0]["id"] == node_id
        assert graph.query(text="Old summary") == []

    def test_delete_archives_node_and_removes_its_edges(self):
        node_id, neighbor_id, _ = _make_star()

        assert graph.delete_node(node_id) is True
        assert graph.get_node(node_id) is None
        assert graph.neighbors(neighbor_id)["edges"] == []
        assert any(row["orig_node_id"] == node_id and row["reason"] == "deleted by user" for row in graph.archived())


class TestQuery:
    def test_query_by_type_orders_by_salience(self):
        graph.upsert_node(type="fact", label="low", salience=0.1)
        graph.upsert_node(type="fact", label="high", salience=0.9)
        results = graph.query(type="fact", limit=10)
        assert [r["label"] for r in results] == ["high", "low"]

    def test_query_by_text_uses_fts(self):
        graph.upsert_node(type="project", label="NeuDocs", summary="documentation tool")
        graph.upsert_node(type="project", label="Marvi", summary="an assistant")
        results = graph.query(text="documentation")
        assert any(r["label"] == "NeuDocs" for r in results)
        assert not any(r["label"] == "Marvi" for r in results)

    def test_find_node_exact_match(self):
        node_id = graph.upsert_node(type="project", label="NeuDocs")
        found = graph.find_node("neudocs")
        assert found is not None
        assert found["id"] == node_id

    def test_find_node_missing_returns_none(self):
        assert graph.find_node("does-not-exist") is None


class TestNeighborsAndSubgraph:
    def test_neighbors_excludes_center_node(self):
        center, a, b = _make_star()
        neigh = graph.neighbors(center, depth=1)
        ids = {n["id"] for n in neigh["nodes"]}
        assert center not in ids
        assert ids == {a, b}
        assert len(neigh["edges"]) == 2

    def test_subgraph_includes_center_node(self):
        center, a, b = _make_star()
        sub = graph.subgraph(center, depth=1)
        ids = {n["id"] for n in sub["nodes"]}
        assert ids == {center, a, b}

    def test_neighbors_depth_two_reaches_second_hop(self):
        center, a, b = _make_star()
        far = graph.upsert_node(type="topic", label="baking")
        graph.add_edge(a, far, "related_to")
        neigh_depth1 = graph.neighbors(center, depth=1)
        neigh_depth2 = graph.neighbors(center, depth=2)
        assert far not in {n["id"] for n in neigh_depth1["nodes"]}
        assert far in {n["id"] for n in neigh_depth2["nodes"]}

    def test_neighbors_of_isolated_node_is_empty(self):
        lonely = graph.upsert_node(type="fact", label="isolated fact")
        neigh = graph.neighbors(lonely)
        assert neigh == {"nodes": [], "edges": []}

    def test_top_salience_subgraph_orders_and_bounds(self):
        for i in range(5):
            graph.upsert_node(type="fact", label=f"fact-{i}", salience=i / 10)
        result = graph.top_salience_subgraph(limit=2)
        assert len(result["nodes"]) == 2
        assert result["nodes"][0]["salience"] >= result["nodes"][1]["salience"]


class TestFormatNeighborhood:
    def test_formats_relations_as_readable_lines(self):
        center, a, b = _make_star()
        neigh = graph.neighbors(center)
        nodes_by_id = {n["id"]: n for n in neigh["nodes"]}
        text = graph.format_neighborhood(graph.get_node(center), neigh["edges"], nodes_by_id)
        assert "NeuDocs" in text
        assert "funds" in text
        assert "works_on" in text

    def test_formats_empty_neighborhood(self):
        node = graph.upsert_node(type="fact", label="lonely")
        text = graph.format_neighborhood(graph.get_node(node), [], {})
        assert "no recorded connections" in text.lower()


class TestMergeNodes:
    def test_merge_moves_edges_and_archives_the_dropped_node(self):
        keep = graph.upsert_node(type="project", label="NeuDocs", summary="keep summary")
        drop = graph.upsert_node(type="project", label="NeuDoc", summary="drop summary")
        other = graph.upsert_node(type="person", label="Shereef")
        graph.add_edge(drop, other, "works_on")

        assert graph.merge_nodes(keep, drop) is True
        assert graph.get_node(drop) is None
        assert graph.count() == 2  # keep + other

        neigh = graph.neighbors(keep)
        assert any(n["id"] == other for n in neigh["nodes"])

        archived = graph.archived()
        assert any(a["orig_node_id"] == drop and a["label"] == "NeuDoc" for a in archived)

    def test_merge_dedups_edges_created_by_the_repoint(self):
        keep = graph.upsert_node(type="project", label="NeuDocs")
        drop = graph.upsert_node(type="project", label="NeuDoc")
        other = graph.upsert_node(type="person", label="Shereef")
        graph.add_edge(keep, other, "works_on", weight=1.0)
        graph.add_edge(drop, other, "works_on", weight=1.0)

        assert graph.merge_nodes(keep, drop) is True
        neigh = graph.neighbors(keep)
        assert len(neigh["edges"]) == 1
        assert neigh["edges"][0]["weight"] == 2.0

    def test_merge_same_id_is_a_noop(self):
        node_id = graph.upsert_node(type="fact", label="x")
        assert graph.merge_nodes(node_id, node_id) is False

    def test_merge_missing_node_returns_false(self):
        node_id = graph.upsert_node(type="fact", label="x")
        assert graph.merge_nodes(node_id, 999999) is False


class TestPruneLowSalience:
    def test_prune_archives_rather_than_deletes(self):
        graph.upsert_node(type="fact", label="low", salience=0.05)
        graph.upsert_node(type="fact", label="mid", salience=0.5)
        graph.upsert_node(type="fact", label="high", salience=0.95)

        pruned = graph.prune_low_salience(max_nodes=2)
        assert pruned == 1
        assert graph.count() == 2

        remaining_labels = {n["label"] for n in graph.query(type="fact", limit=10)}
        assert remaining_labels == {"mid", "high"}

        archived = graph.archived()
        assert len(archived) == 1
        assert archived[0]["label"] == "low"
        assert archived[0]["summary"] is not None  # text preserved, never lost

    def test_prune_under_cap_is_a_noop(self):
        graph.upsert_node(type="fact", label="a", salience=0.5)
        assert graph.prune_low_salience(max_nodes=10) == 0
        assert graph.count() == 1

    def test_prune_removes_edges_of_pruned_nodes(self):
        low = graph.upsert_node(type="fact", label="low", salience=0.05)
        high = graph.upsert_node(type="fact", label="high", salience=0.95)
        graph.add_edge(low, high, "related_to")

        graph.prune_low_salience(max_nodes=1)
        neigh = graph.neighbors(high)
        assert neigh["edges"] == []


class TestCountAndDisabled:
    def test_count_is_zero_initially(self):
        assert graph.count() == 0

    def test_disabled_reads_return_empty_shapes(self, monkeypatch):
        node_id = graph.upsert_node(type="fact", label="x")
        monkeypatch.setattr(
            graph, "graph_config",
            lambda config=None: {"enabled": False, "max_nodes": 5000, "inject_neighborhood": True, "build_in_reflection": True},
        )
        assert graph.get_node(node_id) is None
        assert graph.query(type="fact") == []
        assert graph.neighbors(node_id) == {"nodes": [], "edges": []}
        assert graph.subgraph(node_id) == {"nodes": [], "edges": []}
