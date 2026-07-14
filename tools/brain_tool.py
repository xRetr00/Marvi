"""Fast local Brain recall tool."""

from __future__ import annotations

from typing import Any, Dict

from tools.registry import registry
from tools.brain.indexer import brain_config
from tools.brain.store import BrainStore


def _enabled() -> bool:
    return brain_config()["enabled"]


def _recall_files(args: Dict[str, Any], **_: Any) -> Dict[str, Any]:
    store = BrainStore()
    try:
        return {"success": True, "results": store.search(str(args.get("query") or ""), int(args.get("limit") or 8))}
    finally:
        store.close()


registry.register(
    name="recall_files",
    toolset="memory",
    check_fn=_enabled,
    emoji="🧠",
    handler=_recall_files,
    schema={
        "name": "recall_files",
        "description": "Search the user's explicitly indexed local Brain folders. Returns short matching snippets and paths; use read_file only when one result needs exact context.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words or phrase to find."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
    },
)
