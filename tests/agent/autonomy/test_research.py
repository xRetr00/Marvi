"""Tests for self-directed web research (Marvi freedom spec §1.2,
``agent/autonomy/research.py``). ``tools.delegate_tool``'s child-agent
machinery is faked — no real subagent, no network, no LLM.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


class _FakeParentAgent:
    pass


@pytest.fixture
def _fake_delegate(monkeypatch):
    """Fakes _build_child_agent + _run_single_child so run_research_question
    can be exercised without a real child AIAgent or network call."""
    calls = {"build": [], "run": []}

    def _fake_build(**kwargs):
        calls["build"].append(kwargs)
        return SimpleNamespace(_delegate_role="leaf")

    def _fake_run(**kwargs):
        calls["run"].append(kwargs)
        return {"status": "completed", "summary": "Yes, it recurs monthly."}

    monkeypatch.setattr("tools.delegate_tool._build_child_agent", _fake_build)
    monkeypatch.setattr("tools.delegate_tool._run_single_child", _fake_run)
    return calls


class TestRunResearchQuestion:
    def test_returns_answer_on_success(self, _fake_delegate):
        from agent.autonomy.research import run_research_question

        result = run_research_question(
            "Does the Ziraat pattern recur monthly?",
            "noticed it in episodes",
            parent_agent=_FakeParentAgent(),
        )

        assert result is not None
        assert result["answer"] == "Yes, it recurs monthly."
        assert result["status"] == "completed"

    def test_uses_web_only_toolset_no_writes(self, _fake_delegate):
        from agent.autonomy.research import RESEARCH_TOOLSETS, run_research_question

        run_research_question("Q", "", parent_agent=_FakeParentAgent())

        assert RESEARCH_TOOLSETS == ["web"]
        built_kwargs = _fake_delegate["build"][0]
        assert built_kwargs["toolsets"] == ["web"]
        assert "file" not in built_kwargs["toolsets"]
        assert built_kwargs["role"] == "leaf"

    def test_bounded_iterations(self, _fake_delegate):
        from agent.autonomy.research import RESEARCH_MAX_ITERATIONS, run_research_question

        run_research_question("Q", "", parent_agent=_FakeParentAgent())

        built_kwargs = _fake_delegate["build"][0]
        assert built_kwargs["max_iterations"] == RESEARCH_MAX_ITERATIONS

    def test_empty_question_returns_none(self, _fake_delegate):
        from agent.autonomy.research import run_research_question

        assert run_research_question("   ", "", parent_agent=_FakeParentAgent()) is None
        assert _fake_delegate["build"] == []

    def test_missing_parent_agent_returns_none(self, _fake_delegate):
        from agent.autonomy.research import run_research_question

        assert run_research_question("Q", "", parent_agent=None) is None
        assert _fake_delegate["build"] == []

    def test_empty_summary_returns_none(self, monkeypatch):
        from agent.autonomy.research import run_research_question

        monkeypatch.setattr(
            "tools.delegate_tool._build_child_agent", lambda **kw: SimpleNamespace()
        )
        monkeypatch.setattr(
            "tools.delegate_tool._run_single_child",
            lambda **kw: {"status": "failed", "summary": "", "error": ""},
        )

        assert run_research_question("Q", "", parent_agent=_FakeParentAgent()) is None

    def test_falls_back_to_error_text_when_no_summary(self, monkeypatch):
        from agent.autonomy.research import run_research_question

        monkeypatch.setattr(
            "tools.delegate_tool._build_child_agent", lambda **kw: SimpleNamespace()
        )
        monkeypatch.setattr(
            "tools.delegate_tool._run_single_child",
            lambda **kw: {"status": "timeout", "summary": "", "error": "Subagent timed out after 30s"},
        )

        result = run_research_question("Q", "", parent_agent=_FakeParentAgent())
        assert result is not None
        assert "timed out" in result["answer"]

    def test_never_raises_on_delegate_failure(self, monkeypatch):
        from agent.autonomy.research import run_research_question

        def _boom(**kw):
            raise RuntimeError("no credentials configured")

        monkeypatch.setattr("tools.delegate_tool._build_child_agent", _boom)

        assert run_research_question("Q", "", parent_agent=_FakeParentAgent()) is None

    def test_answer_is_truncated(self, monkeypatch):
        from agent.autonomy.research import _MAX_ANSWER_CHARS, run_research_question

        monkeypatch.setattr(
            "tools.delegate_tool._build_child_agent", lambda **kw: SimpleNamespace()
        )
        monkeypatch.setattr(
            "tools.delegate_tool._run_single_child",
            lambda **kw: {"status": "completed", "summary": "x" * (_MAX_ANSWER_CHARS + 500)},
        )

        result = run_research_question("Q", "", parent_agent=_FakeParentAgent())
        assert len(result["answer"]) == _MAX_ANSWER_CHARS
