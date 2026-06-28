"""show_card tool: surface a compact card on the user's voice presence overlay.

Voice-first "show, don't say": when the agent wants to display a short result,
list, link, or a confirm prompt instead of speaking it, it calls show_card and
the desktop renders a glass capsule on the edge-glow presence.
"""

import uuid

from tools.registry import registry
from tools.approval import get_current_session_key
from tools.ui_events import emit_ui_event

SHOW_CARD_SCHEMA = {
    "name": "show_card",
    "description": (
        "Show a compact card on the user's voice presence overlay (a small "
        "glass capsule). Use during voice interactions to SHOW something "
        "(a short result, a list, a link, or a confirm prompt) instead of "
        "speaking it aloud. Not for long text -- keep body under ~200 chars."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "body": {"type": "string", "description": "The main line of the card (short)."},
            "title": {"type": "string", "description": "Optional small uppercase label."},
            "kind": {
                "type": "string",
                "enum": ["info", "result", "approval"],
                "description": "Card style. Default info.",
            },
            "duration_ms": {
                "type": "integer",
                "description": "Auto-dismiss after this many ms. Omit to keep until dismissed.",
            },
            "actions": {
                "type": "array",
                "description": "Optional buttons. Each action's value is sent back as a user message when clicked.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["id", "label"],
                },
            },
        },
        "required": ["body"],
    },
}


def handle_show_card(args: dict, **_kwargs) -> dict:
    """Emit a card.show UI event to the connected client."""
    args = args or {}
    body = args.get("body", "")
    if not body:
        return {"success": False, "error": "body is required"}

    payload = {
        "id": str(uuid.uuid4()),
        "kind": args.get("kind", "info"),
        "title": args.get("title"),
        "body": body,
        "duration": args.get("duration_ms"),
        "actions": args.get("actions"),
    }

    session_key = get_current_session_key(default="")
    delivered = emit_ui_event(session_key, {"event": "card.show", "payload": payload})

    if not delivered:
        return {
            "success": False,
            "error": "No connected client to show the card (cards work in the desktop app voice presence).",
        }
    return {"success": True, "message": "Card shown."}


registry.register(
    name="show_card",
    toolset="tts",
    schema=SHOW_CARD_SCHEMA,
    handler=handle_show_card,
    description="Show a compact card on the voice presence overlay.",
    emoji="🪧",
)
