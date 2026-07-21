"""brain_store_document -- the universal Brain feed-in tool.

Lets chat, the subconscious tick, reflection, and dreaming all hand the
Brain durable reference material they encounter (a policy pasted into chat,
a summary worth keeping, a document surfaced during background thinking)
without needing a watched folder. Writes it under
``HERMES_HOME/brain/collected/<source-slug>/`` and indexes it immediately
(see ``tools/brain/collected.py`` + ``tools/brain/indexer.py``'s
``index_single_document``) so it's searchable via ``recall_files`` right
away.

Registered the same way ``recall_files`` is (``tools/brain_tool.py``):
``from tools.registry import registry`` at module scope, one
``registry.register(...)`` call. Unlike ``recall_files`` this tool is not
gated on ``brain.enabled`` -- it's the seed mechanism a cold-start Brain
needs, so it works even before the user has configured any watched folders.
"""

from __future__ import annotations

from typing import Any, Dict

from tools.registry import registry, tool_error, tool_result


def _store_document(args: Dict[str, Any], **_: Any) -> str:
    title = str(args.get("title") or "").strip()
    text = str(args.get("text") or "")
    source = str(args.get("source") or "").strip()
    raw_ref = args.get("ref")
    ref = str(raw_ref).strip() or None if raw_ref is not None else None

    if not title:
        return tool_error("title is required")
    if not text.strip():
        return tool_error("text is required")
    if not source:
        return tool_error("source is required")

    from tools.brain.collected import write_collected_document

    result = write_collected_document(source=source, title=title, text=text, ref=ref)
    return tool_result(success=True, **result)


registry.register(
    name="brain_store_document",
    toolset="memory",
    emoji="🧠",
    handler=_store_document,
    schema={
        "name": "brain_store_document",
        "description": (
            "Save durable reference material into Marvi's local Brain index so it's "
            "recallable later via recall_files -- a policy, a decision, a summary, or "
            "a document encountered in chat or background thinking that's worth "
            "remembering beyond this conversation. Not for transient chat context; "
            "only durable, reusable reference material. Repeated saves with the same "
            "ref and unchanged text are a no-op."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short human-readable title."},
                "text": {"type": "string", "description": "The document text to store."},
                "source": {
                    "type": "string",
                    "description": "Where this came from, e.g. 'chat', 'subconscious', 'reflection', 'dreaming'.",
                },
                "ref": {
                    "type": "string",
                    "description": "Optional stable identifier (URL, message id, file path) used for dedup across repeated saves.",
                },
            },
            "required": ["title", "text", "source"],
        },
    },
)
