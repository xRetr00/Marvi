"""Tests for tools/episodic_tool.py's recall_episode registration + formatting.

Registration correctness (module-vs-instance import bug guard) is covered
by ``tests/tools/test_tool_registration_smoke.py``; this file focuses on
the handler's behavior and output shape.
"""

from __future__ import annotations

import json

import tools.episodic_tool  # noqa: F401 - self-registers on import
from agent.memory import episodic
from tools.registry import registry


class TestRegistration:
    def test_recall_episode_is_registered_under_memory_toolset(self):
        assert "recall_episode" in registry.get_all_tool_names()
        assert registry.get_toolset_for_tool("recall_episode") == "memory"

    def test_schema_has_expected_parameters(self):
        schema = registry.get_schema("recall_episode")
        assert schema is not None
        props = schema["parameters"]["properties"]
        assert set(props) == {"query", "kind", "since", "until", "limit"}
        assert set(props["kind"]["enum"]) == episodic.VALID_KINDS


class TestHandlerFormatting:
    def test_empty_store_returns_no_matches_message(self):
        result = json.loads(registry.dispatch("recall_episode", {}))
        assert result["success"] is True
        assert result["count"] == 0
        assert "No matching episodes" in result["formatted"]

    def test_dispatch_returns_formatted_episodes(self):
        episodic.record_episode(
            kind="task", title="Deployed the service", summary="Green build, shipped.", source="s", ref="1",
        )

        result = json.loads(registry.dispatch("recall_episode", {"query": "Deployed"}))

        assert result["success"] is True
        assert result["count"] == 1
        assert "Deployed the service" in result["formatted"]
        assert result["episodes"][0]["title"] == "Deployed the service"

    def test_invalid_kind_returns_error(self):
        result = json.loads(registry.dispatch("recall_episode", {"kind": "bogus"}))
        assert "error" in result

    def test_limit_is_clamped(self):
        for i in range(5):
            episodic.record_episode(kind="task", title=f"item-{i}", source="s", ref=str(i))

        result = json.loads(registry.dispatch("recall_episode", {"limit": 999}))
        assert result["count"] == 5  # only 5 exist; clamp just prevents an absurd request

    def test_kind_filter_applies(self):
        episodic.record_episode(kind="task", title="a task", source="s", ref="1")
        episodic.record_episode(kind="room", title="a room event", source="s", ref="2")

        result = json.loads(registry.dispatch("recall_episode", {"kind": "room"}))
        assert result["count"] == 1
        assert result["episodes"][0]["kind"] == "room"
