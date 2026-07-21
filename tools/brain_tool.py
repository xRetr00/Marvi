"""Fast local Brain recall tool."""

from __future__ import annotations

import json
from typing import Any, Dict

from tools.registry import registry, tool_error, tool_result
from tools.brain.indexer import brain_config
from tools.brain.store import BrainStore


def _enabled() -> bool:
    return brain_config()["enabled"]


def _recall_files(args: Dict[str, Any], **_: Any) -> str:
    # Must return a string: registry.dispatch's _normalize_handler_result only
    # accepts a str or the multimodal-content envelope — a bare dict falls into
    # the error branch, which is why recall results never reached the agent.
    store = BrainStore()
    try:
        results = store.search(str(args.get("query") or ""), int(args.get("limit") or 8))
    except Exception as exc:  # noqa: BLE001
        return tool_error(f"Brain search failed: {exc}")
    finally:
        store.close()
    if not results:
        return "No matching documents in the local Brain index."
    return tool_result(json.dumps({"results": results}, ensure_ascii=False))


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
