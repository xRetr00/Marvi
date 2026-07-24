"""Self-directed web research — spec §1.2.

The reflection/dreaming prompt can emit ``<research>{"question":...,
"why":...}</research>`` when the narrative holds an open question worth
resolving. ``cron/scheduler.py`` parses those blocks (see
``cron.subconscious.extract_autonomy_requests``) and, budget permitting,
calls :func:`run_research_question` here to actually go find the answer —
Marvi resolving its own curiosity between ticks.

Implementation reuses ``tools/delegate_tool.py``'s child-agent machinery
directly (``_build_child_agent`` + ``_run_single_child``) rather than the
public ``delegate_task`` tool wrapper, because the wrapper is designed for
in-conversation tool calls and always inherits the parent's full toolset.
Here we need an explicitly narrowed, read-only ``web`` toolset (no file
writes, no terminal) regardless of what toolset the calling cron job itself
was granted — a bounded, disposable research assistant, never one that can
touch disk or execute commands.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Read-only, no-writes toolset for the research subagent (spec §1.2: "web+read
# toolset, no writes"). "web" = web_search + web_extract only (toolsets.py) —
# deliberately NOT "file" (which includes write_file/patch).
RESEARCH_TOOLSETS = ["web"]

# Bounded: this is a single self-directed question, not an open-ended task.
RESEARCH_MAX_ITERATIONS = 12

_MAX_ANSWER_CHARS = 2000


def _build_research_prompt(question: str, why: str) -> str:
    parts = [
        "You are a focused research assistant helping Marvi (a personal AI "
        "companion) answer one question it noticed while reflecting on its "
        "own notes about the user. Use web search to find a real, current "
        "answer — do not guess or invent facts.",
        "",
        f"QUESTION: {question}",
    ]
    if why:
        parts.append(f"WHY MARVI IS ASKING: {why}")
    parts.append(
        "\nReply with a short, direct answer (2-5 sentences). If you can't "
        "find a confident answer, say so plainly instead of speculating."
    )
    return "\n".join(parts)


def run_research_question(
    question: str,
    why: str = "",
    *,
    parent_agent: Any,
    task_index: int = 0,
) -> Optional[Dict[str, Any]]:
    """Spawn a bounded, web-only child agent to answer ``question``.

    Returns ``{"answer": str, "status": str}`` on any completed run (even a
    "couldn't find it" answer is still a completed run — the caller decides
    what to do with the text), or ``None`` if the child couldn't be built or
    run at all (missing parent context, delegate_tool unavailable, hard
    failure). Never raises.
    """
    question = str(question or "").strip()
    if not question or parent_agent is None:
        return None
    try:
        from tools.delegate_tool import _build_child_agent, _run_single_child

        goal = _build_research_prompt(question, str(why or "").strip())
        child = _build_child_agent(
            task_index=task_index,
            goal=goal,
            context=None,
            toolsets=RESEARCH_TOOLSETS,
            model=None,
            max_iterations=RESEARCH_MAX_ITERATIONS,
            task_count=1,
            parent_agent=parent_agent,
            role="leaf",
        )
        result = _run_single_child(
            task_index=task_index,
            goal=goal,
            child=child,
            parent_agent=parent_agent,
        )
        status = str(result.get("status") or "failed")
        summary = str(result.get("summary") or result.get("error") or "").strip()
        if not summary:
            return None
        return {"answer": summary[:_MAX_ANSWER_CHARS], "status": status}
    except Exception:
        logger.debug("autonomy research: run_research_question failed", exc_info=True)
        return None
