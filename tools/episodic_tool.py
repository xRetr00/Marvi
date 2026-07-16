"""Episodic memory recall tool — "has this happened before?" for Marvi.

Loop 1 of the memory-maturity round (see
``docs/superpowers/specs/2026-07-17-marvi-memory-maturity-spec.md``).
Registration mirrors ``tools/brain_tool.py`` exactly: ``from tools.registry
import registry`` then ``registry.register(...)`` — NOT ``from tools import
registry`` (module-vs-instance import bug fixed in 887bc1b5e, guarded by
``tests/tools/test_tool_registration_smoke.py``).
"""

from __future__ import annotations

from typing import Any, Dict

from agent.memory import episodic
from tools.registry import registry, tool_error, tool_result


def _enabled() -> bool:
    return episodic.episodic_config()["enabled"]


def _recall_episode(args: Dict[str, Any], **_: Any) -> str:
    kind = args.get("kind") or None
    if kind:
        kind = str(kind).strip()
        if kind not in episodic.VALID_KINDS:
            return tool_error(
                f"Invalid kind '{kind}'. Use one of: {', '.join(sorted(episodic.VALID_KINDS))}."
            )

    limit = args.get("limit")
    try:
        limit = int(limit) if limit is not None else 20
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))

    episodes = episodic.query(
        text=args.get("query") or None,
        kind=kind,
        since=args.get("since") or None,
        until=args.get("until") or None,
        limit=limit,
    )
    if not episodes:
        return tool_result(success=True, count=0, formatted="No matching episodes found.")

    formatted = "\n".join(episodic.format_episode(ep) for ep in episodes)
    return tool_result(success=True, count=len(episodes), episodes=episodes, formatted=formatted)


registry.register(
    name="recall_episode",
    toolset="memory",
    check_fn=_enabled,
    emoji="🕰️",
    handler=_recall_episode,
    schema={
        "name": "recall_episode",
        "description": (
            "Search Marvi's episodic memory — a time-indexed log of what actually happened "
            "(conversations, tasks, room/presence events, proactive nudges, device/arrival "
            "signals, learning proposals). Use it to answer questions like 'what did I do "
            "last Tuesday', 'when did I last touch X', or 'has this happened before' before "
            "assuming something is new."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text search over title/summary/entities."},
                "kind": {
                    "type": "string",
                    "enum": sorted(episodic.VALID_KINDS),
                    "description": "Restrict to one episode kind.",
                },
                "since": {"type": "string", "description": "ISO timestamp lower bound (inclusive)."},
                "until": {"type": "string", "description": "ISO timestamp upper bound (inclusive)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Default 20."},
            },
        },
    },
)
