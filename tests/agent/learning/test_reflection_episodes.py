"""Reflection-input wiring: the nightly reflection prompt gains a compact
"recent episodes" block (Loop 1, memory-maturity spec §1.4)."""

from __future__ import annotations

from agent.learning import reflection
from agent.memory import episodic


class TestEpisodesForPrompt:
    def test_empty_store_returns_no_meaningful_episodes_message(self):
        assert reflection.episodes_for_prompt() == "No meaningful recent episodes."

    def test_includes_episodes_above_importance_threshold(self):
        episodic.record_episode(
            kind="task", title="Important thing", summary="Worth remembering.",
            source="s", ref="1", importance=0.8,
        )
        episodic.record_episode(
            kind="task", title="Trivial thing", source="s", ref="2", importance=0.1,
        )

        block = reflection.episodes_for_prompt()

        assert "Important thing" in block
        assert "Trivial thing" not in block

    def test_respects_custom_min_importance_from_config(self):
        episodic.record_episode(kind="task", title="Medium thing", source="s", ref="1", importance=0.5)

        cfg = {"memory": {"episodic": {"enabled": True, "retain_days": 400, "min_importance_for_prompt": 0.6}}}
        assert "Medium thing" not in reflection.episodes_for_prompt(cfg)

        cfg_lenient = {"memory": {"episodic": {"enabled": True, "retain_days": 400, "min_importance_for_prompt": 0.3}}}
        assert "Medium thing" in reflection.episodes_for_prompt(cfg_lenient)

    def test_limit_caps_number_of_lines(self):
        for i in range(20):
            episodic.record_episode(kind="task", title=f"item-{i}", source="s", ref=str(i), importance=0.9)

        block = reflection.episodes_for_prompt(limit=5)
        assert len(block.splitlines()) == 5

    def test_disabled_episodic_memory_returns_disabled_message(self):
        cfg = {"memory": {"episodic": {"enabled": False, "retain_days": 400, "min_importance_for_prompt": 0.4}}}
        assert reflection.episodes_for_prompt(cfg) == "Episodic memory disabled."

    def test_never_raises_on_query_failure(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(episodic, "query", _boom)
        assert reflection.episodes_for_prompt() == "Recent episodes unavailable."
