"""Graph-aware recall tool — "what's connected to X, and why" for Marvi.

Part 2 of the "Marvi freedom and graph mind" spec (§2.4,
``docs/superpowers/specs/2026-07-20-marvi-freedom-and-graph-mind-spec.md``).
Registration mirrors ``tools/episodic_tool.py``/``tools/brain_tool.py``
exactly: ``from tools.registry import registry`` then
``registry.register(...)`` — NOT ``from tools import registry`` (the
module-vs-instance import bug fixed in 887bc1b5e, guarded by
``tests/tools/test_tool_registration_smoke.py``).
"""

from __future__ import annotations

from typing import Any, Dict

from agent.memory import graph
from tools.registry import registry, tool_error, tool_result


def _enabled() -> bool:
    return graph.graph_config()["enabled"]


def _recall_graph(args: Dict[str, Any], **_: Any) -> str:
    query_text = str(args.get("query") or "").strip()
    if not query_text:
        return tool_error("query is required — the node label or topic to look up.")

    depth = args.get("depth")
    try:
        depth = int(depth) if depth is not None else 1
    except (TypeError, ValueError):
        depth = 1
    depth = max(1, min(depth, 3))

    center = graph.find_node(query_text) or None
    if center is None:
        matches = graph.query(text=query_text, limit=1)
        center = matches[0] if matches else None
    if center is None:
        return tool_result(
            success=True,
            found=False,
            formatted=f"No graph node found for '{query_text}' yet. Marvi's mind map fills in as it learns.",
        )

    neighborhood = graph.neighbors(center["id"], depth=depth)
    nodes_by_id = {n["id"]: n for n in neighborhood["nodes"]}
    formatted = graph.format_neighborhood(center, neighborhood["edges"], nodes_by_id)

    return tool_result(
        success=True,
        found=True,
        node=center,
        neighbors=neighborhood["nodes"],
        edges=neighborhood["edges"],
        formatted=formatted,
    )


registry.register(
    name="recall_graph",
    toolset="memory",
    check_fn=_enabled,
    emoji="🕸️",
    handler=_recall_graph,
    schema={
        "name": "recall_graph",
        "description": (
            "Look up a node in Marvi's knowledge graph (people, projects, facts, events, "
            "preferences, places, topics, goals, devices, orgs) and return it plus its "
            "neighborhood as readable relations, e.g. 'NeuDocs —built_with→ Marvi; "
            "bakery-job —funds→ NeuDocs'. Use it to answer 'what's connected to X' or "
            "'why does he want Y' questions the flat memory/episode stores can't answer directly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Node label or free-text topic to look up."},
                "depth": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3,
                    "description": "How many hops of neighbors to include. Default 1.",
                },
            },
            "required": ["query"],
        },
    },
)
