"""Tests for the recall_graph tool (graph-mind spec §2.4).

Registration mirrors ``tests/tools/test_episodic_tool.py`` — the
module-vs-instance registration smoke test in
``tests/tools/test_tool_registration_smoke.py`` covers the class of bug this
tool is written to avoid; this file covers the tool's own behavior.
"""

from __future__ import annotations

import json

from agent.memory import graph
from tools import graph_tool
from tools.registry import discover_builtin_tools, registry


class TestRegistration:
    def test_recall_graph_is_registered_with_a_valid_schema(self):
        discover_builtin_tools()
        assert "recall_graph" in registry.get_all_tool_names()
        schema = registry.get_schema("recall_graph")
        assert schema is not None
        assert schema["name"] == "recall_graph"
        assert "query" in schema["parameters"]["properties"]
        assert registry.get_toolset_for_tool("recall_graph") == "memory"


class TestRecallGraphHandler:
    def test_requires_query(self):
        raw = graph_tool._recall_graph({})
        data = json.loads(raw)
        assert "error" in data

    def test_no_matching_node_reports_not_found(self):
        raw = graph_tool._recall_graph({"query": "does-not-exist"})
        data = json.loads(raw)
        assert data["success"] is True
        assert data["found"] is False

    def test_finds_node_and_formats_neighborhood(self):
        center = graph.upsert_node(type="project", label="NeuDocs", summary="A docs tool.")
        other = graph.upsert_node(type="org", label="bakery-job")
        graph.add_edge(center, other, "funds")

        raw = graph_tool._recall_graph({"query": "NeuDocs"})
        data = json.loads(raw)
        assert data["success"] is True
        assert data["found"] is True
        assert data["node"]["label"] == "NeuDocs"
        assert len(data["neighbors"]) == 1
        assert "funds" in data["formatted"]
        assert "bakery-job" in data["formatted"]

    def test_depth_is_clamped(self):
        center = graph.upsert_node(type="project", label="NeuDocs")
        raw = graph_tool._recall_graph({"query": "NeuDocs", "depth": 99})
        data = json.loads(raw)
        assert data["success"] is True  # doesn't raise on an out-of-range depth

    def test_disabled_check_fn_reflects_config(self, monkeypatch):
        monkeypatch.setattr(
            graph, "graph_config",
            lambda config=None: {"enabled": False, "max_nodes": 5000, "inject_neighborhood": True, "build_in_reflection": True},
        )
        assert graph_tool._enabled() is False
