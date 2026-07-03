"""show_card tool: surface a compact card on the user's voice presence.

Voice-first "show, don't say": when the agent wants to display a short result,
list, link, or a confirm prompt instead of speaking it, it calls show_card and
the desktop renders it on the Dynamic Island (the always-present pill at the top
of the screen). The desktop only shows the card when the user is NOT looking at
the main Marvi window, so it never competes with the chat.
"""

import uuid

from tools.registry import registry
from tools.approval import get_current_session_key
from tools.ui_events import emit_ui_event

SHOW_CARD_SCHEMA = {
    "name": "show_card",
    "description": (
        "Show a compact card on the user's desktop voice presence (the Dynamic "
        "Island pill at the top of the screen). Best for VOICE / hands-free "
        "interactions: when the user is talking to you or working in ANOTHER app, "
        "SHOW a short result, a key fact, a link, a list, or a quick confirm "
        "prompt instead of only speaking it. Do NOT rely on it when the user is "
        "actively looking at the main Marvi chat window — they'll read your reply "
        "there, and the desktop deliberately suppresses the card while its window "
        "is focused, so calling it then is wasted. It is always safe to call (a "
        "no-op when suppressed or when no desktop is watching). Keep body under "
        "~200 chars. Use actions for yes/no or quick replies — an action's value "
        "is sent back as the user's next message when tapped."
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
    """Surface a card on the desktop voice presence.

    The desktop renders the capsule directly from this tool call (the invocation
    always streams to the client), so this handler just validates input and
    fires a best-effort UI event for any client that listens on the structured
    event stream. It never reports failure for a missing client — show_card is an
    advisory UI hint, so a no-op when no desktop is watching is success.
    """
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

    # Best-effort secondary delivery for the structured (/v1/runs) event stream.
    # The desktop chat path renders from the tool call itself, so this is not
    # required for the card to appear there.
    session_key = get_current_session_key(default="")
    emit_ui_event(session_key, {"event": "card.show", "payload": payload})

    return {"success": True, "message": "Card shown on the voice presence."}


registry.register(
    name="show_card",
    toolset="tts",
    schema=SHOW_CARD_SCHEMA,
    handler=handle_show_card,
    description="Show a compact card on the voice presence overlay.",
    emoji="🪧",
)
